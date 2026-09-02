"""Freshdesk API v2 연동 (신규 티켓 등록 + 재시도)."""
from __future__ import annotations

import html
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

from config import FreshdeskEnvConfig

logger = logging.getLogger("ticket_sync.freshdesk")


@dataclass
class FreshdeskResult:
    success: bool
    ticket_id: Optional[int] = None
    error: Optional[str] = None


class FreshdeskClient:
    def __init__(self, env: FreshdeskEnvConfig, max_retries: int = 3, backoff_seconds: float = 2.0):
        self.env = env
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.base_url = f"https://{env.domain}/api/v2"
        self.session = requests.Session()
        self.session.auth = (env.api_key, "X")
        self.session.headers.update({"Content-Type": "application/json"})

    @staticmethod
    def _to_iso_date(mdy: str) -> Optional[str]:
        """'7/27/2026' 같은 크롤러의 M/D/YYYY 표기를 Freshdesk custom_date 필드가
        요구하는 'YYYY-MM-DD' 형식으로 바꾼다. 형식이 다르면 그대로 두지 않고 None 을
        반환해 잘못된 값이 전송되지 않도록 한다."""
        if not mdy:
            return None
        parts = mdy.split("/")
        if len(parts) != 3:
            return None
        month, day, year = parts
        try:
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        except ValueError:
            return None

    @staticmethod
    def _extract_email(text: str) -> str:
        """'동현 김\\nkim92.dh@kt.com' 처럼 이름과 이메일이 섞여 있어도 이메일만 뽑아낸다.
        Freshdesk 는 requester_id/email/phone 등 중 최소 하나가 없으면 티켓 생성 자체를
        거부하므로, 요청자 이메일을 신뢰성 있게 뽑아내는 것이 중요하다."""
        if not text:
            return ""
        m = _EMAIL_RE.search(text)
        return m.group(0) if m else ""

    def build_payload(self, ticket: dict, priority_map: dict) -> dict:
        ms_case_id = ticket.get("ms_case_id") or ""
        subject = f"(#{ms_case_id}){ticket.get('title', '')}".strip()

        description_lines = [
            f"<p><b>Microsoft Case ID:</b> {ms_case_id}</p>",
            f"<p><b>제품/서비스:</b> {ticket.get('product', '')}</p>",
            f"<p><b>상태:</b> {ticket.get('status', '')}</p>",
            f"<p><b>심각도:</b> {ticket.get('severity', '')}</p>",
            f"<p><b>요청자:</b> {ticket.get('requester', '')}</p>",
            f"<p><b>담당자:</b> {ticket.get('assignee', '')}</p>",
            f"<p><b>생성일:</b> {ticket.get('created_at', '')}</p>",
            f"<p><b>최종 수정일:</b> {ticket.get('modified_at', '')}</p>",
        ]
        case_url = ticket.get("case_url")
        if case_url:
            description_lines.append(f"<p><b>Engage Center 케이스 URL:</b> <a href=\"{case_url}\">{case_url}</a></p>")
        # "설명"(문제 세부정보의 질문/답변 요약)은 첫 등록 시 의도적으로 생략한다.
        # 같은 내용이 대화 노트(add_conversation_notes)로 하나씩 그대로 등록되므로
        # 여기서도 넣으면 내용이 중복된다.

        priority = self.env.default_priority
        severity = ticket.get("severity")
        if severity and severity in priority_map:
            priority = priority_map[severity]

        custom_fields = {}
        if self.env.custom_field_case_id and ms_case_id:
            custom_fields[self.env.custom_field_case_id] = ms_case_id
        if self.env.custom_field_received_date:
            received_date = self._to_iso_date(ticket.get("modified_at", ""))
            if received_date:
                custom_fields[self.env.custom_field_received_date] = received_date

        email = self._extract_email(ticket.get("requester", ""))

        payload = {
            "subject": subject,
            "description": "".join(description_lines),
            "priority": priority,
            "status": self.env.default_status,
            "tags": [f"ms-case-{ms_case_id}"] if ms_case_id else ["ms-engage-center"],
            "custom_fields": custom_fields,
        }
        if email:
            payload["email"] = email
        else:
            # Freshdesk 는 requester_id/email/phone 등 중 최소 하나가 없으면 티켓 생성을
            # 거부한다. 요청자 이메일을 못 찾은 경우에도 실패로 끝나지 않도록 대체 식별자를 둔다.
            payload["unique_external_id"] = f"ms-case-{ms_case_id}" if ms_case_id else "ms-engage-center-unknown"
        if self.env.default_group:
            payload["group_id"] = self.env.default_group  # Freshdesk 그룹 ID (숫자)
        if self.env.default_responder:
            payload["responder_id"] = self.env.default_responder  # Freshdesk 에이전트 ID (숫자)
        return payload

    def create_ticket(self, ticket: dict, priority_map: dict) -> FreshdeskResult:
        """신규 Freshdesk 티켓을 만든다.

        POST /tickets 는 멱등(idempotent)하지 않다. 실제로 "서버 오류/네트워크 오류로 실패한
        것처럼 보이는 응답을 받고 재시도했더니, 사실은 앞선 요청이 이미 처리되어 티켓이 만들어져
        있어서 똑같은 티켓이 중복 생성"되는 사고가 있었다. Freshdesk API 는 생성 요청에 대해
        클라이언트가 제시하는 idempotency key 를 지원하지 않고, 방금 만든 티켓을 그룹/최근 목록
        조회로 되짚어 확인하는 방법도 API 키의 그룹 조회 권한에 따라 조회가 안 될 수 있어(실제
        확인됨) 신뢰할 수 없다.

        따라서 "생성 요청을 보냈는데 성공/실패를 확신할 수 없는" 응답(5xx, 타임아웃/연결 오류)에
        대해서는 절대로 같은 생성 요청을 다시 보내지 않는다. 실패로 기록해 반환하고, 실제로
        재시도가 필요하면 호출 측(main.py)이 다음 실행에서 로컬 SQLite 의 상태(성공 여부가 아직
        기록되지 않은 케이스만)를 보고 다시 시도하도록 한다. 반대로 429(레이트 리밋)는 요청이
        처리되기 전에 거절된 것이 확실하므로 재시도해도 중복 위험이 없다."""
        payload = self.build_payload(ticket, priority_map)
        url = f"{self.base_url}/tickets"

        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.post(url, json=payload, timeout=30)
                if resp.status_code in (200, 201):
                    ticket_id = resp.json().get("id")
                    return FreshdeskResult(success=True, ticket_id=ticket_id)

                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", self.backoff_seconds))
                    logger.warning("Freshdesk rate limit(429). %d초 후 재시도합니다.", retry_after)
                    time.sleep(retry_after)
                    last_error = f"429 rate limited: {resp.text}"
                    continue

                if 500 <= resp.status_code < 600:
                    # 서버에서 요청을 처리하던 도중 응답만 실패했을 가능성이 있어(=티켓이 이미
                    # 생성됐을 수 있어) 같은 생성 요청을 다시 보내지 않고 실패로 종료한다.
                    last_error = f"{resp.status_code} server error: {resp.text}"
                    logger.error(
                        "Freshdesk 서버 오류(%s): 티켓이 이미 생성됐을 가능성이 있어 재시도하지 "
                        "않고 실패로 처리합니다. 다음 동기화 실행 시 재시도됩니다.",
                        resp.status_code,
                    )
                    return FreshdeskResult(success=False, error=last_error)

                last_error = f"{resp.status_code}: {resp.text}"
                logger.error("Freshdesk 티켓 생성 실패(재시도 불가 오류): %s", last_error)
                return FreshdeskResult(success=False, error=last_error)

            except requests.RequestException as exc:
                # 타임아웃/연결 끊김도 응답만 못 받았을 뿐 서버 쪽에서는 이미 티켓이 생성됐을 수
                # 있는 경우라 5xx 와 동일하게 재시도 없이 실패로 종료한다.
                last_error = str(exc)
                logger.error(
                    "Freshdesk 요청 중 네트워크 오류: 티켓이 이미 생성됐을 가능성이 있어 재시도하지 "
                    "않고 실패로 처리합니다. 다음 동기화 실행 시 재시도됩니다. (%s)",
                    exc,
                )
                return FreshdeskResult(success=False, error=last_error)

        return FreshdeskResult(success=False, error=last_error or "알 수 없는 오류")

    def find_ticket_by_case_tag(self, ms_case_id: str) -> Optional[dict]:
        """Freshdesk 검색 API로 이 Microsoft Case ID 태그를 가진 티켓이 이미 있는지 확인한다
        (예약 실행 전용 신규 알고리즘의 중복 방지 1차 확인 — 내부 추적 테이블에 기록이 없을 때의
        안전망). 있으면 티켓 dict(첫 번째 결과)를, 없으면 None을 반환한다."""
        tag = f"ms-case-{ms_case_id}"
        url = f"{self.base_url}/search/tickets"
        try:
            resp = self.session.get(url, params={"query": f"\"tag:'{tag}'\""}, timeout=30)
            if resp.status_code != 200:
                logger.warning("Freshdesk 태그 검색 실패(%s): %s", resp.status_code, resp.text)
                return None
            results = resp.json().get("results", [])
            return results[0] if results else None
        except requests.RequestException as exc:
            logger.warning("Freshdesk 태그 검색 중 네트워크 오류: %s", exc)
            return None

    _CASE_TAG_RE = re.compile(r"^ms-case-(.+)$")

    @classmethod
    def extract_case_id_from_tags(cls, tags: Optional[list]) -> Optional[str]:
        """create_ticket()/create_new_case_ticket() 이 붙이는 'ms-case-<ID>' 태그에서 다시
        케이스ID를 뽑아낸다("Freshdesk Management"의 완료 케이스 노트 추가 기능처럼, Freshdesk
        티켓 목록에서 거꾸로 Engage Center 케이스를 찾아야 할 때 쓴다)."""
        for tag in tags or []:
            m = cls._CASE_TAG_RE.match(tag or "")
            if m:
                return m.group(1)
        return None

    def list_tickets_created_between(self, start_date: str, end_date: str) -> list[dict]:
        """생성일이 start_date~end_date(둘 다 'YYYY-MM-DD', 포함) 사이인 티켓을 전부 가져온다.

        Freshdesk의 GET /tickets?created_since= 는 그 이후 전체를 반환할 뿐 종료일 필터가
        없어서, 끝 날짜는 여기서 직접 잘라낸다. 기본 정렬이 생성일 오름차순이라 종료일을
        넘는 항목이 나오기 시작하면 그 뒤로는 더 볼 필요가 없어 페이지 조회를 멈춘다."""
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
        url = f"{self.base_url}/tickets"
        results: list[dict] = []
        page = 1
        per_page = 100
        while True:
            try:
                resp = self.session.get(
                    url,
                    params={
                        "created_since": f"{start_date}T00:00:00Z",
                        "per_page": per_page,
                        "page": page,
                        "order_by": "created_at",
                        "order_type": "asc",
                    },
                    timeout=30,
                )
            except requests.RequestException as exc:
                logger.warning("Freshdesk 티켓 목록 조회 중 네트워크 오류: %s", exc)
                break
            if resp.status_code != 200:
                logger.warning("Freshdesk 티켓 목록 조회 실패(%s): %s", resp.status_code, resp.text)
                break
            batch = resp.json()
            if not batch:
                break

            past_end = False
            for t in batch:
                created_at = (t.get("created_at") or "")[:19]
                try:
                    created_dt = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    created_dt = None
                if created_dt and created_dt > end_dt:
                    past_end = True
                    continue
                results.append(t)

            if past_end or len(batch) < per_page:
                break
            page += 1
        return results

    def list_open_tickets(self) -> list[dict]:
        """접수일과 무관하게, 현재 Open/Pending 상태인 티켓을 전부 가져온다("열려있는
        케이스 전체 확인" 기능 전용). Freshdesk 검색 API로 상태만 걸러서 가져오므로,
        List Tickets 엔드포인트로 전체를 페이지네이션하며 상태를 일일이 걸러내는 것보다
        훨씬 빠르다.

        Freshdesk 검색 API는 결과를 최대 300건(페이지당 30건 × 최대 10페이지)까지만
        준다는 제약이 있다 — 열려있는 케이스가 그보다 많으면 일부만 조회된다."""
        url = f"{self.base_url}/search/tickets"
        results: list[dict] = []
        page = 1
        while True:
            try:
                resp = self.session.get(
                    url, params={"query": "\"status:2 OR status:3\"", "page": page}, timeout=30,
                )
            except requests.RequestException as exc:
                logger.warning("Freshdesk 열린 티켓 검색 중 네트워크 오류: %s", exc)
                break
            if resp.status_code != 200:
                logger.warning("Freshdesk 열린 티켓 검색 실패(%s): %s", resp.status_code, resp.text)
                break
            data = resp.json()
            batch = data.get("results", [])
            if not batch:
                break
            results.extend(batch)
            if len(results) >= data.get("total", 0) or page >= 10:
                break
            page += 1
        return results

    def get_ticket(self, ticket_id: int) -> Optional[dict]:
        """티켓의 현재 상태를 확인한다(예약 실행이 완료 감지 전에, 이미 Freshdesk 쪽에서
        직접 처리(해결/종료)되지 않았는지 먼저 확인하는 용도)."""
        url = f"{self.base_url}/tickets/{ticket_id}"
        try:
            resp = self.session.get(url, timeout=30)
            if resp.status_code != 200:
                logger.warning("Freshdesk 티켓 조회 실패(#%s, %s): %s", ticket_id, resp.status_code, resp.text)
                return None
            return resp.json()
        except requests.RequestException as exc:
            logger.warning("Freshdesk 티켓 조회 중 네트워크 오류(#%s): %s", ticket_id, exc)
            return None

    def build_new_case_payload(self, ticket: dict, priority_map: dict) -> dict:
        """예약 실행 전용 신규 알고리즘의 티켓 생성 페이로드. 기존 build_payload()와 달리
        (1) 접수일에 생성일(created_at)을 쓰고(기존은 최종수정일), (2) 문제 세부정보
        (제품/범주/문제/설명)를 Engage Center 그대로 줄바꿈 유지해서 설명에 포함한다.
        기존 build_payload/create_ticket 은 대시보드·케이스 화면의 수동 등록 흐름이 계속
        쓰므로 그대로 두고, 이건 완전히 별도의 페이로드 빌더다."""
        ms_case_id = ticket.get("ms_case_id") or ""
        subject = f"(#{ms_case_id}){ticket.get('title', '')}".strip()

        description_lines = [
            f"<p><b>Microsoft Case ID:</b> {ms_case_id}</p>",
            f"<p><b>제품/서비스:</b> {ticket.get('product', '')}</p>",
            f"<p><b>상태:</b> {ticket.get('status', '')}</p>",
            f"<p><b>심각도:</b> {ticket.get('severity', '')}</p>",
            f"<p><b>요청자:</b> {ticket.get('requester', '')}</p>",
            f"<p><b>담당자:</b> {ticket.get('assignee', '')}</p>",
            f"<p><b>생성일:</b> {ticket.get('created_at', '')}</p>",
            f"<p><b>최종 수정일:</b> {ticket.get('modified_at', '')}</p>",
        ]
        case_url = ticket.get("case_url")
        if case_url:
            description_lines.append(f"<p><b>Engage Center 케이스 URL:</b> <a href=\"{case_url}\">{case_url}</a></p>")

        detail_fields = [
            ("제품", ticket.get("product", "")),
            ("범주", ticket.get("category", "")),
            ("문제", ticket.get("problem_type", "")),
            ("설명", ticket.get("summary", "")),
        ]
        if any(v for _, v in detail_fields):
            description_lines.append("<hr><p><b>문제 세부 정보</b></p>")
            for label, value in detail_fields:
                value_html = html.escape(value or "").replace("\n", "<br>")
                description_lines.append(f"<p><b>{label}:</b><br>{value_html}</p>")

        priority = self.env.default_priority
        severity = ticket.get("severity")
        if severity and severity in priority_map:
            priority = priority_map[severity]

        custom_fields = {}
        if self.env.custom_field_case_id and ms_case_id:
            custom_fields[self.env.custom_field_case_id] = ms_case_id
        if self.env.custom_field_received_date:
            received_date = self._to_iso_date(ticket.get("created_at", ""))
            if received_date:
                custom_fields[self.env.custom_field_received_date] = received_date

        email = self._extract_email(ticket.get("requester", ""))

        payload = {
            "subject": subject,
            "description": "".join(description_lines),
            "priority": priority,
            "status": self.env.default_status,
            "tags": [f"ms-case-{ms_case_id}"] if ms_case_id else ["ms-engage-center"],
            "custom_fields": custom_fields,
        }
        if email:
            payload["email"] = email
        else:
            payload["unique_external_id"] = f"ms-case-{ms_case_id}" if ms_case_id else "ms-engage-center-unknown"
        if self.env.default_group:
            payload["group_id"] = self.env.default_group
        if self.env.default_responder:
            payload["responder_id"] = self.env.default_responder
        return payload

    def create_new_case_ticket(self, ticket: dict, priority_map: dict) -> FreshdeskResult:
        """build_new_case_payload() 를 써서 신규 티켓을 만든다. 재시도/오류 처리 규칙은
        create_ticket() 과 동일하다(응답을 확신할 수 없는 오류는 재시도하지 않고 실패로 반환 —
        중복 생성 방지)."""
        payload = self.build_new_case_payload(ticket, priority_map)
        url = f"{self.base_url}/tickets"

        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.post(url, json=payload, timeout=30)
                if resp.status_code in (200, 201):
                    ticket_id = resp.json().get("id")
                    return FreshdeskResult(success=True, ticket_id=ticket_id)

                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", self.backoff_seconds))
                    logger.warning("Freshdesk rate limit(429). %d초 후 재시도합니다.", retry_after)
                    time.sleep(retry_after)
                    last_error = f"429 rate limited: {resp.text}"
                    continue

                if 500 <= resp.status_code < 600:
                    last_error = f"{resp.status_code} server error: {resp.text}"
                    logger.error(
                        "Freshdesk 서버 오류(%s): 티켓이 이미 생성됐을 가능성이 있어 재시도하지 "
                        "않고 실패로 처리합니다.", resp.status_code,
                    )
                    return FreshdeskResult(success=False, error=last_error)

                last_error = f"{resp.status_code}: {resp.text}"
                logger.error("Freshdesk 티켓 생성 실패(재시도 불가 오류): %s", last_error)
                return FreshdeskResult(success=False, error=last_error)

            except requests.RequestException as exc:
                last_error = str(exc)
                logger.error(
                    "Freshdesk 요청 중 네트워크 오류: 티켓이 이미 생성됐을 가능성이 있어 재시도하지 "
                    "않고 실패로 처리합니다. (%s)", exc,
                )
                return FreshdeskResult(success=False, error=last_error)

        return FreshdeskResult(success=False, error=last_error or "알 수 없는 오류")

    def add_single_note(self, ticket_id: int, body_html: str, private: bool = True) -> FreshdeskResult:
        """노트 하나를 등록한다. private=False 면 요청자에게도 보이는 공개 노트로 등록되며,
        Freshdesk 설정에 따라 요청자에게 이메일 알림이 갈 수 있다 — 예약 실행 설정 화면에서
        관리자가 명시적으로 "공개"를 선택했을 때만 private=False 로 호출해야 한다."""
        url = f"{self.base_url}/tickets/{ticket_id}/notes"
        payload = {"body": body_html, "private": private}
        try:
            resp = self.session.post(url, json=payload, timeout=30)
            if resp.status_code in (200, 201):
                return FreshdeskResult(success=True, ticket_id=ticket_id)
            error = f"{resp.status_code}: {resp.text}"
            logger.warning("노트 등록 실패(티켓 #%s): %s", ticket_id, error)
            return FreshdeskResult(success=False, error=error)
        except requests.RequestException as exc:
            logger.warning("노트 등록 중 네트워크 오류(티켓 #%s): %s", ticket_id, exc)
            return FreshdeskResult(success=False, error=str(exc))

    def close_ticket(self, ticket_id: int) -> FreshdeskResult:
        """티켓 상태를 완료(Closed, 5)로 바꾼다 — "티켓 종료 처리" 기능에서 Engage Center
        완료 확인 후 마지막 대화를 노트로 남긴 다음, Freshdesk 티켓 자체도 종료 상태로
        맞추는 데 쓴다."""
        url = f"{self.base_url}/tickets/{ticket_id}"
        try:
            resp = self.session.put(url, json={"status": 5}, timeout=30)
            if resp.status_code in (200, 201):
                return FreshdeskResult(success=True, ticket_id=ticket_id)
            error = f"{resp.status_code}: {resp.text}"
            logger.warning("티켓 종료 처리 실패(#%s): %s", ticket_id, error)
            return FreshdeskResult(success=False, error=error)
        except requests.RequestException as exc:
            logger.warning("티켓 종료 처리 중 네트워크 오류(#%s): %s", ticket_id, exc)
            return FreshdeskResult(success=False, error=str(exc))

    def add_conversation_notes(self, ticket_id: int, messages: list[dict]) -> list["FreshdeskResult"]:
        """Engage Center 케이스의 메일 대화를 오래된 것부터 순서대로 비공개 노트로 등록해,
        실제로 주고받은 대화 흐름을 티켓 안에 재현한다.

        공개 답장(POST /tickets/{id}/reply) 대신 비공개 노트(private note)를 쓰는 이유:
        reply 는 실제 요청자/참조자 이메일로 알림을 다시 발송한다. 이미 지난 대화를 그대로
        옮기는 것뿐인데 실제 인물들에게 뜬금없는 메일이 재발송되면 안 되기 때문에, 내부에서만
        보이는 비공개 노트로 등록한다."""
        results: list[FreshdeskResult] = []
        url = f"{self.base_url}/tickets/{ticket_id}/notes"
        for msg in messages:
            body_html = msg.get("body_html", "")
            if not msg.get("sender_name") and (msg.get("sender_email") or msg.get("sent")):
                header_bits = []
                if msg.get("sender_email"):
                    header_bits.append(f"<b>발신:</b> {msg['sender_email']}")
                if msg.get("sent"):
                    header_bits.append(f"<b>일시:</b> {msg['sent']}")
                header = (
                    '<div style="font-size:12px;color:#666;margin-bottom:6px;">'
                    + " | ".join(header_bits) + "</div><hr>"
                )
                body_html = header + body_html
            payload = {"body": body_html, "private": True}
            try:
                resp = self.session.post(url, json=payload, timeout=30)
                if resp.status_code in (200, 201):
                    results.append(FreshdeskResult(success=True, ticket_id=ticket_id))
                else:
                    error = f"{resp.status_code}: {resp.text}"
                    logger.warning("대화 노트 등록 실패(티켓 #%s): %s", ticket_id, error)
                    results.append(FreshdeskResult(success=False, error=error))
            except requests.RequestException as exc:
                logger.warning("대화 노트 등록 중 네트워크 오류(티켓 #%s): %s", ticket_id, exc)
                results.append(FreshdeskResult(success=False, error=str(exc)))
        return results
