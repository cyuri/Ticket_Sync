"""웹 UI(수동 "지금 실행")와 앱 내장 스케줄러(예약 실행)가 같은 프로세스 안에서
공유하는 "지금 실행 중인지" 상태.

둘 다 같은 파이썬 프로세스 안의 백그라운드 스레드로 실행되므로, 모듈 전역 상태 하나로
공유하면 어느 쪽이 실행 중이든 대시보드의 "실행 중" 표시와 healthz 가 정확하게 반영되고,
서로 겹치는 것도 여기서 먼저 막을 수 있다(K8s CronJob 처럼 완전히 다른 프로세스와의
충돌은 sync_lock.py 가 별도로 막는다)."""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Optional

_lock = threading.Lock()
_state: dict = {
    "running": False, "started_at": None, "params": None, "last_error": None,
    "step": None, "percent": None, "last_result": None,
}


def try_start(params: dict) -> bool:
    """이미 실행 중이면 False 를 반환하고 아무 것도 바꾸지 않는다. 아니면 실행 중 상태로
    바꾸고 True 를 반환한다."""
    with _lock:
        if _state["running"]:
            return False
        _state["running"] = True
        _state["started_at"] = datetime.now()
        _state["params"] = params
        _state["last_error"] = None
        _state["step"] = None
        _state["percent"] = None
        _state["last_result"] = None
        return True


def set_progress(step: str, percent: Optional[int] = None):
    """실행 중인 작업이 지금 어느 단계인지("케이스 목록 수집 중" 등)와 진행률(0~100)을
    갱신한다. 대시보드에서 게임 로딩 화면처럼 단계/퍼센트로 보여주는 데 쓰인다. 실행 중이
    아닐 때 호출되면(예: try_start 실패 후에도 호출되는 경로) 조용히 무시한다."""
    with _lock:
        if not _state["running"]:
            return
        _state["step"] = step
        if percent is not None:
            _state["percent"] = max(0, min(100, int(percent)))


def finish(error: Optional[str] = None, result: Optional[dict] = None):
    """result 는 "결과 로그"로 화면에 그대로 보여줄 통계 dict(예: 완료 케이스 노트 추가
    기능의 조회/등록/실패 건수) — 로컬 DB에는 저장하지 않고 다음 실행 전까지만 메모리에
    남아있는 값이다. 넘기지 않으면(기존 호출부들처럼) None 으로 비워진다."""
    with _lock:
        _state["running"] = False
        _state["started_at"] = None
        _state["params"] = None
        _state["last_error"] = error
        _state["step"] = None
        _state["percent"] = None
        _state["last_result"] = result


def is_running() -> bool:
    with _lock:
        return _state["running"]


def snapshot() -> dict:
    """대시보드 렌더링/healthz 에서 그대로 쓸 수 있는 스냅샷."""
    with _lock:
        elapsed = None
        if _state["started_at"]:
            elapsed = int((datetime.now() - _state["started_at"]).total_seconds())
        return {
            "running": _state["running"],
            "params": _state["params"],
            "elapsed_seconds": elapsed,
            "last_error": _state["last_error"],
            "step": _state["step"],
            "percent": _state["percent"],
            "last_result": _state["last_result"],
        }
