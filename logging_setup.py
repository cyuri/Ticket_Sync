"""파일 + 콘솔 로깅 구성. logs/ticket_sync_YYYYMMDD.log 에 실행 로그가 쌓인다."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path


def setup_logging(log_dir: Path, run_date: str | None = None) -> Path:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    run_date = run_date or datetime.now().strftime("%Y%m%d")
    log_path = log_dir / f"ticket_sync_{run_date}.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)

    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    logging.getLogger("urllib3").setLevel(logging.WARNING)

    return log_path
