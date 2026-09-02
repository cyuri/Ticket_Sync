"""여러 프로세스가 동시에 브라우저 세션/SQLite 파일을 건드리지 않도록 막는 파일 기반 잠금.

웹 UI 안에서 "실행" 버튼을 두 번 누르는 것은 threading.Lock 하나로 충분히 막을 수 있지만,
K8s 에서는 자동 배치(CronJob)가 완전히 별도의 파드/프로세스로 떠서 동시에 같은
data/browser_profile, data/tickets.db 를 건드릴 수 있다. 이 둘은 같은 파이썬 프로세스가
아니므로 파이썬 객체 잠금으로는 막을 수 없고, 두 프로세스가 공유하는 파일(PVC로 마운트된
data 디렉터리)에 잠금 파일을 두는 방식만 프로세스 경계를 넘어 동작한다."""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ticket_sync.sync_lock")

# 이 시간보다 오래된 잠금 파일은 죽은 프로세스(예: 파드가 강제 종료됨)가 남긴 것으로 보고
# 정리한다. 실제 동기화는 큰 "완료됨" 목록을 받을 때도 몇 분 내로 끝나므로 2시간이면 충분히
# 넉넉한 여유다.
_STALE_SECONDS = 2 * 60 * 60


class SyncAlreadyRunningError(Exception):
    """다른 프로세스(웹 UI 또는 예약 실행)가 이미 동기화를 실행 중일 때 발생시킨다."""


def _lock_path(data_dir: Path) -> Path:
    return Path(data_dir) / "sync.lock"


def _read_owner(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").splitlines()[0]
    except Exception:
        return "(알 수 없음)"


def try_acquire(data_dir: Path, owner: str) -> Optional[Path]:
    """잠금 파일을 새로 만들 수 있으면 만들고 경로를 반환한다. 이미 유효한 잠금이 있으면
    None 을 반환한다. os.O_EXCL 로 원자적으로 생성하므로, 두 프로세스가 정확히 같은
    순간에 시도해도 반드시 하나만 성공한다."""
    path = _lock_path(data_dir)
    if path.exists():
        age = time.time() - path.stat().st_mtime
        if age < _STALE_SECONDS:
            return None
        logger.warning("오래된 잠금 파일(%.0f초 경과, owner=%s)을 정리하고 다시 시도합니다.", age, _read_owner(path))
        try:
            path.unlink()
        except OSError:
            pass
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(f"{owner}\n{datetime.now().isoformat(timespec='seconds')}\n{os.getpid()}\n")
        return path
    except FileExistsError:
        return None


def release(data_dir: Path):
    path = _lock_path(data_dir)
    try:
        path.unlink()
    except OSError:
        pass


def current_owner(data_dir: Path) -> Optional[str]:
    path = _lock_path(data_dir)
    if not path.exists():
        return None
    return _read_owner(path)


@contextmanager
def acquire_or_raise(data_dir: Path, owner: str):
    """with sync_lock.acquire_or_raise(data_dir, "web-ui"): ... 형태로 쓴다.
    이미 다른 프로세스가 실행 중이면 SyncAlreadyRunningError 를 던진다."""
    lock_path = try_acquire(data_dir, owner)
    if lock_path is None:
        raise SyncAlreadyRunningError(
            f"다른 프로세스({current_owner(data_dir)})가 이미 동기화를 실행 중입니다."
        )
    try:
        yield
    finally:
        release(data_dir)
