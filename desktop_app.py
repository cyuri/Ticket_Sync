"""Ticket Sync 데스크톱 실행기 — 네이티브 창(tkinter) + 이 PC의 기본 브라우저.

이전 버전은 pywebview(+Microsoft Edge WebView2)로 화면을 창 안에 직접 그렸는데,
**실제로 다른 PC 2대에서 창이 아예 안 뜨고 콘솔도 조용히 멈추는 문제가 있었다** —
WebView2는 Windows에 보통 깔려있지만 별도 설치가 필요한 외부 의존성이라, 없는 PC에서는
그 초기화 단계에서 조용히 실패했다(원인 특정도 어려웠다).

이 버전은 그 의존성을 완전히 없앤다:
- 창 자체는 tkinter(파이썬 표준 라이브러리 — 항상 번들되어 있어 추가 설치가 필요 없다)로
  만든 작은 제어판(상태 표시 + "대시보드 열기"/"종료" 버튼)이다. 회색 배경의 전형적인
  Windows 프로그램 창 느낌이다.
- 실제 화면(대시보드)은 이 PC에 이미 깔려있는 기본 브라우저(Windows 10/11엔 Edge가 항상
  있음)로 연다 — 창 안에 브라우저 엔진을 내장하지 않으므로 WebView2 같은 추가 런타임이
  전혀 필요 없다.

그래서 별도로 설치해야 하는 것 없이 어떤 Windows PC에서도 동일하게 동작해야 한다."""
from __future__ import annotations

import os
import sys
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
from tkinter import ttk

# Windows 콘솔의 기본 코드페이지(cp949 등)는 한글을 출력하다가 깨질 수 있어 UTF-8로
# 강제하고, line_buffering=True 로 줄바꿈마다 즉시 flush 해서 초기화 중에 출력이 버퍼에
# 갇혀 콘솔에 "아무것도 안 보이는" 것처럼 보이는 문제를 막는다.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

PORT = int(os.getenv("TICKET_SYNC_PORT", "8000"))
URL = f"http://127.0.0.1:{PORT}"


def _run_server():
    import uvicorn

    from webapp import app

    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


def _wait_until_ready(timeout_s: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_s
    url = f"{URL}/healthz"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Ticket Sync")
        root.geometry("420x220")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.quit)

        self.status_var = tk.StringVar(value="서버를 시작하는 중...")
        ttk.Label(root, textvariable=self.status_var, wraplength=380, justify="center").pack(pady=(28, 14))

        self.open_btn = ttk.Button(root, text="대시보드 열기", command=self.open_dashboard, state="disabled")
        self.open_btn.pack(pady=6)

        ttk.Button(root, text="종료", command=self.quit).pack(pady=6)

        ttk.Label(root, text=URL, foreground="#666666").pack(side="bottom", pady=12)

        threading.Thread(target=self._start_server_and_wait, daemon=True).start()

    def _start_server_and_wait(self):
        print("[1/2] 웹 서버 시작 중...")
        threading.Thread(target=_run_server, daemon=True).start()
        ready = _wait_until_ready()
        self.root.after(0, self._on_ready, ready)

    def _on_ready(self, ready: bool):
        if ready:
            print("      OK")
            print(f"[2/2] 준비 완료 — {URL}")
            self.status_var.set(f"실행 중\n{URL}")
            self.open_btn["state"] = "normal"
            self.open_dashboard()
        else:
            print("[오류] 30초 안에 웹 서버가 응답하지 않았습니다.")
            self.status_var.set(
                "서버가 응답하지 않습니다.\nconfig.json / .env 파일을 확인하세요."
            )

    def open_dashboard(self):
        webbrowser.open(URL)

    def quit(self):
        self.root.destroy()
        os._exit(0)


def main():
    print("=" * 60)
    print("Ticket Sync 시작 중...")
    print("=" * 60)
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
