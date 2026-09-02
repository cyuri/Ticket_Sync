"""rqlite(https://rqlite.io)를 `sqlite3` 모듈과 최대한 비슷하게 쓰기 위한 최소 클라이언트.

AKS로 옮긴 웹 대시보드(파드)와 이 PC의 local_runner.py가 파일 하나(SQLite)를 직접 공유할
방법이 없어서, rqlite(SQLite 엔진 그대로 쓰면서 HTTP로 네트워크 접속을 지원하는 오픈소스)를
양쪽이 같이 보게 한다. `storage.py`의 기존 코드는 전부 `conn.execute(sql, params)`,
`conn.cursor()`, `row["col"]`, `cur.lastrowid`/`cur.rowcount`, `conn.commit()`,
`conn.executescript(...)`, `for row in conn.execute(...)` 같은 표준 `sqlite3` 관용구만 쓰고
있어서(트랜잭션을 여러 문장 걸쳐 묶어 쓰지 않음), 그 부분만 흉내내면 storage.py 쪽은 거의
그대로 재사용된다.

rqlite HTTP API 요약(공식 문서 기준):
- 쓰기: POST /db/execute, 바디 [[sql, param, ...], ...], 응답 results[i]에 last_insert_id/
  rows_affected.
- 읽기: POST /db/query, 바디 [[sql, param, ...], ...], 응답 results[i]에 columns/values.
- 실패해도 HTTP 200이고 결과 항목 안에 "error" 키로 온다 — 매번 확인해야 한다.
- 인증은 HTTP Basic Auth."""
from __future__ import annotations

import re
from typing import Any, Optional

import requests

_READ_ONLY_RE = re.compile(r"^\s*(SELECT|PRAGMA|EXPLAIN)\b", re.IGNORECASE)


class RqliteError(Exception):
    pass


class RqliteRow:
    """sqlite3.Row 를 흉내낸다: row["col"], dict(row), len(row), for v in row(값 순회)."""

    __slots__ = ("_columns", "_values")

    def __init__(self, columns: list[str], values: list[Any]):
        self._columns = columns
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._values[self._columns.index(key)]
        return self._values[key]

    def keys(self):
        return list(self._columns)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __repr__(self):
        return f"RqliteRow({dict(zip(self._columns, self._values))!r})"


class RqliteCursor:
    def __init__(self, conn: "RqliteConnection"):
        self._conn = conn
        self._rows: list[RqliteRow] = []
        self._pos = 0
        self.lastrowid: Optional[int] = None
        self.rowcount: int = -1

    def execute(self, sql: str, params: Any = ()) -> "RqliteCursor":
        params = list(params) if params else []
        statement = [sql, *params]
        is_read = bool(_READ_ONLY_RE.match(sql))
        endpoint = "/db/query" if is_read else "/db/execute"
        resp = self._conn._post(endpoint, [statement])
        results = resp.get("results") or []
        if not results:
            self._rows, self._pos = [], 0
            return self
        result = results[0]
        if result.get("error"):
            raise RqliteError(f"rqlite 쿼리 실패: {result['error']} (sql={sql!r})")

        columns = result.get("columns") or []
        values = result.get("values") or []
        self._rows = [RqliteRow(columns, row) for row in values]
        self._pos = 0
        self.lastrowid = result.get("last_insert_id")
        self.rowcount = result.get("rows_affected", -1)
        return self

    def fetchone(self) -> Optional[RqliteRow]:
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self) -> list[RqliteRow]:
        rows = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class RqliteConnection:
    """`sqlite3.Connection` 대체용. `.row_factory` 는 sqlite3.Row 로만 쓰이므로 별도 구현 없이
    무시하고 항상 RqliteRow 를 반환한다(속성만 받아주고 저장은 안 함 — storage.py 가
    `self._conn.row_factory = sqlite3.Row` 를 호출해도 에러 없이 지나가게)."""

    row_factory = None

    def __init__(self, base_url: str, auth: Optional[tuple[str, str]] = None, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self._auth = auth
        self._timeout = timeout
        self._session = requests.Session()

    def _post(self, path: str, body: list) -> dict:
        try:
            resp = self._session.post(
                f"{self.base_url}{path}", json=body, auth=self._auth, timeout=self._timeout,
                params={"level": "weak"},
            )
        except requests.RequestException as exc:
            raise RqliteError(f"rqlite 연결 실패({self.base_url}{path}): {exc}") from exc
        if resp.status_code != 200:
            raise RqliteError(f"rqlite HTTP 오류 {resp.status_code}: {resp.text}")
        return resp.json()

    def execute(self, sql: str, params: Any = ()) -> RqliteCursor:
        return self.cursor().execute(sql, params)

    def executescript(self, script: str):
        """SCHEMA 상수 적용 전용 — `;` 로 나눈 여러 statement를 순서대로 실행한다.
        SCHEMA 안 문자열 리터럴에는 세미콜론이 없으므로 단순 split 으로 충분하다.

        SCHEMA는 각 CREATE TABLE 바로 위에 그 테이블을 설명하는 여러 줄짜리 `-- 주석`을
        붙여둔다. 조각 맨 앞줄만 보고 "-- 로 시작하면 주석뿐인 조각"이라고 판단하면, 주석
        다음 줄에 있는 실제 CREATE TABLE 문까지 통째로 걸러져 그 테이블이 아예 안 만들어지는
        버그가 있었다(freshdesk_case_links 테이블이 실제로 생성되지 않아 바로 다음 statement인
        인덱스 생성이 "no such table"로 실패하는 것으로 확인됨). 그래서 맨 앞의 `--` 로
        시작하는 줄들만 하나씩 제거하고, 그 아래 남은 실제 SQL은 그대로 실행한다."""
        for raw in script.split(";"):
            lines = raw.strip("\n").split("\n")
            while lines and lines[0].strip().startswith("--"):
                lines.pop(0)
            statement = "\n".join(lines).strip()
            if not statement:
                continue
            self.execute(statement)

    def commit(self):
        """rqlite는 /db/execute 요청 하나하나가 즉시 커밋되므로 할 일이 없다(no-op).
        storage.py 가 매 쓰기마다 부르는 것과 궁합이 맞는다 — 여러 문장을 묶는 명시적
        트랜잭션은 이 프로젝트에서 쓰지 않는다."""
        pass

    def cursor(self) -> RqliteCursor:
        return RqliteCursor(self)

    def close(self):
        self._session.close()
