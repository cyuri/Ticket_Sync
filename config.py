"""환경 변수(.env) + config.json 을 읽어 하나의 AppConfig 객체로 합쳐준다.

우선순위: CLI 인자 > config.json > .env / 기본값
민감 정보(API Key, 비밀번호)는 반드시 .env 또는 OS 환경 변수로만 받는다.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# PyInstaller(--onefile)로 패키징되면 __file__ 은 임시 압축 해제 폴더를 가리키므로,
# .exe 와 같은 폴더에 있는 config.json/.env/config/output/logs/data 를 읽도록
# 실행 파일 기준 경로를 사용한다. 일반 파이썬 실행(python main.py)일 때는 스크립트 폴더를 쓴다.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

_ca_bundle_ensured = False


def _ensure_corporate_ca_bundle():
    """KT 사내망은 TLS 검사(프록시가 실제 인증서를 자체 서명 인증서로 바꿔치기)를 하기
    때문에, 사내망 PC에서 직접 실행되는 local_runner.py 가 requests 로 외부 HTTPS
    (Freshdesk API 등)를 호출하면 인증서 검증에 실패한다(SSLCertVerificationError:
    self-signed certificate in certificate chain). Docker 이미지는 빌드 시 이 인증서를
    시스템 신뢰 저장소에 이미 추가해뒀지만, 호스트에서 직접 도는 local_runner.py 는 그
    조치가 안 되어 있어 실제로 이 오류가 났다.

    certifi 의 기본 공개 CA 목록 뒤에 KT 사내 루트 인증서(docker/certs/kt-corp-ca-bundle.crt,
    앞서 Docker 이미지에도 쓴 것과 같은 파일)를 이어붙인 합본 파일을 한 번만 만들어두고,
    REQUESTS_CA_BUNDLE 로 지정해 requests 가 공개 CA와 사내 CA를 모두 신뢰하게 한다."""
    global _ca_bundle_ensured
    if _ca_bundle_ensured or os.environ.get("REQUESTS_CA_BUNDLE"):
        return
    _ca_bundle_ensured = True

    kt_bundle = BASE_DIR / "docker" / "certs" / "kt-corp-ca-bundle.crt"
    if not kt_bundle.exists():
        return

    try:
        import certifi

        combined_path = BASE_DIR / "data" / "combined-ca-bundle.pem"
        if not combined_path.exists() or combined_path.stat().st_mtime < kt_bundle.stat().st_mtime:
            combined_path.parent.mkdir(parents=True, exist_ok=True)
            base_bundle = Path(certifi.where()).read_text(encoding="utf-8")
            kt_pem = kt_bundle.read_text(encoding="utf-8")
            combined_path.write_text(base_bundle + "\n" + kt_pem, encoding="utf-8")
    except Exception:
        return

    os.environ["REQUESTS_CA_BUNDLE"] = str(combined_path)
    os.environ["SSL_CERT_FILE"] = str(combined_path)


def _ensure_bundled_playwright_browsers():
    """exe와 같은 폴더에 ms-playwright(Chromium 등) 폴더가 있으면 Playwright가 그걸 쓰도록
    PLAYWRIGHT_BROWSERS_PATH 를 지정한다. 배포용 실행 파일(TicketSyncDesktop.exe 등)에
    브라우저를 통째로 넣어서 옮기면, 대상 PC에 Python/Playwright가 전혀 없어도 별도 설치
    없이 바로 동작하게 하기 위함이다(실제로 다른 PC에서 "Playwright 브라우저를 설치하세요"
    오류를 겪어서 추가함). 폴더가 없으면(기존처럼 이 PC에 이미 설치된 브라우저를 그대로
    쓰는 경우) 아무것도 안 하고 넘어간다."""
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return
    bundled = BASE_DIR / "ms-playwright"
    if bundled.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled)


@dataclass
class MSAccountConfig:
    username: str
    password: str
    login_url: str
    case_management_url: str
    browser_profile_dir: Path


@dataclass
class FreshdeskEnvConfig:
    name: str
    label: str
    domain: str
    api_key: str
    default_group: Optional[int]
    default_responder: Optional[int]
    default_priority: int
    default_status: int
    custom_field_case_id: str
    custom_field_received_date: str


@dataclass
class CrawlerConfig:
    headless: bool
    enable_detail_page_enrichment: bool
    download_timeout_ms: int
    navigation_timeout_ms: int


@dataclass
class AppConfig:
    ms_account: MSAccountConfig
    freshdesk: FreshdeskEnvConfig
    crawler: CrawlerConfig
    priority_map: dict
    excel_dir: Path
    log_dir: Path
    db_path: Path
    selectors: dict
    column_map: dict
    db_backend: str = "sqlite"
    rqlite_url: Optional[str] = None
    rqlite_auth: Optional[tuple] = None


def _to_optional_int(value) -> Optional[int]:
    if value in (None, "", 0, "0"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"설정 파일을 찾을 수 없습니다: {path}\n"
            f"config.example.json 을 복사해서 config.json 을 만들어주세요."
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_freshdesk_from_db(
    db_path: Path, require_credentials: bool = True,
    db_backend: str = "sqlite", rqlite_url: Optional[str] = None,
    rqlite_auth: Optional[tuple] = None,
) -> FreshdeskEnvConfig:
    """Freshdesk 자격증명은 더 이상 .env/config.json 이 아니라, 웹 UI의 "Freshdesk
    Management > 설정" 화면에서 이름 붙여 저장한 여러 연결 중 "활성"으로 지정된 연결을
    DB에서 읽는다(예전 _resolve_freshdesk_env 의 prod/test/custom 3분기 로직을 대체).

    AppConfig가 아직 만들어지기 전(이 함수 자체가 AppConfig를 만드는 과정의 일부)이라
    db_backend/rqlite_url/rqlite_auth 를 직접 받는다 — TicketStore.from_config() 를
    쓸 수 없는 유일한 자리다."""
    from storage import TicketStore

    store = TicketStore(db_path, db_backend=db_backend, rqlite_url=rqlite_url, rqlite_auth=rqlite_auth)
    try:
        conn = store.get_active_freshdesk_connection()
    finally:
        store.close()

    conn = conn or {}
    domain = conn.get("domain") or ""
    api_key = conn.get("api_key") or ""
    if require_credentials and (not domain or not api_key):
        raise ValueError(
            "활성화된 Freshdesk 연결이 없습니다. Freshdesk Management > 설정 화면에서 연결을 "
            "추가하고 활성으로 지정하세요."
        )

    return FreshdeskEnvConfig(
        name=conn.get("name") or "default",
        label=conn.get("name") or "Freshdesk",
        domain=domain,
        api_key=api_key,
        default_group=_to_optional_int(conn.get("default_group")),
        default_responder=_to_optional_int(conn.get("default_responder")),
        default_priority=int(conn.get("default_priority") or 2),
        default_status=int(conn.get("default_status") or 2),
        custom_field_case_id=conn.get("custom_field_case_id") or "",
        custom_field_received_date=conn.get("custom_field_received_date") or "",
    )


def load_config(
    config_path: Optional[str] = None,
    env_path: Optional[str] = None,
    freshdesk_choice: str = "prod",
    cli_overrides: Optional[dict] = None,
    require_freshdesk_credentials: bool = True,
) -> AppConfig:
    """freshdesk_choice/cli_overrides 는 이전 prod/test/custom 다중 환경 시절의 매개변수다.
    지금은 Freshdesk 자격증명이 DB의 단일 설정 하나뿐이라 실제로는 쓰이지 않지만, 호출부를
    전부 고치지 않아도 되도록 시그니처만 유지한다."""
    load_dotenv(dotenv_path=env_path or (BASE_DIR / ".env"))
    _ensure_corporate_ca_bundle()
    _ensure_bundled_playwright_browsers()

    cfg_path = Path(config_path) if config_path else BASE_DIR / "config.json"
    cfg = _load_json(cfg_path)

    selectors_path = BASE_DIR / "config" / "selectors.json"
    column_map_path = BASE_DIR / "config" / "column_map.json"
    selectors = _load_json(selectors_path)
    column_map = _load_json(column_map_path)
    column_map = {k: v for k, v in column_map.items() if not k.startswith("_")}

    ms_account = MSAccountConfig(
        username=os.getenv("MS_USERNAME", ""),
        password=os.getenv("MS_PASSWORD", ""),
        login_url=os.getenv("MS_LOGIN_URL", "https://engage.microsoft.com/"),
        case_management_url=os.getenv(
            "MS_CASE_MANAGEMENT_URL", "https://engage.microsoft.com/Support/CaseManagement"
        ),
        browser_profile_dir=Path(os.getenv("MS_BROWSER_PROFILE_DIR", "./data/browser_profile")).resolve(),
    )

    output_cfg = cfg.get("output", {})
    db_path = (BASE_DIR / output_cfg.get("db_path", "./data/tickets.db")).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # 기본값은 지금까지와 동일한 로컬 SQLite 파일 — 이 env들을 안 건드리면 아무 영향 없다.
    # AKS처럼 웹 앱과 local_runner.py가 파일을 공유할 수 없는 배포에서만 rqlite로 바꾼다.
    db_backend = os.getenv("TICKET_SYNC_DB_BACKEND", "sqlite")
    rqlite_url = os.getenv("TICKET_SYNC_RQLITE_URL")
    rqlite_user = os.getenv("TICKET_SYNC_RQLITE_USER")
    rqlite_password = os.getenv("TICKET_SYNC_RQLITE_PASSWORD")
    rqlite_auth = (rqlite_user, rqlite_password) if rqlite_user else None

    freshdesk = _load_freshdesk_from_db(
        db_path, require_credentials=require_freshdesk_credentials,
        db_backend=db_backend, rqlite_url=rqlite_url, rqlite_auth=rqlite_auth,
    )

    crawler_cfg = cfg.get("crawler", {})
    # 컨테이너(K8s) 환경은 화면이 없어 항상 headless=true 여야 하는데, 마운트된
    # config.json 을 매번 고치지 않고 배포 시 환경 변수로만 강제할 수 있게 한다.
    headless_env = os.getenv("TICKET_SYNC_HEADLESS")
    headless = headless_env.lower() == "true" if headless_env is not None else bool(crawler_cfg.get("headless", False))
    crawler = CrawlerConfig(
        headless=headless,
        enable_detail_page_enrichment=bool(crawler_cfg.get("enable_detail_page_enrichment", True)),
        download_timeout_ms=int(crawler_cfg.get("download_timeout_seconds", 60)) * 1000,
        navigation_timeout_ms=int(crawler_cfg.get("navigation_timeout_seconds", 30)) * 1000,
    )

    excel_dir = (BASE_DIR / output_cfg.get("excel_dir", "./output")).resolve()
    log_dir = (BASE_DIR / output_cfg.get("log_dir", "./logs")).resolve()

    for d in (excel_dir, log_dir, db_path.parent, ms_account.browser_profile_dir):
        d.mkdir(parents=True, exist_ok=True)

    return AppConfig(
        ms_account=ms_account,
        freshdesk=freshdesk,
        crawler=crawler,
        priority_map=cfg.get("freshdesk_priority_map", {}),
        excel_dir=excel_dir,
        log_dir=log_dir,
        db_path=db_path,
        selectors=selectors,
        column_map=column_map,
        db_backend=db_backend,
        rqlite_url=rqlite_url,
        rqlite_auth=rqlite_auth,
    )
