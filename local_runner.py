"""로컬 PC에서 직접 띄우는 "실행 에이전트".

Engage Center 로그인은 사내망/VPN에 연결된 이 PC에서만 가능하다(컨테이너는 별도
네트워크 경로라 로그인 자체가 안 된다). 그래서 웹 UI(대시보드)는 컨테이너로 띄우되,
실제 동기화 실행은 이 스크립트가 호스트에서 대신 수행하고, 컨테이너는 이 에이전트에게
HTTP로 실행을 요청만 한다 (webapp.py 의 TICKET_SYNC_EXEC_MODE=remote 설정과 짝을 이룬다).

실행 방법 (VPN 연결된 상태에서, PC에 계속 켜둔다):
    .venv\\Scripts\\python.exe local_runner.py
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, Header, HTTPException
import uvicorn

import config as config_module
import job_status
from logging_setup import setup_logging
from main import run_sync

logger = logging.getLogger("ticket_sync.local_runner")

# config.load_config() 는 요청 핸들러 안에서만 호출돼 .env 를 그때서야 읽는다 — 그런데
# 아래 RUNNER_TOKEN 은 모듈 임포트 시점에 바로 읽어야 하므로, 여기서 직접 한 번 로드한다
# (webapp.py는 Docker의 env_file 로 프로세스 환경 변수에 바로 꽂히므로 이 문제가 없지만,
# local_runner.py는 그냥 python 프로세스라 .env 로더가 따로 필요하다).
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

# 지금까지는 같은 PC 안(host.docker.internal)에서만 호출됐으니 인증이 없어도 안전했다.
# Cloudflare Tunnel 등으로 이 에이전트를 외부(예: AKS)에 노출하면, 아무나 이 URL을 알면
# 실제 Engage Center 실행/Freshdesk 등록을 시킬 수 있게 되므로 공유 토큰으로 막는다.
# TICKET_SYNC_RUNNER_TOKEN 을 설정하지 않으면(기존 로컬 전용 사용법) 검사를 건너뛴다.
RUNNER_TOKEN = os.getenv("TICKET_SYNC_RUNNER_TOKEN", "")


def _verify_token(authorization: str = Header(default="")):
    if not RUNNER_TOKEN:
        return
    if authorization != f"Bearer {RUNNER_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")


app = FastAPI(title="Ticket Sync Local Runner", dependencies=[Depends(_verify_token)])


def _run_job(params: dict):
    run_date_str = datetime.now().strftime("%Y%m%d")
    try:
        app_config = config_module.load_config(
            freshdesk_choice=params["freshdesk_env"],
            require_freshdesk_credentials=params["do_freshdesk"],
        )
        setup_logging(app_config.log_dir, run_date_str)
        logger.info("로컬 실행 에이전트: 동기화 시작 %s", params)
        run_sync(
            app_config,
            params["mode"],
            params["start_date"],
            params["end_date"],
            params["case_status"],
            params["date_basis"],
            params["do_freshdesk"],
            run_date_str,
            owner=params.get("owner") or "local-runner",
            skip_dedup=params.get("skip_dedup", False),
            save_excel=params.get("save_excel", False),
        )
        job_status.finish()
    except Exception as exc:  # noqa: BLE001 - 실행 이력 화면에 원인을 보여주기 위해 광범위하게 잡는다
        logger.exception("로컬 실행 에이전트: 동기화 중 오류")
        job_status.finish(error=str(exc))


@app.post("/run")
def run(
    freshdesk_env: str = Form("prod"),
    do_freshdesk: Optional[str] = Form(None),
    mode: str = Form("all"),
    start_date: Optional[str] = Form(None),
    end_date: Optional[str] = Form(None),
    date_basis: str = Form("created"),
    case_status: str = Form("open"),
    test_mode: Optional[str] = Form(None),
    save_excel: Optional[str] = Form(None),
    owner: str = Form("web-ui"),
):
    """webapp.py(컨테이너)가 "실행" 요청을 위임할 때 호출하는 엔드포인트.
    webapp.py 의 /sync 와 동일한 규칙으로 원본 폼 값을 해석한다."""
    params = {
        "freshdesk_env": freshdesk_env,
        "do_freshdesk": do_freshdesk == "on",
        "mode": mode,
        "start_date": start_date if mode == "range" else None,
        "end_date": (end_date or datetime.now().strftime("%Y-%m-%d")) if mode == "range" else None,
        "date_basis": date_basis,
        "case_status": case_status,
        "skip_dedup": test_mode == "on",
        "save_excel": save_excel == "on",
        "owner": owner,
    }
    if not job_status.try_start(params):
        return {"ok": False, "error": "already_running"}
    threading.Thread(target=_run_job, args=(params,), daemon=True).start()
    return {"ok": True}


def _run_case_sync_job(params: dict):
    try:
        app_config = config_module.load_config(
            freshdesk_choice=params["freshdesk_env"], require_freshdesk_credentials=True,
        )
        setup_logging(app_config.log_dir, datetime.now().strftime("%Y%m%d"))
        logger.info("로컬 실행 에이전트: 신규 Freshdesk 동기화 시작 %s", params)

        import freshdesk_case_sync

        if params.get("case_kind") == "closed":
            freshdesk_case_sync.sync_closed_cases(
                app_config, scope=params["scope"],
                start_date=params.get("start_date"), end_date=params.get("end_date"),
                creation_note_visibility=params.get("creation_note_visibility", "private"),
                completion_note_visibility=params.get("completion_note_visibility", "private"),
            )
        else:
            freshdesk_case_sync.sync_new_cases(
                app_config, scope=params["scope"],
                start_date=params.get("start_date"), end_date=params.get("end_date"),
                creation_note_visibility=params.get("creation_note_visibility", "private"),
            )
            if params.get("run_closure_check"):
                freshdesk_case_sync.check_and_finalize_closed_cases(
                    app_config, note_visibility=params["note_visibility"],
                )
        job_status.finish()
    except Exception as exc:  # noqa: BLE001 - 실행 이력에 원인을 남기기 위해 광범위하게 잡는다
        logger.exception("로컬 실행 에이전트: 신규 Freshdesk 동기화 중 오류")
        job_status.finish(error=str(exc))


@app.post("/run-case-sync")
def run_case_sync(
    scope: str = Form("today"),
    start_date: Optional[str] = Form(None),
    end_date: Optional[str] = Form(None),
    note_visibility: str = Form("private"),
    creation_note_visibility: str = Form("private"),
    completion_note_visibility: str = Form("private"),
    case_kind: str = Form("open"),
    run_closure_check: Optional[str] = Form(None),
    owner: str = Form("scheduler"),
):
    """scheduler.py(예약 실행) 와 대시보드의 "전체 오픈 케이스 일괄 등록"/"완료된 케이스
    일괄 등록" 버튼이 위임하는 엔드포인트. 기존 /run(전체 수집 파이프라인)과 완전히
    분리된 새 알고리즘(freshdesk_case_sync.py) 전용이다. case_kind="closed" 면 완료된
    케이스 일괄 등록(sync_closed_cases)을, 아니면 기존 오픈 케이스 동기화를 수행한다."""
    params = {
        "freshdesk_env": "prod",
        "scope": scope if scope in ("today", "all", "range") else "today",
        "start_date": start_date or None,
        "end_date": end_date or None,
        "note_visibility": note_visibility,
        "creation_note_visibility": creation_note_visibility,
        "completion_note_visibility": completion_note_visibility,
        "case_kind": case_kind if case_kind in ("open", "closed") else "open",
        "run_closure_check": run_closure_check == "on",
        "owner": owner,
    }
    if not job_status.try_start(params):
        return {"ok": False, "error": "already_running"}
    threading.Thread(target=_run_case_sync_job, args=(params,), daemon=True).start()
    return {"ok": True}


def _run_closed_note_sweep_job(params: dict):
    try:
        app_config = config_module.load_config(require_freshdesk_credentials=True)
        setup_logging(app_config.log_dir, datetime.now().strftime("%Y%m%d"))
        logger.info("로컬 실행 에이전트: 티켓 종료 처리 시작 %s", params)

        import freshdesk_case_sync

        if params.get("scope") == "all_open":
            stats = freshdesk_case_sync.check_all_open_cases_for_completion(
                app_config, note_visibility=params.get("note_visibility", "private"),
            )
        else:
            stats = freshdesk_case_sync.check_closed_cases_by_freshdesk_date(
                app_config, start_date=params["date"], end_date=params["date"],
                note_visibility=params.get("note_visibility", "private"),
            )
        job_status.finish(result=stats)
    except Exception as exc:  # noqa: BLE001 - 실행 이력에 원인을 남기기 위해 광범위하게 잡는다
        logger.exception("로컬 실행 에이전트: 티켓 종료 처리 중 오류")
        job_status.finish(error=str(exc))


@app.post("/run-closed-note-sweep")
def run_closed_note_sweep(
    scope: str = Form("date"),
    date: str = Form(""),
    note_visibility: str = Form("private"),
    owner: str = Form("web-ui-freshdesk"),
):
    """"Freshdesk Management > 티켓 종료 처리" 화면의 실행 버튼이 위임하는 엔드포인트.
    scope="date" 면 접수일 하루만, scope="all_open" 이면 접수일과 무관하게 현재 열려있는
    케이스ID 태그 티켓 전체를 대상으로 한다. 어느 쪽이든 로컬 DB에는 아무것도 저장하지
    않는 1회성 점검이다."""
    scope = scope if scope in ("date", "all_open") else "date"
    params = {
        "scope": scope,
        "date": date,
        "note_visibility": note_visibility,
        "owner": owner,
    }
    if not job_status.try_start(params):
        return {"ok": False, "error": "already_running"}
    threading.Thread(target=_run_closed_note_sweep_job, args=(params,), daemon=True).start()
    return {"ok": True}


@app.get("/healthz")
def healthz():
    return {"status": "ok", **job_status.snapshot()}


if __name__ == "__main__":
    print("Ticket Sync 로컬 실행 에이전트 시작: http://0.0.0.0:8787 (VPN 연결 상태를 유지하세요)")
    uvicorn.run(app, host="0.0.0.0", port=8787)
