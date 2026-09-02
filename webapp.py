"""웹 UI 실행 진입점.

CLI 대화형 마법사(main.run_wizard) 대신, 브라우저에서 조건을 입력하고 실행 버튼을 누르면
main.run_sync() 를 그대로 호출하는 웹 페이지다. 실행 이력은 storage.py 의 sync_runs 테이블을
그대로 읽어 대시보드에 보여준다.

로컬 실행:
    uvicorn webapp:app --host 0.0.0.0 --port 8000

Playwright(브라우저 자동화)는 동기(sync) API 라서 FastAPI 의 이벤트 루프 스레드에서 직접
호출할 수 없다. 그래서 동기화 작업은 항상 별도 스레드에서 실행하고, 한 번에 하나만 실행되도록
잠금(lock)으로 막는다(브라우저 프로필/DB 파일을 여러 실행이 동시에 건드리면 안 되기 때문).
"""
from __future__ import annotations

import html
import json
import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlencode

import requests
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config as config_module
import job_status
import scheduler as scheduler_module
import sync_lock
from crawler import _parse_date, filter_by_date_range
from freshdesk_client import FreshdeskClient, FreshdeskResult
from logging_setup import setup_logging
from main import run_sync
from storage import TicketStore

logger = logging.getLogger("ticket_sync.webapp")

# templates/static 는 빌드 스크립트(build_webapp_exe.bat)가 --add-data 로 exe 안에 그대로
# 번들링한다 — PyInstaller(--onefile)는 이런 번들 데이터를 실행 시점에 sys._MEIPASS 임시
# 폴더로 풀어놓으므로(exe 자신의 폴더가 아님), 그 경로를 써야 한다. config.py 의 BASE_DIR
# (exe와 같은 폴더 — config.json 처럼 사용자가 수정해야 하는 "외부" 파일용)과는 다른
# 용도라는 점에 주의.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
else:
    BASE_DIR = Path(__file__).resolve().parent

# Engage Center 로그인은 사내망/VPN 연결된 PC에서만 가능해서, 컨테이너 안에서는 실제
# 로그인이 안 된다. EXEC_MODE=remote 이면 실행 요청을 컨테이너 밖의 로컬 PC에서 띄운
# local_runner.py 에게 HTTP로 위임하고, 이 웹 앱 자신은 실행을 하지 않는다(대시보드/이력
# 조회만 담당). 컨테이너 없이 로컬에서 webapp.py 를 직접 띄우는 경우는 기존처럼 inprocess.
EXEC_MODE = os.getenv("TICKET_SYNC_EXEC_MODE", "inprocess")
LOCAL_RUNNER_URL = os.getenv("TICKET_SYNC_LOCAL_RUNNER_URL", "http://host.docker.internal:8787")
# local_runner.py 가 TICKET_SYNC_RUNNER_TOKEN 을 요구하도록 설정된 배포(예: Cloudflare
# Tunnel로 외부에 노출한 AKS 배포)에서, 같은 토큰을 실어 보내야 실행 요청이 통과한다.
RUNNER_TOKEN = os.getenv("TICKET_SYNC_RUNNER_TOKEN", "")

# Freshdesk 연결 대상(테스트/운영)은 화면에서 고르지 않고 항상 운영으로 고정한다.
FRESHDESK_ENV = "prod"

app = FastAPI(title="Ticket Sync")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.on_event("startup")
def _start_scheduler():
    scheduler_module.start(_app_config())


def _run_job(params: dict):
    run_date_str = datetime.now().strftime("%Y%m%d")
    try:
        app_config = config_module.load_config(
            freshdesk_choice=params["freshdesk_env"],
            require_freshdesk_credentials=params["do_freshdesk"],
        )
        setup_logging(app_config.log_dir, run_date_str)
        logger.info("웹 UI에서 동기화 실행 요청: %s", params)
        run_sync(
            app_config,
            params["mode"],
            params["start_date"],
            params["end_date"],
            params["case_status"],
            params["date_basis"],
            params["do_freshdesk"],
            run_date_str,
            owner="web-ui",
            skip_dedup=params.get("skip_dedup", False),
            save_excel=params.get("save_excel", False),
        )
        job_status.finish()
    except Exception as exc:  # noqa: BLE001 - 실행 이력 화면에 원인을 보여주기 위해 광범위하게 잡는다
        logger.exception("웹 UI 동기화 실행 중 오류")
        job_status.finish(error=str(exc))


def _app_config():
    # 폼 입력과 무관하게 db 위치/백엔드만 필요할 때는 자격 증명 없이 config 를 읽는다.
    return config_module.load_config(require_freshdesk_credentials=False)


def _db_path() -> Path:
    return _app_config().db_path


def _open_store() -> TicketStore:
    """db_backend(sqlite/rqlite)에 맞춰 TicketStore를 연다."""
    return TicketStore.from_config(_app_config())


def _runner_headers() -> dict:
    """local_runner.py 호출 시 실을 인증 헤더. TICKET_SYNC_RUNNER_TOKEN 을 설정 안 했으면
    (같은 PC 안에서만 쓰는 지금까지의 기본 사용법) 빈 딕셔너리라 아무 영향이 없다."""
    if not RUNNER_TOKEN:
        return {}
    return {"Authorization": f"Bearer {RUNNER_TOKEN}"}


def _current_status() -> dict:
    """실행 상태 스냅샷. EXEC_MODE=remote 면 컨테이너 자신이 아니라 로컬 실행 에이전트
    (local_runner.py)가 실제로 실행 중이므로, 그쪽에 물어봐서 상태를 가져온다."""
    if EXEC_MODE == "remote":
        try:
            resp = requests.get(f"{LOCAL_RUNNER_URL}/healthz", headers=_runner_headers(), timeout=2)
            data = resp.json()
        except requests.RequestException:
            return {
                "running": False,
                "params": None,
                "elapsed_seconds": None,
                "last_error": None,
                "offline": True,
            }
        return {
            "running": data.get("running", False),
            "params": data.get("params"),
            "elapsed_seconds": data.get("elapsed_seconds"),
            "last_error": data.get("last_error"),
            "step": data.get("step"),
            "percent": data.get("percent"),
            "last_result": data.get("last_result"),
            "offline": False,
        }
    return {**job_status.snapshot(), "offline": False}


def _base_ctx(active_nav: str) -> dict:
    """모든 페이지가 공통으로 쓰는 상단 실행 상태(대기중/실행중)와 현재 탭 표시.
    job_status 는 웹 UI(수동 실행)와 scheduler.py(예약 실행) 가 공유하는 상태라,
    둘 중 어느 쪽이 실행 중이어도 여기 정확히 반영된다."""
    snap = _current_status()
    return {
        "active_nav": active_nav,
        "running": snap["running"],
        "running_params": snap["params"],
        "elapsed_seconds": snap["elapsed_seconds"],
        "local_runner_offline": snap["offline"],
        "running_step": snap.get("step"),
        "running_percent": snap.get("percent"),
        "last_result": snap.get("last_result"),
        "last_error": snap.get("last_error"),
    }


def _build_stats(runs: list[dict], total_run_count: int) -> dict:
    if not runs:
        return {
            "total_runs": total_run_count,
            "success_rate": None,
            "last_run_at": None,
            "recent_new_total": 0,
            "recent_fd_success_total": 0,
            "sparkline": [],
        }
    ok_count = sum(1 for r in runs if not r.get("error_count"))

    # 하루에 실행이 여러 번(수동+예약 등) 있으면 막대 간격이 실제 날짜 간격과 안 맞아
    # 보이므로, 실행 단위가 아니라 날짜 단위로 합쳐서 하루에 막대 하나만 그린다.
    # 수집 건수와 오류 건수는 크기 단위가 서로 많이 달라서(오류가 보통 훨씬 적음) 한
    # 막대의 색으로만 표시하면 오류 건수 자체가 안 보이므로, 각자 자기 최댓값 기준으로
    # 따로 막대를 그린다(수집 막대 + 오류 막대, 하루에 두 개).
    by_day: dict[str, dict] = {}
    for r in runs:
        day = (r.get("run_at") or "")[:10]
        if not day:
            continue
        bucket = by_day.setdefault(day, {"total_collected": 0, "error_count": 0})
        bucket["total_collected"] += r.get("total_collected") or 0
        bucket["error_count"] += r.get("error_count") or 0

    days_sorted = sorted(by_day.keys())[-14:]  # 오래된 -> 최신 순, 최근 14일
    max_collected = max((by_day[d]["total_collected"] for d in days_sorted), default=0) or 1
    max_errors = max((by_day[d]["error_count"] for d in days_sorted), default=0) or 1
    n = len(days_sorted)
    # 막대마다 날짜를 다 찍으면 겹쳐서 안 보이므로, 막대 수에 따라 몇 개마다 하나씩만
    # 라벨을 보여준다(항상 처음/마지막은 보여줌).
    label_step = 1 if n <= 7 else (2 if n <= 10 else 3)
    sparkline = []
    for i, day in enumerate(days_sorted):
        b = by_day[day]
        sparkline.append({
            "run_at": day,
            "date_label": day[5:10].replace("-", "/") if len(day) >= 10 else "",
            "show_label": i == 0 or i == n - 1 or i % label_step == 0,
            "total_collected": b["total_collected"],
            "height_pct": round(b["total_collected"] / max_collected * 100),
            "error_count": b["error_count"],
            "error_height_pct": round(b["error_count"] / max_errors * 100) if b["error_count"] else 0,
        })

    return {
        "total_runs": total_run_count,
        "success_rate": round(ok_count / len(runs) * 100),
        "last_run_at": runs[0].get("run_at"),
        "recent_new_total": sum(r.get("new_count") or 0 for r in runs),
        "recent_fd_success_total": sum(r.get("freshdesk_success") or 0 for r in runs),
        "sparkline": sparkline,
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, start_date: str = "", end_date: str = "", reset: str = ""):
    today = datetime.now().strftime("%Y-%m-%d")

    if reset:
        start_date = end_date = today
    elif not start_date and not end_date:
        # 쿼리 파라미터 없이 들어온 경우(예: 사이드바로 다른 페이지 갔다가 대시보드로 복귀) —
        # 지난번에 고른 기간을 쿠키에서 復元한다. 쿠키조차 없으면(최초 방문) 기본값은 오늘 하루치.
        cookie_val = request.cookies.get("ts_dash_range", "")
        if "," in cookie_val:
            start_date, end_date = cookie_val.split(",", 1)
        else:
            start_date = end_date = today

    store = _open_store()
    try:
        runs = [dict(r) for r in store.get_recent_runs(50, start_date or None, end_date or None)]
        total_run_count = store.get_run_count()
    finally:
        store.close()
    response = templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            **_base_ctx("dashboard"),
            "runs": runs,
            "stats": _build_stats(runs, total_run_count),
            "today": today,
            "filter_start_date": start_date,
            "filter_end_date": end_date,
        },
    )
    if reset:
        response.delete_cookie("ts_dash_range")
    else:
        response.set_cookie("ts_dash_range", f"{start_date},{end_date}", max_age=60 * 60 * 24 * 30)
    return response


@app.post("/sync")
def start_sync(
    do_freshdesk: Optional[str] = Form(None),
    mode: str = Form("all"),
    start_date: Optional[str] = Form(None),
    end_date: Optional[str] = Form(None),
    date_basis: str = Form("created"),
    case_status: str = Form("open"),
    test_mode: Optional[str] = Form(None),
    save_excel: Optional[str] = Form(None),
):
    if EXEC_MODE == "remote":
        # 이 컨테이너는 실행을 직접 하지 않는다(사내망/VPN이 없어 로그인이 안 됨) —
        # 로컬 PC에서 띄운 local_runner.py 에게 그대로 위임한다.
        try:
            resp = requests.post(
                f"{LOCAL_RUNNER_URL}/run",
                data={
                    "freshdesk_env": FRESHDESK_ENV,
                    "do_freshdesk": do_freshdesk or "",
                    "mode": mode,
                    "start_date": start_date or "",
                    "end_date": end_date or "",
                    "date_basis": date_basis,
                    "case_status": case_status,
                    "test_mode": test_mode or "",
                    "save_excel": save_excel or "",
                    "owner": "web-ui",
                },
                headers=_runner_headers(),
                timeout=5,
            )
            body = resp.json()
        except requests.RequestException:
            return RedirectResponse("/?error=local_runner_unreachable", status_code=303)
        if not body.get("ok"):
            return RedirectResponse("/?error=already_running", status_code=303)
        return RedirectResponse("/?started=1", status_code=303)

    # 이 프로세스 안에서 이미 실행 중인지(수동 실행이든 예약 실행이든)는 job_status 로 막고,
    # K8s 에서 완전히 다른 파드(CronJob)가 실행 중인지는 sync_lock 파일로 미리 확인해
    # 즉시 피드백을 준다(최종 방어선은 run_sync 안의 파일 잠금).
    other_owner = sync_lock.current_owner(_db_path().parent)
    if other_owner:
        return RedirectResponse(f"/?error=already_running_by_{other_owner}", status_code=303)

    params = {
        "freshdesk_env": FRESHDESK_ENV,
        "do_freshdesk": do_freshdesk == "on",
        "mode": mode,
        "start_date": start_date if mode == "range" else None,
        "end_date": (end_date or datetime.now().strftime("%Y-%m-%d")) if mode == "range" else None,
        "date_basis": date_basis,
        "case_status": case_status,
        "skip_dedup": test_mode == "on",
        "save_excel": save_excel == "on",
    }
    if not job_status.try_start(params):
        return RedirectResponse("/?error=already_running", status_code=303)

    thread = threading.Thread(target=_run_job, args=(params,), daemon=True)
    thread.start()

    return RedirectResponse("/?started=1", status_code=303)


@app.get("/freshdesk/sync", response_class=HTMLResponse)
def freshdesk_sync_page(request: Request, started: str = "", error: str = ""):
    return templates.TemplateResponse(
        request,
        "freshdesk_sync.html",
        {
            **_base_ctx("freshdesk_sync"),
            "today": datetime.now().strftime("%Y-%m-%d"),
            "started": started,
            "error": error,
        },
    )


@app.get("/freshdesk/settings", response_class=HTMLResponse)
def freshdesk_settings_page(request: Request, saved: str = "", deleted: str = "", activated: str = ""):
    store = _open_store()
    try:
        connections = [dict(r) for r in store.list_freshdesk_connections()]
    finally:
        store.close()
    return templates.TemplateResponse(
        request,
        "freshdesk_settings.html",
        {
            **_base_ctx("freshdesk_settings"),
            "connections": connections,
            "saved": saved,
            "deleted": deleted,
            "activated": activated,
        },
    )


def _freshdesk_connection_form_to_dict(
    name, domain, api_key, default_group, default_responder,
    default_priority, default_status, custom_field_case_id, custom_field_received_date,
) -> dict:
    return {
        "name": name,
        "domain": domain,
        "api_key": api_key,
        "default_group": default_group,
        "default_responder": default_responder,
        "default_priority": default_priority or "2",
        "default_status": default_status or "2",
        "custom_field_case_id": custom_field_case_id,
        "custom_field_received_date": custom_field_received_date,
    }


@app.post("/freshdesk/settings/create")
def create_freshdesk_connection(
    name: str = Form(""),
    domain: str = Form(""),
    api_key: str = Form(""),
    default_group: str = Form(""),
    default_responder: str = Form(""),
    default_priority: str = Form("2"),
    default_status: str = Form("2"),
    custom_field_case_id: str = Form(""),
    custom_field_received_date: str = Form(""),
):
    """새 Freshdesk 연결을 이름 붙여 저장한다. 저장 즉시 목록 화면으로 돌아가므로(폼은
    다시 빈 채로 접혀 보인다), 이 연결이 처음 만드는 연결이면 자동으로 활성이 된다."""
    data = _freshdesk_connection_form_to_dict(
        name, domain, api_key, default_group, default_responder,
        default_priority, default_status, custom_field_case_id, custom_field_received_date,
    )
    store = _open_store()
    try:
        store.create_freshdesk_connection(data)
    finally:
        store.close()
    return RedirectResponse("/freshdesk/settings?saved=1", status_code=303)


@app.post("/freshdesk/settings/{conn_id}/update")
def update_freshdesk_connection(
    conn_id: int,
    name: str = Form(""),
    domain: str = Form(""),
    api_key: str = Form(""),
    default_group: str = Form(""),
    default_responder: str = Form(""),
    default_priority: str = Form("2"),
    default_status: str = Form("2"),
    custom_field_case_id: str = Form(""),
    custom_field_received_date: str = Form(""),
):
    data = _freshdesk_connection_form_to_dict(
        name, domain, api_key, default_group, default_responder,
        default_priority, default_status, custom_field_case_id, custom_field_received_date,
    )
    store = _open_store()
    try:
        store.update_freshdesk_connection(conn_id, data)
    finally:
        store.close()
    return RedirectResponse("/freshdesk/settings?saved=1", status_code=303)


@app.post("/freshdesk/settings/{conn_id}/delete")
def delete_freshdesk_connection(conn_id: int):
    store = _open_store()
    try:
        store.delete_freshdesk_connection(conn_id)
    finally:
        store.close()
    return RedirectResponse("/freshdesk/settings?deleted=1", status_code=303)


@app.post("/freshdesk/settings/{conn_id}/activate")
def activate_freshdesk_connection(conn_id: int):
    store = _open_store()
    try:
        store.set_active_freshdesk_connection(conn_id)
    finally:
        store.close()
    return RedirectResponse("/freshdesk/settings?activated=1", status_code=303)


@app.post("/freshdesk/closed-note-sweep")
def closed_note_sweep(
    scope: str = Form("date"),
    date: str = Form(""),
    note_visibility: str = Form("private"),
):
    """"티켓 종료 처리" — scope="date" 면 접수일이 이 날짜인 Freshdesk 티켓만, scope=
    "all_open" 이면 접수일과 무관하게 현재 열려있는(케이스ID 태그 있는) Freshdesk 티켓
    전체를 대상으로 Engage Center 완료 여부를 확인한다. 완료됐으면 마지막 대화를 노트로
    남긴 뒤 Freshdesk 티켓도 종료 처리한다. 로컬 tickets/추적 테이블에는 아무것도 저장하지
    않는 1회성 점검이라(freshdesk_case_sync.py), 결과는 job_status 의 last_result 로만
    보여준다."""
    scope = scope if scope in ("date", "all_open") else "date"
    note_visibility = note_visibility if note_visibility in ("public", "private") else "private"
    if scope == "date" and not date:
        return RedirectResponse("/freshdesk/sync?error=missing_date", status_code=303)

    if EXEC_MODE == "remote":
        try:
            resp = requests.post(
                f"{LOCAL_RUNNER_URL}/run-closed-note-sweep",
                data={
                    "scope": scope,
                    "date": date,
                    "note_visibility": note_visibility,
                    "owner": "web-ui-freshdesk",
                },
                headers=_runner_headers(),
                timeout=5,
            )
            body = resp.json()
        except requests.RequestException:
            return RedirectResponse("/freshdesk/sync?error=local_runner_unreachable", status_code=303)
        if not body.get("ok"):
            return RedirectResponse("/freshdesk/sync?error=already_running", status_code=303)
        return RedirectResponse("/freshdesk/sync?started=1", status_code=303)

    if not job_status.try_start({"kind": "closed_note_sweep", "scope": scope, "date": date}):
        return RedirectResponse("/freshdesk/sync?error=already_running", status_code=303)

    def _job():
        try:
            import freshdesk_case_sync

            app_config = config_module.load_config(require_freshdesk_credentials=True)
            if scope == "all_open":
                stats = freshdesk_case_sync.check_all_open_cases_for_completion(
                    app_config, note_visibility=note_visibility,
                )
            else:
                stats = freshdesk_case_sync.check_closed_cases_by_freshdesk_date(
                    app_config, start_date=date, end_date=date, note_visibility=note_visibility,
                )
            job_status.finish(result=stats)
        except Exception as exc:  # noqa: BLE001
            logger.exception("티켓 종료 처리 중 오류")
            job_status.finish(error=str(exc))

    threading.Thread(target=_job, daemon=True).start()
    return RedirectResponse("/freshdesk/sync?started=1", status_code=303)


@app.post("/freshdesk/bulk-register-open")
def bulk_register_open_cases(
    scope: str = Form("all"),
    start_date: Optional[str] = Form(None),
    end_date: Optional[str] = Form(None),
    creation_note_visibility: str = Form("private"),
):
    """새 Freshdesk 동기화 알고리즘(freshdesk_case_sync.py)의 1단계만, "오늘 생성분"이
    아니라 현재 열려있는 케이스(전체 또는 지정한 기간)를 대상으로 수동으로 한 번 돌린다 —
    이 기능 도입 전부터 있던 케이스들을 한 번에 따라잡기 위함. 완료 감지(2/3단계)는 하지
    않는다 — 그건 예약 실행이 매일 자동으로 담당한다."""
    scope = scope if scope in ("all", "range") else "all"
    creation_note_visibility = creation_note_visibility if creation_note_visibility in ("public", "private") else "private"

    if EXEC_MODE == "remote":
        try:
            resp = requests.post(
                f"{LOCAL_RUNNER_URL}/run-case-sync",
                data={
                    "scope": scope,
                    "start_date": start_date or "",
                    "end_date": end_date or "",
                    "creation_note_visibility": creation_note_visibility,
                    "run_closure_check": "",
                    "owner": "web-ui-bulk",
                },
                headers=_runner_headers(),
                timeout=5,
            )
            body = resp.json()
        except requests.RequestException:
            return RedirectResponse("/freshdesk/sync?error=local_runner_unreachable", status_code=303)
        if not body.get("ok"):
            return RedirectResponse("/freshdesk/sync?error=already_running", status_code=303)
        return RedirectResponse("/freshdesk/sync?started=1", status_code=303)

    if not job_status.try_start({"kind": "bulk_register_open"}):
        return RedirectResponse("/freshdesk/sync?error=already_running", status_code=303)

    def _job():
        try:
            import freshdesk_case_sync

            app_config = config_module.load_config(
                freshdesk_choice=FRESHDESK_ENV, require_freshdesk_credentials=True,
            )
            freshdesk_case_sync.sync_new_cases(
                app_config, scope=scope, start_date=start_date, end_date=end_date,
                creation_note_visibility=creation_note_visibility,
            )
            job_status.finish()
        except Exception as exc:  # noqa: BLE001
            logger.exception("전체 오픈 케이스 일괄 등록 중 오류")
            job_status.finish(error=str(exc))

    threading.Thread(target=_job, daemon=True).start()
    return RedirectResponse("/freshdesk/sync?started=1", status_code=303)


@app.post("/freshdesk/bulk-register-closed")
def bulk_register_closed_cases(
    scope: str = Form("all"),
    start_date: Optional[str] = Form(None),
    end_date: Optional[str] = Form(None),
    creation_note_visibility: str = Form("private"),
    completion_note_visibility: str = Form("private"),
):
    """이미 완료(Closed)된 케이스 중 Freshdesk에 아직 없는 것만 새로 등록한다 — 오픈 케이스
    일괄 등록과 대상만 다를 뿐 같은 방식(추적 테이블/태그로 중복 확인)이다. 이미 완료된
    케이스이므로 등록과 동시에 최초 문의/최종 업데이트 노트를 한 번에 남기고 완료 처리까지
    끝낸다(freshdesk_case_sync.sync_closed_cases 참고)."""
    scope = scope if scope in ("all", "range") else "all"
    creation_note_visibility = creation_note_visibility if creation_note_visibility in ("public", "private") else "private"
    completion_note_visibility = completion_note_visibility if completion_note_visibility in ("public", "private") else "private"

    if EXEC_MODE == "remote":
        try:
            resp = requests.post(
                f"{LOCAL_RUNNER_URL}/run-case-sync",
                data={
                    "scope": scope,
                    "start_date": start_date or "",
                    "end_date": end_date or "",
                    "creation_note_visibility": creation_note_visibility,
                    "completion_note_visibility": completion_note_visibility,
                    "case_kind": "closed",
                    "run_closure_check": "",
                    "owner": "web-ui-bulk",
                },
                headers=_runner_headers(),
                timeout=5,
            )
            body = resp.json()
        except requests.RequestException:
            return RedirectResponse("/freshdesk/sync?error=local_runner_unreachable", status_code=303)
        if not body.get("ok"):
            return RedirectResponse("/freshdesk/sync?error=already_running", status_code=303)
        return RedirectResponse("/freshdesk/sync?started=1", status_code=303)

    if not job_status.try_start({"kind": "bulk_register_closed"}):
        return RedirectResponse("/freshdesk/sync?error=already_running", status_code=303)

    def _job():
        try:
            import freshdesk_case_sync

            app_config = config_module.load_config(
                freshdesk_choice=FRESHDESK_ENV, require_freshdesk_credentials=True,
            )
            freshdesk_case_sync.sync_closed_cases(
                app_config, scope=scope, start_date=start_date, end_date=end_date,
                creation_note_visibility=creation_note_visibility,
                completion_note_visibility=completion_note_visibility,
            )
            job_status.finish()
        except Exception as exc:  # noqa: BLE001
            logger.exception("완료된 케이스 일괄 등록 중 오류")
            job_status.finish(error=str(exc))

    threading.Thread(target=_job, daemon=True).start()
    return RedirectResponse("/freshdesk/sync?started=1", status_code=303)


@app.get("/download/{run_id}")
def download_excel(run_id: int):
    store = _open_store()
    try:
        run = store.get_run(run_id)
    finally:
        store.close()
    if run is None or not run["excel_path"]:
        return HTMLResponse("해당 실행의 Excel 파일을 찾을 수 없습니다.", status_code=404)
    path = Path(run["excel_path"])
    if not path.exists():
        return HTMLResponse(f"Excel 파일이 디스크에 없습니다: {path}", status_code=404)
    return FileResponse(path, filename=path.name)


def _initials(email: Optional[str]) -> str:
    if not email:
        return "?"
    local = email.split("@")[0]
    return (local[:2] or "?").upper()


def _is_microsoft_side(email: Optional[str]) -> bool:
    if not email:
        return False
    return "microsoft.com" in email.lower()


def _sort_tickets(tickets: list[dict], sort: str) -> list[dict]:
    """"recent"(기본)는 이미 storage.list_tickets() 가 last_synced_at DESC 로 정렬해서
    주므로 그대로 둔다. 나머지 기준(생성일/업데이트일)은 Engage 원본 날짜 표기라 SQL로는
    시간순 정렬이 안 돼서 여기서 파싱해 직접 정렬한다. 날짜를 못 읽은 케이스는 정렬 방향과
    무관하게 항상 맨 뒤로 보낸다."""
    if sort not in ("created_desc", "created_asc", "updated_desc", "updated_asc", "title_asc"):
        return tickets

    if sort == "title_asc":
        return sorted(tickets, key=lambda t: (t.get("title") or "").lower())

    field = "modified_at" if sort.startswith("updated") else "created_at"
    descending = sort.endswith("_desc")
    dated, undated = [], []
    for t in tickets:
        d = _parse_date(t.get(field) or "")
        (dated if d else undated).append((d, t))
    dated.sort(key=lambda pair: pair[0], reverse=descending)
    return [t for _, t in dated] + [t for _, t in undated]


@app.get("/cases", response_class=HTMLResponse)
def cases_list(
    request: Request,
    status: str = "open",
    q: str = "",
    date_basis: str = "created",
    start_date: str = "",
    end_date: str = "",
    sort: str = "recent",
    fd_success: Optional[int] = None,
    fd_failed: Optional[int] = None,
    deleted: Optional[int] = None,
):
    store = _open_store()
    try:
        # last_synced_at DESC(우리 시스템이 가장 최근에 수집/갱신한 순서)로 이미 정렬돼서 온다.
        tickets = [dict(r) for r in store.list_tickets(status=status, search=q.strip())]
    finally:
        store.close()

    if start_date or end_date:
        date_field = "modified_at" if date_basis == "updated" else "created_at"
        tickets = filter_by_date_range(tickets, "range", start_date or None, end_date or None, date_field=date_field)

    tickets = _sort_tickets(tickets, sort)

    # 케이스 상세로 들어갔다가 "목록으로" 눌렀을 때 지금 이 필터(검색어/기간/정렬 등)가
    # 그대로 유지되도록, 케이스 상세 화면에 그대로 넘겨줄 쿼리스트링을 만들어둔다.
    back_qs = urlencode({
        "status": status, "q": q, "date_basis": date_basis,
        "start_date": start_date, "end_date": end_date, "sort": sort,
    })

    return templates.TemplateResponse(
        request,
        "cases_list.html",
        {
            **_base_ctx("cases"),
            "tickets": tickets,
            "status": status,
            "q": q,
            "date_basis": date_basis,
            "start_date": start_date,
            "end_date": end_date,
            "sort": sort,
            "back_qs": back_qs,
            "fd_success": fd_success,
            "fd_failed": fd_failed,
            "deleted": deleted,
        },
    )


@app.post("/cases/register-freshdesk")
def register_freshdesk_selected(
    ticket_ids: Optional[List[int]] = Form(None),
    status: str = Form("open"),
    q: str = Form(""),
    date_basis: str = Form("created"),
    start_date: str = Form(""),
    end_date: str = Form(""),
    sort: str = Form("recent"),
):
    """케이스 목록 화면에서 사람이 직접 고른 케이스만 Freshdesk에 등록한다(자동 실행과 별개).
    Freshdesk는 공개 SaaS라 사내망/VPN 없이도 컨테이너 안에서 바로 호출 가능하다(Engage
    Center 로그인과 달리 local_runner.py 로 위임할 필요가 없다)."""
    if not ticket_ids:
        redirect_qs = f"status={status}&q={q}&date_basis={date_basis}&start_date={start_date}&end_date={end_date}&sort={sort}"
        return RedirectResponse(f"/cases?{redirect_qs}", status_code=303)

    app_config = config_module.load_config(freshdesk_choice=FRESHDESK_ENV, require_freshdesk_credentials=True)
    client = FreshdeskClient(app_config.freshdesk)
    store = _open_store()
    success = 0
    failed = 0
    try:
        for ticket_id in ticket_ids:
            row = store.get_ticket_by_id(ticket_id)
            if row is None:
                continue
            t = dict(row)
            try:
                fd_result = client.create_ticket(t, app_config.priority_map)
            except Exception as exc:
                logger.warning("Freshdesk 수동 등록 중 오류 (ticket_id=%s): %s", ticket_id, exc, exc_info=True)
                fd_result = FreshdeskResult(success=False, error=str(exc))
            sync_ts = datetime.now().isoformat(timespec="seconds")
            fd_status = "success" if fd_result.success else "failed"
            store.mark_freshdesk_result(ticket_id, fd_status, fd_result.ticket_id, fd_result.error, sync_ts)
            if fd_result.success:
                success += 1
                if t.get("communication_messages") and fd_result.ticket_id:
                    try:
                        messages = json.loads(t["communication_messages"])
                        client.add_conversation_notes(fd_result.ticket_id, messages)
                    except Exception:
                        logger.warning("대화 노트 등록 실패 (ticket_id=%s)", ticket_id, exc_info=True)
            else:
                failed += 1
    finally:
        store.close()

    redirect_qs = (
        f"status={status}&q={q}&date_basis={date_basis}&start_date={start_date}&end_date={end_date}"
        f"&sort={sort}&fd_success={success}&fd_failed={failed}"
    )
    return RedirectResponse(f"/cases?{redirect_qs}", status_code=303)


@app.post("/cases/delete-selected")
def delete_selected_cases(
    ticket_ids: Optional[List[int]] = Form(None),
    status: str = Form("open"),
    q: str = Form(""),
    date_basis: str = Form("created"),
    start_date: str = Form(""),
    end_date: str = Form(""),
    sort: str = Form("recent"),
):
    """케이스 보기 화면 목록에서만 지운다 — 실제 Freshdesk 티켓은 그대로 남아있고,
    우리 쪽 케이스 목록/DB에서만 제거한다(다음 수집 조건에 다시 걸리면 재수집될 수 있음)."""
    deleted = 0
    if ticket_ids:
        store = _open_store()
        try:
            for ticket_id in ticket_ids:
                if store.delete_ticket(ticket_id):
                    deleted += 1
        finally:
            store.close()

    redirect_qs = (
        f"status={status}&q={q}&date_basis={date_basis}&start_date={start_date}&end_date={end_date}"
        f"&sort={sort}&deleted={deleted}"
    )
    return RedirectResponse(f"/cases?{redirect_qs}", status_code=303)


@app.get("/cases/{ticket_id}", response_class=HTMLResponse)
def case_detail(request: Request, ticket_id: int, back: str = "status=open"):
    """back: 케이스 목록에서 넘어온 필터(검색어/기간 등)를 그대로 담은 쿼리스트링 —
    "목록으로" 링크에 그대로 돌려줘서 검색/기간 조건이 풀리지 않게 한다."""
    store = _open_store()
    try:
        row = store.get_ticket_by_id(ticket_id)
    finally:
        store.close()
    if row is None:
        return HTMLResponse("케이스를 찾을 수 없습니다.", status_code=404)

    ticket = dict(row)
    messages = []
    if ticket.get("communication_messages"):
        try:
            raw = json.loads(ticket["communication_messages"])
            for m in raw:
                is_me = not _is_microsoft_side(m.get("sender_email"))
                messages.append({**m, "is_me": is_me, "initials": _initials(m.get("sender_email"))})
        except Exception:
            logger.warning("케이스 %s 의 communication_messages 파싱 실패", ticket.get("ms_case_id"), exc_info=True)

    # Jinja 의 |e|replace 체이닝은 Markup 객체에 replace 를 걸면 삽입하려는 "<br>" 자체가
    # 다시 이스케이프되는 문제가 있어(&lt;br&gt;로 보임), 파이썬에서 먼저 안전하게 만든다.
    summary_html = html.escape(ticket.get("summary") or "").replace("\n", "<br>")

    return templates.TemplateResponse(
        request,
        "case_detail.html",
        {
            **_base_ctx("cases"),
            "ticket": ticket,
            "messages": messages,
            "back_qs": back,
            "summary_html": summary_html,
        },
    )


@app.get("/freshdesk/schedule", response_class=HTMLResponse)
def schedule_page(request: Request):
    store = _open_store()
    try:
        schedule = store.get_schedule()
    finally:
        store.close()
    return templates.TemplateResponse(
        request,
        "schedule.html",
        {
            **_base_ctx("freshdesk_schedule"),
            "schedule": schedule,
            "next_run": scheduler_module.next_run_time(),
        },
    )


@app.post("/freshdesk/schedule")
def save_schedule(
    enabled: Optional[str] = Form(None),
    run_time: str = Form("09:00"),
    completion_note_visibility: str = Form("private"),
    creation_note_visibility: str = Form("private"),
):
    # 예약 실행은 항상 새 Freshdesk 동기화 알고리즘(오늘 생성분·오픈 케이스·항상 등록)을
    # 쓰므로, mode/case_status/date_basis/do_freshdesk 는 더 이상 화면에서 고르지 않는다
    # (DB 컬럼은 하위 호환을 위해 남겨두고 고정값만 채운다).
    schedule = {
        "enabled": 1 if enabled == "on" else 0,
        "run_time": run_time,
        "freshdesk_env": FRESHDESK_ENV,
        "do_freshdesk": 1,
        "mode": "today",
        "case_status": "open",
        "date_basis": "created",
        "completion_note_visibility": completion_note_visibility if completion_note_visibility in ("public", "private") else "private",
        "creation_note_visibility": creation_note_visibility if creation_note_visibility in ("public", "private") else "private",
    }
    store = _open_store()
    try:
        store.save_schedule(schedule)
    finally:
        store.close()
    scheduler_module.reload(schedule)
    return RedirectResponse("/freshdesk/schedule?saved=1", status_code=303)


@app.get("/healthz")
def healthz():
    """K8s liveness/readiness probe 용."""
    return {"status": "ok", "running": job_status.is_running()}


if __name__ == "__main__":
    # `python webapp.py`(또는 이를 PyInstaller로 패키징한 exe)로 직접 실행하는 경우 —
    # Docker/uvicorn CLI 없이 이 PC에서 응용프로그램처럼 바로 띄운다. 컨테이너와 달리
    # 이 PC는 VPN이 연결돼 있으므로, TICKET_SYNC_EXEC_MODE 를 따로 설정하지 않으면 기본값
    # "inprocess" 로 동작해 대시보드+실제 Engage Center 수집/Freshdesk 등록까지 이 프로세스
    # 하나로 전부 처리한다(local_runner.py 없이도 동작 — 컨테이너로 띄우는 방식은 그대로
    # docker-compose.yml/Dockerfile 로 계속 쓸 수 있다. 이건 그 대안일 뿐이다).
    import webbrowser

    import uvicorn

    port = int(os.getenv("TICKET_SYNC_PORT", "8000"))
    print("=" * 60)
    print("Ticket Sync 웹 앱 시작")
    print(f"잠시 후 브라우저에서 http://localhost:{port} 가 자동으로 열립니다.")
    print("이 창을 닫으면 앱이 종료됩니다.")
    print("=" * 60)
    threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    uvicorn.run(app, host="0.0.0.0", port=port)
