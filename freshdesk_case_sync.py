"""예약 실행(스케줄러) 전용 Freshdesk 신규 동기화 알고리즘.

기존 main.run_sync() 파이프라인(케이스 목록/상세를 전부 긁어 로컬 tickets 테이블 = /cases
화면에 저장)과 완전히 분리된 별도 흐름이다. 대시보드 "지금 실행"과 /cases 화면의 수동 Freshdesk
등록 기능은 이 모듈과 무관하게 지금까지 그대로 동작한다.

3단계:
1. sync_new_cases()      — Engage Center 오픈 케이스를 확인해, Freshdesk에 태그로 없는 것만
                            새로 등록한다. /cases 화면에는 아무것도 쓰지 않고, 내부 전용
                            추적 테이블(storage.freshdesk_case_links)에만 기록한다. 등록 직후,
                            커뮤니케이션 중 가장 오래된(=최초 문의) 1건을 노트로 함께 등록한다
                            (공개/비공개는 creation_note_visibility 설정에 따름).
2. check_and_finalize_closed_cases() — 그 추적 테이블에서 아직 완료 확인이 안 된 케이스들을
                            대상으로 (a) Freshdesk 티켓이 아직 열려있는지, (b) Engage Center에서
                            실제로 완료됐는지 확인한다.
3. (2에 포함) 완료가 확인된 케이스는 Engage Center 커뮤니케이션의 가장 최신 메시지를 가져와
                            해당 Freshdesk 티켓에 노트로 등록한다(공개/비공개는 예약 설정에 따름).
"""
from __future__ import annotations

import html
import logging
from datetime import date

import crawler
import job_status
from config import AppConfig
from freshdesk_client import FreshdeskClient
from storage import TicketStore

logger = logging.getLogger("ticket_sync.freshdesk_case_sync")

# Freshdesk 표준 상태 코드: 2=Open, 3=Pending, 4=Resolved, 5=Closed.
_FRESHDESK_OPEN_STATUSES = (2, 3)


def _build_completion_note_html(thread: list[dict]) -> str:
    """완료 확인된 케이스의 커뮤니케이션 전체 스레드(crawler.get_full_communication_thread()가
    돌려주는, 오래된 것부터 정렬된 원문 리스트)를 노트 하나로 이어붙인다. 예전에는 가장 최신
    답장 자신의 새 내용 1건만 남겼는데, 그 답장에 인용된 이전 대화까지 스크롤해야 보이는
    내용이 실제로는 Freshdesk 노트에서 통째로 빠지는 문제가 있어서 전체 스레드를 그대로
    남기도록 바꿨다. 각 메시지 본문은 평문(줄바꿈만 있음)이라 escape 후 <br>로 바꿔야
    Freshdesk에서도 줄바꿈/공백이 그대로 유지된다."""
    parts = ["<p><b>완료 확인됨 — Engage Center 커뮤니케이션 전체</b></p>"]
    for m in thread:
        meta_bits = " · ".join(b for b in (m.get("source"), m.get("updated_at")) if b)
        if meta_bits:
            parts.append(f"<p style='margin-top:14px;'><b>{html.escape(meta_bits)}</b></p>")
        body_html = html.escape(m.get("body", "") or "").replace("\n", "<br>")
        parts.append(f"<div>{body_html}</div>")
    return "".join(parts)


def sync_new_cases(
    app_config: AppConfig,
    scope: str = "today",
    start_date: str | None = None,
    end_date: str | None = None,
    creation_note_visibility: str = "private",
) -> dict:
    """scope="today": 오늘 새로 생성된 오픈 케이스만 확인(예약 실행 기본).
    scope="all": 현재 열려있는 케이스 전체 확인.
    scope="range": start_date~end_date(생성일 기준) 사이 오픈 케이스만 확인.
    ("all"/"range" 는 대시보드의 수동 "전체 오픈 케이스 일괄 등록" 버튼용.)

    creation_note_visibility="public"이면 최초 문의 노트가 실제 요청자에게도 보이는 공개
    노트로 등록된다(이메일 알림이 갈 수 있음). 기본은 비공개."""
    mode = {"today": "today", "range": "range"}.get(scope, "all")
    tickets, warnings = crawler.collect_tickets(
        app_config, mode, start_date if mode == "range" else None,
        end_date if mode == "range" else None,
        case_status="open", date_basis="created", do_freshdesk=False,
    )

    store = TicketStore.from_config(app_config)
    client = FreshdeskClient(app_config.freshdesk)
    created = 0
    skipped_existing = 0
    failed = 0
    total = len(tickets)
    try:
        for idx, t in enumerate(tickets, start=1):
            if total:
                job_status.set_progress(
                    f"Freshdesk 중복 확인/등록 중 ({idx}/{total})", 70 + round(idx / total * 25)
                )
            case_id = (t.get("ms_case_id") or "").strip()
            if not case_id:
                continue

            if store.find_case_link(case_id):
                skipped_existing += 1
                continue

            existing_fd = client.find_ticket_by_case_tag(case_id)
            if existing_fd:
                # Freshdesk 엔 이미 있는데 우리 추적 테이블엔 없던 경우(이 기능 도입 전 생성분
                # 등) — 지금 연결해두면 다음부터는 추적 테이블 확인만으로 걸러진다.
                store.create_case_link(case_id, existing_fd["id"], t.get("created_at", ""))
                skipped_existing += 1
                continue

            result = client.create_new_case_ticket(t, app_config.priority_map)
            if result.success:
                store.create_case_link(case_id, result.ticket_id, t.get("created_at", ""))
                created += 1
                logger.info("신규 Freshdesk 티켓 생성됨 (case=%s, ticket=#%s)", case_id, result.ticket_id)

                # 커뮤니케이션 중 가장 오래된(=최초 문의) 1건을 노트로 함께 등록한다.
                # collect_tickets() 의 상세 보강 단계에서 이미 전체 대화를 수집해뒀으므로
                # (t["_communication_messages"], 오래된순 정렬) 여기서는 그 첫 항목만
                # 쓰면 되고, 브라우저를 다시 열어 커뮤니케이션 탭을 조회할 필요가 없다.
                oldest = (t.get("_communication_messages") or [None])[0]
                if oldest:
                    body_html = (
                        f"<p><b>신규 등록 — Engage Center 최초 문의</b> "
                        f"({oldest.get('sender_email', '')} · {oldest.get('sent', '')})</p>"
                        f"<div>{oldest.get('body_html', '')}</div>"
                    )
                    note_result = client.add_single_note(
                        result.ticket_id, body_html, private=(creation_note_visibility != "public"),
                    )
                    if not note_result.success:
                        logger.warning(
                            "신규 케이스 최초 문의 노트 등록 실패 (case=%s, ticket=#%s): %s",
                            case_id, result.ticket_id, note_result.error,
                        )
            else:
                failed += 1
                logger.warning("신규 케이스 Freshdesk 등록 실패 (case=%s): %s", case_id, result.error)
    finally:
        store.close()

    stats = {
        "scanned": len(tickets), "created": created,
        "skipped_existing": skipped_existing, "failed": failed, "warnings": warnings,
    }
    logger.info("신규 케이스 동기화 완료: %s", stats)
    return stats


def sync_closed_cases(
    app_config: AppConfig,
    scope: str = "all",
    start_date: str | None = None,
    end_date: str | None = None,
    creation_note_visibility: str = "private",
    completion_note_visibility: str = "private",
) -> dict:
    """대시보드의 수동 "완료된 케이스 일괄 등록" 버튼 전용 — sync_new_cases() 와 동일한
    방식(추적 테이블 → Freshdesk 태그 검색 순으로 중복 확인)으로 Engage Center에서 이미
    완료된(Closed) 케이스 중 Freshdesk에 아직 없는 것만 새로 등록한다.

    이미 완료된 케이스이므로 등록과 동시에 완료 처리까지 한 번에 끝낸다: 최초 문의(가장
    오래된 커뮤니케이션)를 등록 노트로 남기고, 마지막 업데이트(가장 최신 커뮤니케이션)를
    완료 노트로 바로 남긴 뒤, 추적 테이블에 이미 완료 확인이 끝난 상태로 기록해서 이후
    완료 감지 단계(check_and_finalize_closed_cases)가 다시 건드리지 않게 한다.

    scope="all": 완료된 케이스 전체. scope="range": start_date~end_date(생성일 기준)
    완료된 케이스만."""
    mode = "range" if scope == "range" else "all"
    tickets, warnings = crawler.collect_tickets(
        app_config, mode, start_date if mode == "range" else None,
        end_date if mode == "range" else None,
        case_status="closed", date_basis="created", do_freshdesk=False,
    )

    store = TicketStore.from_config(app_config)
    client = FreshdeskClient(app_config.freshdesk)
    created = 0
    skipped_existing = 0
    failed = 0
    total = len(tickets)
    try:
        for idx, t in enumerate(tickets, start=1):
            if total:
                job_status.set_progress(
                    f"Freshdesk 중복 확인/등록 중 ({idx}/{total})", 70 + round(idx / total * 25)
                )
            case_id = (t.get("ms_case_id") or "").strip()
            if not case_id:
                continue

            if store.find_case_link(case_id):
                skipped_existing += 1
                continue

            existing_fd = client.find_ticket_by_case_tag(case_id)
            if existing_fd:
                store.create_case_link(
                    case_id, existing_fd["id"], t.get("created_at", ""), status="note_posted",
                )
                skipped_existing += 1
                continue

            result = client.create_new_case_ticket(t, app_config.priority_map)
            if not result.success:
                failed += 1
                logger.warning("완료 케이스 Freshdesk 등록 실패 (case=%s): %s", case_id, result.error)
                continue

            store.create_case_link(
                case_id, result.ticket_id, t.get("created_at", ""), status="note_posted",
            )
            created += 1
            logger.info("완료 케이스 신규 Freshdesk 티켓 생성됨 (case=%s, ticket=#%s)", case_id, result.ticket_id)

            # collect_tickets() 의 상세 보강 단계에서 이미 전체 대화를 수집해뒀으므로
            # (t["_communication_messages"], 오래된순 정렬) 브라우저를 다시 열어 조회할
            # 필요 없이 첫/마지막 항목만 쓰면 된다.
            messages = t.get("_communication_messages") or []
            oldest = messages[0] if messages else None
            latest = messages[-1] if len(messages) >= 2 else None

            if oldest:
                body_html = (
                    f"<p><b>신규 등록 — Engage Center 최초 문의</b> "
                    f"({oldest.get('sender_email', '')} · {oldest.get('sent', '')})</p>"
                    f"<div>{oldest.get('body_html', '')}</div>"
                )
                note_result = client.add_single_note(
                    result.ticket_id, body_html, private=(creation_note_visibility != "public"),
                )
                if not note_result.success:
                    logger.warning(
                        "완료 케이스 최초 문의 노트 등록 실패 (case=%s, ticket=#%s): %s",
                        case_id, result.ticket_id, note_result.error,
                    )

            # 메시지가 1건뿐이면 위 최초 문의 노트가 곧 마지막 업데이트이기도 하므로
            # 같은 내용을 완료 노트로 중복 등록하지 않는다.
            if latest:
                body_html = (
                    f"<p><b>완료 확인됨 — Engage Center 마지막 업데이트</b> "
                    f"({latest.get('sender_email', '')} · {latest.get('sent', '')})</p>"
                    f"<div>{latest.get('body_html', '')}</div>"
                )
                note_result = client.add_single_note(
                    result.ticket_id, body_html, private=(completion_note_visibility != "public"),
                )
                if not note_result.success:
                    logger.warning(
                        "완료 케이스 최종 노트 등록 실패 (case=%s, ticket=#%s): %s",
                        case_id, result.ticket_id, note_result.error,
                    )
    finally:
        store.close()

    stats = {
        "scanned": len(tickets), "created": created,
        "skipped_existing": skipped_existing, "failed": failed, "warnings": warnings,
    }
    logger.info("완료 케이스 일괄 등록 완료: %s", stats)
    return stats


def check_and_finalize_closed_cases(app_config: AppConfig, note_visibility: str = "private") -> dict:
    """완료 감지 + 완료 케이스에 최종 대화 노트 등록. 후보가 없으면 브라우저조차 열지 않는다."""
    store = TicketStore.from_config(app_config)
    try:
        candidates = [dict(r) for r in store.get_awaiting_closure_links()]
    finally:
        store.close()

    today = date.today()
    candidates = [
        c for c in candidates
        if crawler._parse_date(c.get("engage_created_at") or "") != today
    ]

    stats = {"candidates": len(candidates), "closed_confirmed": 0, "notes_posted": 0, "failed": 0}
    if not candidates:
        logger.info("완료 감지 대상 케이스가 없어 건너뜁니다.")
        return stats

    job_status.set_progress("완료 여부 확인 중 (Freshdesk)", 10)
    client = FreshdeskClient(app_config.freshdesk)
    store = TicketStore.from_config(app_config)
    still_open = []
    try:
        for c in candidates:
            fd_ticket = client.get_ticket(c["freshdesk_ticket_id"])
            if fd_ticket is None:
                continue
            if fd_ticket.get("status") not in _FRESHDESK_OPEN_STATUSES:
                # 에이전트가 Freshdesk 에서 이미 직접 처리함 — 완료 감지 대상에서 제외.
                store.mark_case_link_closed_elsewhere(c["ms_case_id"])
                continue
            still_open.append(c)
    finally:
        store.close()

    if not still_open:
        logger.info("완료 감지 후보가 모두 Freshdesk 에서 이미 처리되어 종료합니다.")
        return stats

    pw = context = page = None
    store = TicketStore.from_config(app_config)
    try:
        job_status.set_progress("Engage Center 로그인 확인 중", 20)
        pw, context, page = crawler.open_browser_context(app_config)
        crawler.ensure_logged_in(page, app_config)

        job_status.set_progress("완료된 케이스 목록 확인 중", 30)
        closed_titles = crawler.list_case_ids_by_status(page, context, app_config, "closed")
        confirmed_closed = [c for c in still_open if c["ms_case_id"] in closed_titles]
        stats["closed_confirmed"] = len(confirmed_closed)

        note_total = len(confirmed_closed)
        for note_idx, c in enumerate(confirmed_closed, start=1):
            if note_total:
                job_status.set_progress(
                    f"완료 케이스 최종 노트 등록 중 ({note_idx}/{note_total})",
                    35 + round(note_idx / note_total * 60),
                )
            case_id = c["ms_case_id"]
            title = closed_titles.get(case_id, "")
            try:
                thread = crawler.get_full_communication_thread(page, app_config, title)
            except Exception as exc:
                stats["failed"] += 1
                logger.warning("완료 케이스 최종 대화 조회 실패 (case=%s): %s", case_id, exc, exc_info=True)
                continue

            if not thread:
                # 대화 내역이 없는 케이스 — 노트로 남길 내용이 없으니 그냥 종결 처리한다.
                store.mark_case_link_note_posted(case_id)
                continue

            body_html = _build_completion_note_html(thread)
            result = client.add_single_note(
                c["freshdesk_ticket_id"], body_html, private=(note_visibility != "public"),
            )
            if result.success:
                stats["notes_posted"] += 1
                store.mark_case_link_note_posted(case_id)
                logger.info("완료 케이스 최종 노트 등록됨 (case=%s, ticket=#%s)", case_id, c["freshdesk_ticket_id"])
            else:
                stats["failed"] += 1
                logger.warning("완료 케이스 최종 노트 등록 실패 (case=%s): %s", case_id, result.error)
    finally:
        store.close()
        try:
            if context:
                context.close()
        finally:
            if pw:
                pw.stop()

    logger.info("완료 감지/노트 등록 완료: %s", stats)
    return stats


def _filter_candidates_with_case_tag(fd_tickets: list[dict], stats: dict) -> list[tuple]:
    """Freshdesk 티켓 목록에서 아직 열려있고(Open/Pending) 케이스ID 태그(ms-case-<ID>)가
    붙어있는 것만 남긴다. 태그 없는 티켓, 이미 해결/종료된 티켓은 건수만 세고 건너뛴다."""
    candidates = []  # (freshdesk_ticket_id, ms_case_id)
    for t in fd_tickets:
        if t.get("status") not in _FRESHDESK_OPEN_STATUSES:
            stats["already_resolved"] += 1
            continue
        case_id = FreshdeskClient.extract_case_id_from_tags(t.get("tags"))
        if not case_id:
            stats["no_case_tag"] += 1
            continue
        candidates.append((t["id"], case_id))
    return candidates


def _check_and_close_candidates(
    app_config: AppConfig, client: FreshdeskClient, candidates: list[tuple],
    note_visibility: str, stats: dict,
) -> dict:
    """(freshdesk_ticket_id, ms_case_id) 후보 목록을 받아, Engage Center에서 완료됐는지
    확인하고 완료된 것만 마지막 대화를 노트로 등록한 뒤 Freshdesk 티켓을 종료 처리한다.
    check_closed_cases_by_freshdesk_date() 와 check_all_open_cases_for_completion() 이
    공유하는 본체 로직(대상을 어떻게 모으는지만 서로 다르다)."""
    if not candidates:
        logger.info("티켓 종료 처리: 대상 티켓이 없어 건너뜁니다. %s", stats)
        return stats

    pw = context = page = None
    try:
        job_status.set_progress("Engage Center 로그인 확인 중", 15)
        pw, context, page = crawler.open_browser_context(app_config)
        crawler.ensure_logged_in(page, app_config)

        # "열기" 목록에 없으면 완료된 것으로 본다 — 완료됨 목록을 먼저 조회할 필요가 없다.
        job_status.set_progress("열려있는 케이스 목록 확인 중", 25)
        open_case_ids = set(crawler.list_case_ids_by_status(page, context, app_config, "open").keys())
        closed_candidates = [(tid, cid) for tid, cid in candidates if cid not in open_case_ids]

        if not closed_candidates:
            logger.info("티켓 종료 처리: 열려있지 않은(=완료 추정) 케이스가 없습니다.")
            return stats

        job_status.set_progress("완료된 케이스 목록에서 제목 확인 중", 35)
        closed_titles = crawler.list_case_ids_by_status(page, context, app_config, "closed")
        stats["closed_confirmed"] = sum(1 for _, cid in closed_candidates if cid in closed_titles)

        note_total = len(closed_candidates)
        for idx, (ticket_id, case_id) in enumerate(closed_candidates, start=1):
            job_status.set_progress(
                f"완료 케이스 대화 노트 등록 중 ({idx}/{note_total})", 40 + round(idx / note_total * 55)
            )
            title = closed_titles.get(case_id)
            if not title:
                # 열기 목록엔 없지만 완료됨 목록에서도 못 찾은 경우 — 필터 반영 지연 등으로
                # 판단이 애매하니 안전하게 실패로 남기고 다음 조회 때 다시 확인되게 둔다.
                stats["failed"] += 1
                continue
            try:
                thread = crawler.get_full_communication_thread(page, app_config, title)
            except Exception as exc:
                stats["failed"] += 1
                logger.warning("완료 케이스 최종 대화 조회 실패 (case=%s): %s", case_id, exc, exc_info=True)
                continue
            if not thread:
                continue

            body_html = _build_completion_note_html(thread)
            result = client.add_single_note(ticket_id, body_html, private=(note_visibility != "public"))
            if not result.success:
                stats["failed"] += 1
                logger.warning("완료 케이스 노트 등록 실패 (case=%s): %s", case_id, result.error)
                continue

            stats["notes_posted"] += 1
            logger.info("완료 케이스 노트 등록됨 (case=%s, ticket=#%s)", case_id, ticket_id)

            close_result = client.close_ticket(ticket_id)
            if close_result.success:
                stats["tickets_closed"] += 1
                logger.info("티켓 종료 처리됨 (case=%s, ticket=#%s)", case_id, ticket_id)
            else:
                stats["failed"] += 1
                logger.warning("티켓 종료 처리 실패 (case=%s, ticket=#%s): %s", case_id, ticket_id, close_result.error)
    finally:
        try:
            if context:
                context.close()
        finally:
            if pw:
                pw.stop()

    logger.info("티켓 종료 처리 완료: %s", stats)
    return stats


def check_closed_cases_by_freshdesk_date(
    app_config: AppConfig, start_date: str, end_date: str, note_visibility: str = "private",
) -> dict:
    """"Freshdesk Management > 티켓 종료 처리 > 접수일 선택" 전용 — 로컬 tracking 테이블
    (freshdesk_case_links)과 무관하게, Freshdesk에 등록된 티켓 중 접수일이 start_date~
    end_date 사이인 것만 조회해서 Engage Center에서 완료됐는지 확인하고, 완료됐다면
    마지막 업데이트 대화를 노트로 등록한 뒤 Freshdesk 티켓 자체도 종료 처리한다.
    ticket_sync 자체 DB에는 아무것도 저장하지 않는 1회성 점검 기능이다."""
    stats = {
        "scanned": 0, "already_resolved": 0, "no_case_tag": 0,
        "closed_confirmed": 0, "notes_posted": 0, "tickets_closed": 0, "failed": 0,
    }

    job_status.set_progress("Freshdesk 티켓 조회 중", 5)
    client = FreshdeskClient(app_config.freshdesk)
    fd_tickets = client.list_tickets_created_between(start_date, end_date)
    stats["scanned"] = len(fd_tickets)

    candidates = _filter_candidates_with_case_tag(fd_tickets, stats)
    return _check_and_close_candidates(app_config, client, candidates, note_visibility, stats)


def check_all_open_cases_for_completion(app_config: AppConfig, note_visibility: str = "private") -> dict:
    """"Freshdesk Management > 티켓 종료 처리 > 열려있는 케이스 전체 확인" 전용 — 접수일과
    무관하게, 현재 Open/Pending 상태이고 케이스ID 태그가 있는 Freshdesk 티켓을 전부 확인
    대상으로 삼는다. 그 뒤 완료 확인/노트 등록/종료 처리는 check_closed_cases_by_freshdesk_date()
    와 완전히 동일한 로직(_check_and_close_candidates)을 공유한다."""
    stats = {
        "scanned": 0, "already_resolved": 0, "no_case_tag": 0,
        "closed_confirmed": 0, "notes_posted": 0, "tickets_closed": 0, "failed": 0,
    }

    job_status.set_progress("Freshdesk 열려있는 티켓 조회 중", 5)
    client = FreshdeskClient(app_config.freshdesk)
    fd_tickets = client.list_open_tickets()
    stats["scanned"] = len(fd_tickets)

    candidates = _filter_candidates_with_case_tag(fd_tickets, stats)
    return _check_and_close_candidates(app_config, client, candidates, note_visibility, stats)
