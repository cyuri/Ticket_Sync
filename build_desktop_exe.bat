@echo off
REM TicketSyncDesktop.exe 빌드 스크립트 (Windows, PyInstaller)
REM 웹 화면(webapp.py)을 독립된 프로그램 창(tkinter 제어판) + 이 PC의 기본 브라우저로
REM 띄우는 응용프로그램. 창 안에 브라우저 엔진을 내장하지 않아(WebView2 등 불필요) 추가
REM 런타임 설치 없이 어떤 Windows PC에서도 동일하게 동작한다. 화면/기능은
REM ticket_sync_app.exe(브라우저로 바로 열리는 버전), Docker 컨테이너와 완전히
REM 동일하다 — 실행 방식만 다르다.
REM 사용법: build_desktop_exe.bat  (프로젝트 루트에서 실행)

setlocal

echo [1/4] 가상환경 확인 및 패키지 설치...
python -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo [2/4] Playwright 브라우저(Chromium) 설치...
python -m playwright install chromium
if errorlevel 1 goto :error

echo [3/4] PyInstaller로 exe 빌드...
REM templates/static 은 화면 렌더링에 반드시 필요해서 exe 안에 그대로 넣는다.
python -m PyInstaller --onefile --name TicketSyncDesktop ^
    --add-data "templates;templates" ^
    --add-data "static;static" ^
    --hidden-import freshdesk_case_sync ^
    desktop_app.py
if errorlevel 1 goto :error

echo [4/4] 배포용 보조 파일 복사...
if not exist dist\config mkdir dist\config
copy /Y config\selectors.json dist\config\selectors.json
copy /Y config\column_map.json dist\config\column_map.json
REM KT 사내망 TLS 인터셉션 인증서 — 다른 PC로 exe만 옮기면 이 파일이 없어서 Freshdesk API
REM 호출이 SSL 인증서 오류로 실패한다(local_runner.py 에서 실제로 겪었던 문제와 동일).
if not exist dist\docker\certs mkdir dist\docker\certs
copy /Y docker\certs\kt-corp-ca-bundle.crt dist\docker\certs\kt-corp-ca-bundle.crt
copy /Y config.example.json dist\config.example.json
copy /Y .env.example dist\.env.example
copy /Y README.md dist\README.md
if not exist dist\logs mkdir dist\logs
if not exist dist\output mkdir dist\output
if not exist dist\data mkdir dist\data

echo.
echo 빌드 완료: dist\TicketSyncDesktop.exe
echo dist 폴더를 배포하기 전에 config.example.json -^> config.json,
echo .env.example -^> .env 로 복사해서 값을 채워주세요.
echo 더블클릭하면 브라우저가 아니라 "Ticket Sync"라는 작은 프로그램 창(상태 표시 +
echo "대시보드 열기"/"종료" 버튼)이 뜨고, 실제 화면은 이 PC의 기본 브라우저로 자동으로
echo 열립니다. 프로그램 창을 닫으면 서버도 함께 종료됩니다.
echo 기본 포트는 8000이고 TICKET_SYNC_PORT 환경변수로 바꿀 수 있습니다 — Docker
echo 컨테이너나 ticket_sync_app.exe 와 같은 PC에서 동시에 띄우면 포트가 겹치니
echo 그중 하나만 켜두거나 포트를 다르게 설정하세요.
echo.
echo [중요] Playwright 브라우저 바이너리(Chromium)는 exe 안에 포함되지 않습니다.
echo 배포 대상 PC에서 실제로 "Playwright 브라우저를 설치하세요" 오류가 났던 적이 있습니다
echo (Python이 없는 PC). 대상 PC도 사내망(VPN 연결)이라 headless_shell/ffmpeg 은 필요
echo 없고, 아래 중 하나만 하면 됩니다:
echo   (a) 대상 PC에서 한 번 "python -m playwright install chromium" 실행 (Python 필요)
echo   (b) [권장] 이 PC의 chromium-* 폴더(보통 %%LOCALAPPDATA%%\ms-playwright\chromium-<번호>)
echo       만 dist\ms-playwright\ 안에 그대로 복사하세요. config.py 가 실행 시 exe와 같은
echo       폴더에 ms-playwright 폴더가 있으면 자동으로 그걸 쓰도록 이미 설정돼 있어서,
echo       PLAYWRIGHT_BROWSERS_PATH 를 따로 지정할 필요도 없습니다(Python 미설치 PC에서도
echo       바로 동작). 다만 Chromium 자체가 커서(~400MB) 배포 폴더 용량이 크게 늘어납니다.
goto :eof

:error
echo 빌드 중 오류가 발생했습니다.
exit /b 1
