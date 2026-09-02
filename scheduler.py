"""웹 UI에서 설정하는 "예약 실행"(예: 매일 09:00) 을 담당하는 앱 내장 스케줄러.

K8s CronJob(k8s/cronjob.yaml)과 하는 일이 겹친다 — 어느 쪽이든 하나만 쓰면 된다.
차이는: CronJob은 클러스터 밖(kubectl)에서 스케줄을 관리하고, 이 스케줄러는 웹 UI 화면에서
직접 켜고 끄고 시간을 바꿀 수 있다(재배포 없이). 웹 서버 프로세스가 떠 있는 동안에만
동작하므로, 컨테이너로 띄워도 그 컨테이너가 계속 실행 중이면 그 안에서 계속 돈다.

실제 실행 알고리즘은 freshdesk_case_sync.py 에 있다 — 오늘 새로 생성된 오픈 케이스를
Freshdesk에 태그 기준 중복 없이 등록하고(1단계), 예전에 등록한 케이스가 Engage Center에서
완료됐는지 매일 확인해(2단계) 완료됐으면 마지막 대화를 노트로 남긴다(3단계). 이 흐름은
/cases 화면(로컬 tickets 테이블)에는 아무것도 쓰지 않는다 — 대시보드 "지금 실행"과 /cases의
수동 Freshdesk 등록은 이 스케줄러와 무관하게 지금까지처럼 동작한다.

job_status/sync_lock 보호 방식은 기존과 동일 — 예약 실행과 수동 실행이 겹치면 나중 쪽이
"다른 프로세스가 이미 실행 중" 오류로 조용히 스킵된다.

TICKET_SYNC_EXEC_MODE=remote (webapp.py 와 동일 규칙) 인 경우, 이 스케줄러는 타이머
역할만 하고 실제 실행은 local_runner.py 에게 HTTP로 위임한다 — 사내망/VPN 이 없는
컨테이너 안에서는 실제 로그인이 안 되기 때문이다."""
from __future__ import annotations

import logging
import os

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config as config_module
import job_status
import sync_lock
from storage import TicketStore

logger = logging.getLogger("ticket_sync.scheduler")

_JOB_ID = "daily-sync"
_scheduler: BackgroundScheduler | None = None

EXEC_MODE = os.getenv("TICKET_SYNC_EXEC_MODE", "inprocess")
LOCAL_RUNNER_URL = os.getenv("TICKET_SYNC_LOCAL_RUNNER_URL", "http://host.docker.internal:8787")
RUNNER_TOKEN = os.getenv("TICKET_SYNC_RUNNER_TOKEN", "")

# Freshdesk 연결 대상은 화면에서 고르지 않고 항상 운영으로 고정한다(webapp.py 의
# FRESHDESK_ENV 와 동일한 결정).
FRESHDESK_ENV = "prod"


def _runner_headers() -> dict:
    """webapp.py의 동명 헬퍼와 동일 — local_runner.py가 TICKET_SYNC_RUNNER_TOKEN 을 요구하는
    배포에서만 헤더가 실제로 채워진다."""
    if not RUNNER_TOKEN:
        return {}
    return {"Authorization": f"Bearer {RUNNER_TOKEN}"}


def _run_scheduled_job():
    """스케줄된 시각에 APScheduler 가 호출하는 함수. job_status 로 실행 상태를 표시해야
    대시보드의 "실행 중" 표시/healthz 가 예약 실행 중에도 정확하게 보인다."""
    schedule = None
    attempted = False
    try:
        probe_config = config_module.load_config(require_freshdesk_credentials=False)
        store = TicketStore.from_config(probe_config)
        try:
            schedule = store.get_schedule()
        finally:
            store.close()

        if not schedule.get("enabled"):
            logger.info("예약 실행이 꺼져 있어 이번 스케줄은 건너뜁니다.")
            return

        if EXEC_MODE == "remote":
            attempted = _trigger_remote_run(schedule)
            return

        if not job_status.try_start({"source": "schedule", **schedule}):
            logger.warning("예약 실행 시각이 됐지만 이미 다른 실행이 진행 중이라 건너뜁니다.")
            return

        attempted = True
        # freshdesk_case_sync 는 crawler(Playwright) 를 쓰므로 무거운 임포트다 — 실제로
        # 예약 실행이 켜졌을 때만 임포트해서 웹 서버 시작 비용에 영향을 주지 않는다.
        import freshdesk_case_sync

        app_config = config_module.load_config(
            freshdesk_choice=FRESHDESK_ENV, require_freshdesk_credentials=True,
        )
        logger.info("예약 실행(신규 Freshdesk 동기화) 시작")
        freshdesk_case_sync.sync_new_cases(
            app_config, scope="today",
            creation_note_visibility=schedule.get("creation_note_visibility", "private"),
        )
        freshdesk_case_sync.check_and_finalize_closed_cases(
            app_config, note_visibility=schedule.get("completion_note_visibility", "private"),
        )
        job_status.finish()
    except sync_lock.SyncAlreadyRunningError as exc:
        logger.warning("예약 실행 시각이 됐지만 다른 프로세스가 이미 실행 중이라 건너뜁니다: %s", exc)
        job_status.finish(error=str(exc))
    except Exception as exc:
        logger.exception("예약 실행 중 오류가 발생했습니다")
        job_status.finish(error=str(exc))
    finally:
        if attempted:
            try:
                probe_config = config_module.load_config(require_freshdesk_credentials=False)
                store = TicketStore.from_config(probe_config)
                try:
                    store.mark_schedule_triggered()
                finally:
                    store.close()
            except Exception:
                logger.warning("예약 실행 시각 기록 실패", exc_info=True)


def _trigger_remote_run(schedule: dict) -> bool:
    """local_runner.py(로컬 PC) 에게 예약 실행을 위임한다. 위임에 성공하면 True."""
    try:
        resp = requests.post(
            f"{LOCAL_RUNNER_URL}/run-case-sync",
            data={
                "scope": "today",
                "note_visibility": schedule.get("completion_note_visibility", "private"),
                "creation_note_visibility": schedule.get("creation_note_visibility", "private"),
                "run_closure_check": "on",
                "owner": "scheduler",
            },
            headers=_runner_headers(),
            timeout=5,
        )
        body = resp.json()
    except requests.RequestException as exc:
        logger.warning("예약 실행: 로컬 실행 에이전트(PC)에 연결하지 못했습니다: %s", exc)
        return False
    if not body.get("ok"):
        logger.warning("예약 실행: 로컬 실행 에이전트가 이미 다른 실행 중이라 건너뜁니다.")
        return False
    logger.info("예약 실행: 로컬 실행 에이전트에 위임했습니다.")
    return True


def _apply_trigger(schedule: dict):
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(_JOB_ID)
    except Exception:
        pass
    if not schedule.get("enabled"):
        logger.info("예약 실행이 꺼져 있어 스케줄을 등록하지 않습니다.")
        return
    hour, _, minute = schedule.get("run_time", "09:00").partition(":")
    _scheduler.add_job(
        _run_scheduled_job,
        CronTrigger(hour=int(hour or 9), minute=int(minute or 0)),
        id=_JOB_ID,
        replace_existing=True,
    )
    logger.info("예약 실행 등록됨: 매일 %s", schedule.get("run_time"))


def start(app_config) -> BackgroundScheduler:
    """앱 시작 시 한 번 호출한다. 저장된 예약 설정을 읽어 스케줄을 등록한다."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler()  # 타임존 생략 시 시스템 로컬 타임존을 그대로 쓴다
    _scheduler.start()
    store = TicketStore.from_config(app_config)
    try:
        schedule = store.get_schedule()
    finally:
        store.close()
    _apply_trigger(schedule)
    return _scheduler


def reload(schedule: dict):
    """웹 UI에서 예약 설정을 저장한 직후 호출해 스케줄을 즉시 반영한다."""
    _apply_trigger(schedule)


def next_run_time() -> str | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job(_JOB_ID)
    if job is None or job.next_run_time is None:
        return None
    return job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
