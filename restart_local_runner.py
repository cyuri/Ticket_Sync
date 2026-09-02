"""local_runner.py 수동 재시작용 실행 파일.

더블클릭으로 실행하는 독립 .exe (build_restart_exe.bat 로 PyInstaller 빌드) — 이미 떠있는
local_runner.py 를 찾아서 종료하고, 새 프로세스로 다시 띄운 뒤 정상 기동됐는지(/healthz)
확인한다. local_runner.py 가 브라우저 관련 오류 등으로 죽었을 때, 로그인해서 명령어를 다시
치지 않고도 재시작할 수 있도록 하기 위한 용도다.

빌드 후 exe 를 어디로 옮겨도 동작하도록, 프로젝트 경로는 아래 PROJECT_DIR 에 고정한다
(시작프로그램 폴더의 바로가기와 동일한 방식 — 프로젝트를 다른 경로로 옮기면 이 값도
같이 고쳐야 한다)."""
from __future__ import annotations

import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import psutil

# Windows 콘솔의 기본 코드페이지(cp949 등)는 "—" 같은 일부 문자를 표현하지 못해 그냥
# print() 만 해도 UnicodeEncodeError 로 죽을 수 있다 — 항상 UTF-8로 강제한다.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROJECT_DIR = Path(r"C:\Users\youre\Downloads\ticket_sync")
PYTHONW = PROJECT_DIR / ".venv" / "Scripts" / "pythonw.exe"
HEALTHZ_URL = "http://localhost:8787/healthz"


def _find_running() -> list[psutil.Process]:
    found = []
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            cmdline = proc.info["cmdline"] or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if any("local_runner.py" in part for part in cmdline):
            found.append(proc)
    return found


def _stop_running() -> int:
    procs = _find_running()
    for proc in procs:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if procs:
        _, alive = psutil.wait_procs(procs, timeout=5)
        for proc in alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    return len(procs)


def _check_healthz(timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(HEALTHZ_URL, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    print("=" * 60)
    print("Ticket Sync 로컬 실행 에이전트(local_runner.py) 재시작")
    print("=" * 60)

    if not PYTHONW.exists():
        print(f"[오류] {PYTHONW} 를 찾을 수 없습니다. PROJECT_DIR 설정을 확인하세요.")
        return 1

    print("\n[1/3] 기존 프로세스 확인 중...")
    stopped = _stop_running()
    print(f"      기존 프로세스 {stopped}개 종료함." if stopped else "      실행 중인 프로세스 없음.")

    print("\n[2/3] 새 프로세스 시작 중...")
    # stdin/stdout/stderr 를 명시적으로 DEVNULL 로 돌리지 않으면(예: DETACHED_PROCESS 플래그만
    # 쓰는 경우) pythonw.exe 가 유효하지 않은 콘솔 핸들을 물려받아 시작하자마자 조용히
    # 죽는다 — 실제로 겪은 문제라 반드시 이렇게 해야 한다.
    subprocess.Popen(
        [str(PYTHONW), "local_runner.py"],
        cwd=str(PROJECT_DIR),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    print("\n[3/3] 정상 기동 확인 중(healthz)...")
    if _check_healthz():
        print("      OK — local_runner.py 가 정상적으로 재시작됐습니다.")
        result = 0
    else:
        print("      [오류] 10초 안에 응답이 없습니다. VPN 연결/설정을 확인하세요.")
        result = 1

    print()
    input("아무 키나 눌러 이 창을 닫으세요...")
    return result


if __name__ == "__main__":
    sys.exit(main())
