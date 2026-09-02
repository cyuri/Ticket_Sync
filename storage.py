"""SQLite 기반 티켓 저장소 + 중복 판단 + 동기화 이력 기록."""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ms_case_id TEXT,
    dedup_key TEXT,
    title TEXT,
    status TEXT,
    status_message TEXT,
    severity TEXT,
    created_at TEXT,
    modified_at TEXT,
    product TEXT,
    product_family TEXT,
    category TEXT,
    problem_type TEXT,
    requester TEXT,
    assignee TEXT,
    incident_manager TEXT,
    workspace TEXT,
    country_region TEXT,
    timezone TEXT,
    case_owner TEXT,
    contact_method TEXT,
    contract_id TEXT,
    case_type TEXT,
    closed_at TEXT,
    tenant_id TEXT,
    subscription_id TEXT,
    tenant TEXT,
    case_url TEXT,
    summary TEXT,
    communication_html_path TEXT,
    communication_messages TEXT,
    first_synced_at TEXT NOT NULL,
    last_synced_at TEXT NOT NULL,
    freshdesk_ticket_id INTEGER,
    freshdesk_status TEXT NOT NULL DEFAULT 'not_attempted',
    freshdesk_error TEXT,
    freshdesk_synced_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_ms_case_id
    ON tickets(ms_case_id) WHERE ms_case_id IS NOT NULL AND ms_case_id != '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_dedup_key
    ON tickets(dedup_key) WHERE dedup_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,
    mode TEXT,
    start_date TEXT,
    end_date TEXT,
    case_status TEXT,
    date_basis TEXT,
    freshdesk_env TEXT,
    total_collected INTEGER,
    new_count INTEGER,
    duplicate_count INTEGER,
    freshdesk_success INTEGER,
    freshdesk_failed INTEGER,
    error_count INTEGER,
    error_summary TEXT,
    excel_path TEXT,
    trigger TEXT
);

-- 예약 실행(매일 정해진 시각에 자동 실행) 설정. 사용자가 웹 UI에서 켜고 끄는 단일
-- 설정이라 행이 하나만 존재한다(id=1 고정).
CREATE TABLE IF NOT EXISTS schedule_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 0,
    run_time TEXT NOT NULL DEFAULT '09:00',
    freshdesk_env TEXT NOT NULL DEFAULT 'test',
    do_freshdesk INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'today',
    case_status TEXT NOT NULL DEFAULT 'open',
    date_basis TEXT NOT NULL DEFAULT 'created',
    completion_note_visibility TEXT NOT NULL DEFAULT 'private',
    creation_note_visibility TEXT NOT NULL DEFAULT 'private',
    updated_at TEXT,
    last_triggered_at TEXT
);

-- 예약 실행 전용 신규 알고리즘이 "이 Engage 케이스로 이미 Freshdesk 티켓을 만들었는지",
-- "완료 확인 후 마지막 대화 노트를 이미 남겼는지"를 추적하는 내부 전용 테이블. /cases
-- 화면(tickets 테이블)과는 완전히 분리되어 있어 웹 케이스 목록에는 노출되지 않는다.
CREATE TABLE IF NOT EXISTS freshdesk_case_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ms_case_id TEXT NOT NULL,
    freshdesk_ticket_id INTEGER NOT NULL,
    engage_created_at TEXT,
    status TEXT NOT NULL DEFAULT 'awaiting_closure',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_freshdesk_case_links_case_id
    ON freshdesk_case_links(ms_case_id);

-- (더 이상 쓰이지 않음 — freshdesk_connections 로 대체됐다. 이전 버전에서 저장된 값이
-- 있으면 첫 실행 시 freshdesk_connections 로 1회 옮겨진다. 과거 데이터 보존을 위해
-- 테이블 자체는 남겨둔다.)
CREATE TABLE IF NOT EXISTS freshdesk_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    domain TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '',
    default_group TEXT NOT NULL DEFAULT '',
    default_responder TEXT NOT NULL DEFAULT '',
    default_priority INTEGER NOT NULL DEFAULT 2,
    default_status INTEGER NOT NULL DEFAULT 2,
    custom_field_case_id TEXT NOT NULL DEFAULT '',
    custom_field_received_date TEXT NOT NULL DEFAULT '',
    updated_at TEXT
);

-- Freshdesk 자격증명을 이름 붙여 여러 개 저장하고(예: "Connection1"), 그중 하나만
-- is_active=1 로 지정해서 실제 등록/예약 실행/노트 추가 기능이 그 연결을 쓰게 한다.
-- prod/test 같은 고정된 구분이 아니라 사용자가 자유롭게 이름 붙여 관리한다.
CREATE TABLE IF NOT EXISTS freshdesk_connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '',
    default_group TEXT NOT NULL DEFAULT '',
    default_responder TEXT NOT NULL DEFAULT '',
    default_priority INTEGER NOT NULL DEFAULT 2,
    default_status INTEGER NOT NULL DEFAULT 2,
    custom_field_case_id TEXT NOT NULL DEFAULT '',
    custom_field_received_date TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def compute_dedup_key(title: str, created_at: str, requester: str) -> str:
    normalized = f"{(title or '').strip().lower()}|{(created_at or '').strip()}|{(requester or '').strip().lower()}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass
class DedupResult:
    is_new: bool
    ticket_row_id: Optional[int] = None


_TICKET_FIELDS = [
    "title", "status", "status_message", "severity", "created_at", "modified_at",
    "product", "product_family", "category", "problem_type", "requester", "assignee",
    "incident_manager", "workspace", "country_region", "timezone",
    "case_owner", "contact_method", "contract_id", "case_type", "closed_at",
    "tenant_id", "subscription_id", "tenant", "case_url", "summary",
    "communication_html_path", "communication_messages",
]

# config/column_map.json 이 CSV에서 채워주지 않는, 케이스 상세 페이지를 직접 열어야만
# 얻을 수 있는 필드들. modified_at(업데이트됨)이 지난 실행과 똑같다면 케이스 내용도
# 똑같다는 뜻이므로, 이 필드들은 상세 페이지를 다시 열지 않고 이전 값을 그대로 재사용해도
# 안전하다(크롤링 속도 최적화, crawler.collect_tickets 의 existing_tickets 인자로 쓰인다).
DETAIL_ENRICHMENT_FIELDS = [
    "category", "problem_type", "incident_manager", "country_region", "timezone",
    "case_owner", "contact_method", "case_url", "summary",
    "communication_html_path", "communication_messages",
]


class TicketStore:
    def __init__(
        self, db_path: Path, db_backend: str = "sqlite",
        rqlite_url: Optional[str] = None, rqlite_auth: Optional[tuple] = None,
    ):
        """db_backend="sqlite"(기본, 지금까지와 동일)면 db_path 의 로컬 파일을 그대로 쓴다.
        db_backend="rqlite"면 db_path 는 무시하고 rqlite_url(HTTP)로 접속한다 — 웹 앱과
        local_runner.py가 파일을 공유할 수 없는 배포(예: AKS)에서 같은 DB를 보기 위함."""
        self.db_path = Path(db_path)
        self.db_backend = db_backend
        if db_backend == "rqlite":
            from rqlite_client import RqliteConnection

            if not rqlite_url:
                raise ValueError("db_backend='rqlite' 인데 rqlite_url 이 없습니다.")
            self._conn = RqliteConnection(rqlite_url, auth=rqlite_auth)
        else:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate_add_columns()
        self._migrate_freshdesk_settings_to_connections()
        self._conn.commit()

    @classmethod
    def from_config(cls, app_config) -> "TicketStore":
        """AppConfig의 db_backend/rqlite_url/rqlite_auth 설정에 맞춰 알맞은 백엔드로 연다.
        일반적인 호출부는 TicketStore(app_config.db_path) 대신 이걸 쓴다."""
        return cls(
            app_config.db_path,
            db_backend=getattr(app_config, "db_backend", "sqlite"),
            rqlite_url=getattr(app_config, "rqlite_url", None),
            rqlite_auth=getattr(app_config, "rqlite_auth", None),
        )

    def _migrate_add_columns(self):
        """이전 버전에서 만들어진 DB에 새 필드 컬럼을 추가한다 (기존 데이터 보존)."""
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(tickets)")}
        for col in _TICKET_FIELDS:
            if col not in existing:
                self._conn.execute(f"ALTER TABLE tickets ADD COLUMN {col} TEXT")

        existing_runs = {row["name"] for row in self._conn.execute("PRAGMA table_info(sync_runs)")}
        for col in ("case_status", "date_basis", "trigger"):
            if col not in existing_runs:
                self._conn.execute(f"ALTER TABLE sync_runs ADD COLUMN {col} TEXT")

        existing_schedule = {row["name"] for row in self._conn.execute("PRAGMA table_info(schedule_config)")}
        if "completion_note_visibility" not in existing_schedule:
            self._conn.execute(
                "ALTER TABLE schedule_config ADD COLUMN completion_note_visibility TEXT NOT NULL DEFAULT 'private'"
            )
        if "creation_note_visibility" not in existing_schedule:
            self._conn.execute(
                "ALTER TABLE schedule_config ADD COLUMN creation_note_visibility TEXT NOT NULL DEFAULT 'private'"
            )

    def _migrate_freshdesk_settings_to_connections(self):
        """이전 버전(단일 설정 freshdesk_settings)에서 값을 저장해둔 적이 있으면, 딱 한 번
        freshdesk_connections 로 "Connection1"이라는 이름의 활성 연결로 옮겨온다.

        freshdesk_connections 가 비어있는지가 아니라 freshdesk_settings 행 자체가 아직
        남아있는지로 판단한다 — "비어있으면 마이그레이션"으로 하면, 사용자가 나중에
        연결을 전부 삭제했을 때(정상적인 사용법) 옛날 freshdesk_settings 데이터가 매번
        다시 살아나는 문제가 있었다. 마이그레이션 후에는 freshdesk_settings 행을 지워서
        다시는 실행되지 않게 한다."""
        row = self._conn.execute("SELECT * FROM freshdesk_settings WHERE id = 1").fetchone()
        if not row:
            return
        if (row["domain"] or "").strip():
            now_iso = datetime.now().isoformat(timespec="seconds")
            existing_count = self._conn.execute(
                "SELECT COUNT(*) AS c FROM freshdesk_connections"
            ).fetchone()["c"]
            self._conn.execute(
                """INSERT INTO freshdesk_connections
                    (name, domain, api_key, default_group, default_responder, default_priority,
                     default_status, custom_field_case_id, custom_field_received_date, is_active,
                     created_at, updated_at)
                   VALUES ('Connection1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["domain"], row["api_key"], row["default_group"], row["default_responder"],
                    row["default_priority"], row["default_status"], row["custom_field_case_id"],
                    row["custom_field_received_date"], 0 if existing_count > 0 else 1, now_iso, now_iso,
                ),
            )
        self._conn.execute("DELETE FROM freshdesk_settings WHERE id = 1")

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def list_tickets(self, status: str = "all", search: str = "") -> list[sqlite3.Row]:
        """케이스 보기 화면용: 상태(open/closed/all)와 검색어로 필터링해, 우리 시스템이
        가장 최근에 수집/갱신한 순서(last_synced_at DESC)로 반환한다. Engage Center 원본
        상태값은 'Open'/'Closed' 등 대소문자가 섞여 있어 LOWER() 로 비교한다. last_synced_at
        은 upsert_ticket() 이 항상 datetime.now().isoformat() 로 저장하는 ISO 문자열이라
        SQL 문자열 정렬 그대로 시간순이 된다(created_at/modified_at 은 Engage 원본 표기라
        이렇게 정렬 안 됨 — 그런 기준 정렬은 webapp.py 에서 파싱해서 따로 처리한다)."""
        cur = self._conn.cursor()
        query = "SELECT * FROM tickets WHERE 1=1"
        params: list = []
        if status == "open":
            query += " AND LOWER(status) = 'open'"
        elif status == "closed":
            query += " AND LOWER(status) NOT IN ('open', '')"
        if search:
            query += " AND (title LIKE ? OR ms_case_id LIKE ? OR requester LIKE ?)"
            like = f"%{search}%"
            params.extend([like, like, like])
        query += " ORDER BY last_synced_at DESC, id DESC"
        cur.execute(query, params)
        return cur.fetchall()

    def get_ticket_by_id(self, ticket_id: int) -> Optional[sqlite3.Row]:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        return cur.fetchone()

    def delete_ticket(self, ticket_id: int) -> bool:
        """케이스 보기 화면 목록에서만 지운다(로컬 DB 행 삭제). 실제 Freshdesk 티켓은
        건드리지 않는다 — 다음 수집에서 조건에 다시 걸리면 새 행으로 재수집될 수 있다."""
        cur = self._conn.cursor()
        cur.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def get_enrichment_cache(self) -> dict:
        """ms_case_id -> {"modified_at": ..., 상세 페이지 전용 필드...} 매핑을 반환한다.
        크롤링 시작 전에 호출해서 crawler.collect_tickets 에 넘기면, 지난 실행과
        modified_at 이 같은 케이스는 상세 페이지를 다시 열지 않고 여기 값을 재사용한다."""
        cur = self._conn.cursor()
        cols = ", ".join(["ms_case_id", "modified_at", *DETAIL_ENRICHMENT_FIELDS])
        cur.execute(f"SELECT {cols} FROM tickets WHERE ms_case_id IS NOT NULL AND ms_case_id != ''")
        return {row["ms_case_id"]: dict(row) for row in cur.fetchall()}

    def find_existing(self, ms_case_id: str, dedup_key: str) -> Optional[sqlite3.Row]:
        cur = self._conn.cursor()
        if ms_case_id:
            cur.execute("SELECT * FROM tickets WHERE ms_case_id = ?", (ms_case_id,))
            row = cur.fetchone()
            if row:
                return row
        if dedup_key:
            cur.execute("SELECT * FROM tickets WHERE dedup_key = ?", (dedup_key,))
            row = cur.fetchone()
            if row:
                return row
        return None

    def upsert_ticket(self, ticket: dict, now_iso: str, commit: bool = True) -> DedupResult:
        """ticket 딕셔너리를 저장. 이미 있으면 갱신만 하고 is_new=False 반환.
        commit=False 로 여러 번 호출하고 마지막에 commit() 을 한 번만 부르면, 티켓마다
        매번 디스크에 fsync 하는 대신 전체를 한 트랜잭션으로 묶어서 더 빠르다(대량 upsert 시)."""
        ms_case_id = (ticket.get("ms_case_id") or "").strip()
        dedup_key = None
        if not ms_case_id:
            dedup_key = compute_dedup_key(
                ticket.get("title", ""), ticket.get("created_at", ""), ticket.get("requester", "")
            )

        existing = self.find_existing(ms_case_id, dedup_key)
        cur = self._conn.cursor()
        values = [ticket.get(f) for f in _TICKET_FIELDS]

        if existing:
            set_clause = ", ".join(f"{f}=?" for f in _TICKET_FIELDS)
            cur.execute(
                f"UPDATE tickets SET {set_clause}, last_synced_at=? WHERE id=?",
                (*values, now_iso, existing["id"]),
            )
            if commit:
                self._conn.commit()
            return DedupResult(is_new=False, ticket_row_id=existing["id"])

        columns = ["ms_case_id", "dedup_key", *_TICKET_FIELDS, "first_synced_at", "last_synced_at", "freshdesk_status"]
        insert_values = [ms_case_id or None, dedup_key, *values, now_iso, now_iso, "not_attempted"]
        placeholders = ", ".join(["?"] * len(columns))
        cur.execute(
            f"INSERT INTO tickets ({', '.join(columns)}) VALUES ({placeholders})",
            insert_values,
        )
        if commit:
            self._conn.commit()
        return DedupResult(is_new=True, ticket_row_id=cur.lastrowid)

    def commit(self):
        self._conn.commit()

    def mark_freshdesk_result(
        self, ticket_row_id: int, status: str, freshdesk_ticket_id: Optional[int],
        error: Optional[str], now_iso: str,
    ):
        self._conn.execute(
            """UPDATE tickets SET freshdesk_status=?, freshdesk_ticket_id=?,
               freshdesk_error=?, freshdesk_synced_at=? WHERE id=?""",
            (status, freshdesk_ticket_id, error, now_iso, ticket_row_id),
        )
        self._conn.commit()

    def get_pending_or_failed_freshdesk(self) -> list[sqlite3.Row]:
        """이전 실행에서 Freshdesk 등록에 실패했거나 아직 시도 안 한 티켓 (다음 실행 때 재처리).

        예전 코드는 실제 쿼리에서 'not_attempted'(새 티켓 저장 시 기본값)를 빼먹어서, "이번
        실행에서는 이미 있던(중복) 티켓이라 신규 등록 대상에는 안 들어갔지만, 예전에 한 번도
        Freshdesk 등록을 시도한 적 없는" 티켓들이 do_freshdesk=True 로 다시 실행해도 영원히
        재시도 대상에 안 들어가는 버그가 있었다(실제로 "실행 이력엔 중복으로만 잡히고 FD
        성공/실패가 둘 다 0인데, Freshdesk에는 등록이 안 되어 있다"는 사례로 확인됨)."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM tickets WHERE freshdesk_status IN ('failed', 'pending', 'not_attempted') ORDER BY id"
        )
        return cur.fetchall()

    def get_recent_runs(
        self, limit: int = 50, start_date: Optional[str] = None, end_date: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        """실행 이력 대시보드용: 최근 실행 기록을 최신순으로 반환한다. start_date/end_date
        ('YYYY-MM-DD')를 주면 run_at(ISO 문자열) 날짜 부분만 뽑아서 그 범위로 좁힌다."""
        cur = self._conn.cursor()
        query = "SELECT * FROM sync_runs WHERE 1=1"
        params: list = []
        if start_date:
            query += " AND date(run_at) >= date(?)"
            params.append(start_date)
        if end_date:
            query += " AND date(run_at) <= date(?)"
            params.append(end_date)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        cur.execute(query, params)
        return cur.fetchall()

    def get_run(self, run_id: int) -> Optional[sqlite3.Row]:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM sync_runs WHERE id = ?", (run_id,))
        return cur.fetchone()

    def get_run_count(self) -> int:
        cur = self._conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM sync_runs")
        return cur.fetchone()["n"]

    def record_sync_run(self, run: dict):
        self._conn.execute(
            """INSERT INTO sync_runs
                (run_at, mode, start_date, end_date, case_status, date_basis, freshdesk_env,
                 total_collected, new_count, duplicate_count, freshdesk_success, freshdesk_failed,
                 error_count, error_summary, excel_path, trigger)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run.get("run_at"), run.get("mode"), run.get("start_date"), run.get("end_date"),
                run.get("case_status"), run.get("date_basis"),
                run.get("freshdesk_env"), run.get("total_collected"), run.get("new_count"),
                run.get("duplicate_count"), run.get("freshdesk_success"), run.get("freshdesk_failed"),
                run.get("error_count"), run.get("error_summary"), run.get("excel_path"),
                run.get("trigger", "manual"),
            ),
        )
        self._conn.commit()

    def get_schedule(self) -> dict:
        """예약 실행 설정을 읽는다. 아직 한 번도 저장 안 했으면 기본값을 반환한다(끔)."""
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM schedule_config WHERE id = 1")
        row = cur.fetchone()
        if row:
            return dict(row)
        return {
            "id": 1, "enabled": 0, "run_time": "09:00", "freshdesk_env": "prod",
            "do_freshdesk": 0, "mode": "today", "case_status": "open", "date_basis": "created",
            "completion_note_visibility": "private", "creation_note_visibility": "private",
            "updated_at": None, "last_triggered_at": None,
        }

    def save_schedule(self, schedule: dict):
        now_iso = datetime.now().isoformat(timespec="seconds")
        self._conn.execute(
            """INSERT INTO schedule_config
                (id, enabled, run_time, freshdesk_env, do_freshdesk, mode, case_status, date_basis,
                 completion_note_visibility, creation_note_visibility, updated_at)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 enabled=excluded.enabled, run_time=excluded.run_time,
                 freshdesk_env=excluded.freshdesk_env, do_freshdesk=excluded.do_freshdesk,
                 mode=excluded.mode, case_status=excluded.case_status, date_basis=excluded.date_basis,
                 completion_note_visibility=excluded.completion_note_visibility,
                 creation_note_visibility=excluded.creation_note_visibility,
                 updated_at=excluded.updated_at""",
            (
                int(schedule.get("enabled", 0)), schedule.get("run_time", "09:00"),
                schedule.get("freshdesk_env", "test"), int(schedule.get("do_freshdesk", 0)),
                schedule.get("mode", "today"), schedule.get("case_status", "open"),
                schedule.get("date_basis", "created"),
                schedule.get("completion_note_visibility", "private"),
                schedule.get("creation_note_visibility", "private"), now_iso,
            ),
        )
        self._conn.commit()

    def mark_schedule_triggered(self):
        self._conn.execute(
            "UPDATE schedule_config SET last_triggered_at = ? WHERE id = 1",
            (datetime.now().isoformat(timespec="seconds"),),
        )
        self._conn.commit()

    # ---------------- Freshdesk 자격증명(이름 붙인 여러 연결 중 하나를 활성으로) ---------------- #

    @staticmethod
    def _freshdesk_connection_fields(data: dict) -> tuple:
        return (
            (data.get("name") or "").strip() or "이름 없는 연결",
            (data.get("domain") or "").strip(),
            (data.get("api_key") or "").strip(),
            (data.get("default_group") or "").strip(),
            (data.get("default_responder") or "").strip(),
            int(data.get("default_priority") or 2),
            int(data.get("default_status") or 2),
            (data.get("custom_field_case_id") or "").strip(),
            (data.get("custom_field_received_date") or "").strip(),
        )

    def list_freshdesk_connections(self) -> list[sqlite3.Row]:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM freshdesk_connections ORDER BY id")
        return cur.fetchall()

    def get_freshdesk_connection(self, conn_id: int) -> Optional[sqlite3.Row]:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM freshdesk_connections WHERE id = ?", (conn_id,))
        return cur.fetchone()

    def get_active_freshdesk_connection(self) -> Optional[dict]:
        """등록/예약 실행/노트 추가 기능이 실제로 쓸 연결. 활성으로 지정된 게 없으면 None —
        호출부(config.py)가 "Freshdesk Management > 설정에서 먼저 연결을 활성화하세요" 같은
        안내를 낸다."""
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM freshdesk_connections WHERE is_active = 1 LIMIT 1")
        row = cur.fetchone()
        return dict(row) if row else None

    def create_freshdesk_connection(self, data: dict) -> int:
        """새 연결을 저장한다. 아직 활성 연결이 하나도 없으면(첫 연결이면) 자동으로 활성으로
        지정한다 — 매번 따로 "활성으로 지정"을 누르지 않아도 바로 쓸 수 있게."""
        now_iso = datetime.now().isoformat(timespec="seconds")
        has_active = self._conn.execute(
            "SELECT COUNT(*) AS c FROM freshdesk_connections WHERE is_active = 1"
        ).fetchone()["c"] > 0
        fields = self._freshdesk_connection_fields(data)
        cur = self._conn.execute(
            """INSERT INTO freshdesk_connections
                (name, domain, api_key, default_group, default_responder, default_priority,
                 default_status, custom_field_case_id, custom_field_received_date, is_active,
                 created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (*fields, 0 if has_active else 1, now_iso, now_iso),
        )
        self._conn.commit()
        return cur.lastrowid

    def update_freshdesk_connection(self, conn_id: int, data: dict):
        now_iso = datetime.now().isoformat(timespec="seconds")
        fields = self._freshdesk_connection_fields(data)
        self._conn.execute(
            """UPDATE freshdesk_connections SET
                 name=?, domain=?, api_key=?, default_group=?, default_responder=?,
                 default_priority=?, default_status=?, custom_field_case_id=?,
                 custom_field_received_date=?, updated_at=?
               WHERE id=?""",
            (*fields, now_iso, conn_id),
        )
        self._conn.commit()

    def delete_freshdesk_connection(self, conn_id: int):
        self._conn.execute("DELETE FROM freshdesk_connections WHERE id = ?", (conn_id,))
        self._conn.commit()

    def set_active_freshdesk_connection(self, conn_id: int):
        """다른 연결들의 활성 표시는 모두 끄고, 이 연결만 활성으로 지정한다(항상 최대
        하나만 활성)."""
        now_iso = datetime.now().isoformat(timespec="seconds")
        self._conn.execute("UPDATE freshdesk_connections SET is_active = 0, updated_at = ?", (now_iso,))
        self._conn.execute(
            "UPDATE freshdesk_connections SET is_active = 1, updated_at = ? WHERE id = ?",
            (now_iso, conn_id),
        )
        self._conn.commit()

    # ---------------- 예약 실행 전용 Freshdesk 신규 알고리즘: 케이스 연결 추적 ----------------

    def find_case_link(self, ms_case_id: str) -> Optional[sqlite3.Row]:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM freshdesk_case_links WHERE ms_case_id = ?", (ms_case_id,))
        return cur.fetchone()

    def create_case_link(
        self, ms_case_id: str, freshdesk_ticket_id: int, engage_created_at: str,
        status: str = "awaiting_closure",
    ):
        """status="note_posted" 로 만들면 처음부터 완료 확인이 끝난 것으로 기록한다 — 이미
        완료 상태인 케이스를 일괄 등록할 때(등록과 동시에 최종 노트까지 남길 때) 쓴다.
        이렇게 하면 이후 완료 감지 단계(get_awaiting_closure_links)가 다시 건드리지 않는다."""
        now_iso = datetime.now().isoformat(timespec="seconds")
        self._conn.execute(
            """INSERT INTO freshdesk_case_links
                (ms_case_id, freshdesk_ticket_id, engage_created_at, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(ms_case_id) DO UPDATE SET
                 freshdesk_ticket_id=excluded.freshdesk_ticket_id,
                 engage_created_at=excluded.engage_created_at,
                 status=excluded.status,
                 updated_at=excluded.updated_at""",
            (ms_case_id, freshdesk_ticket_id, engage_created_at, status, now_iso, now_iso),
        )
        self._conn.commit()

    def get_awaiting_closure_links(self) -> list[sqlite3.Row]:
        """아직 완료 확인/노트 등록이 안 끝난 링크를 전부 반환한다. "오늘 생성된 건 제외" 조건은
        engage_created_at 표기가 CSV 원본 그대로라 SQL 문자열 비교로는 신뢰할 수 없어서,
        호출 측(freshdesk_case_sync.py)이 crawler._parse_date 로 파싱해서 판단한다."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM freshdesk_case_links WHERE status = 'awaiting_closure' ORDER BY id"
        )
        return cur.fetchall()

    def mark_case_link_note_posted(self, ms_case_id: str):
        self._conn.execute(
            "UPDATE freshdesk_case_links SET status = 'note_posted', updated_at = ? WHERE ms_case_id = ?",
            (datetime.now().isoformat(timespec="seconds"), ms_case_id),
        )
        self._conn.commit()

    def mark_case_link_closed_elsewhere(self, ms_case_id: str):
        """Freshdesk 쪽에서 이미 open 이 아닌 것으로 확인된 경우(에이전트가 직접 처리 등) —
        더 이상 이 케이스를 완료 감지 대상으로 보지 않는다."""
        self._conn.execute(
            "UPDATE freshdesk_case_links SET status = 'closed_elsewhere', updated_at = ? WHERE ms_case_id = ?",
            (datetime.now().isoformat(timespec="seconds"), ms_case_id),
        )
        self._conn.commit()
