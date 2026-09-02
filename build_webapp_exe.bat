@echo off
REM ticket_sync_app.exe 빌드 스크립트 (Windows, PyInstaller)
REM 웹 대시보드(webapp.py)를 Docker 없이 이 PC에서 더블클릭으로 바로 띄우는 응용프로그램.
REM 컨테이너로 띄우는 방식(docker-compose.yml)은 그대로 계속 쓸 수 있다 — 이건 그 대안이다.
REM 사용법: build_webapp_exe.bat  (프로젝트 루트에서 실행)

setlocal

echo [1/4] 가상환경 확인 및 패키지 설치...
python -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo [2/4] Playwright 브라우저(Chromium) 설치...
python -m playwright install chromium
if errorlevel 1 goto :error

echo [3/4] PyInstaller로 exe 빌드...
REM templates/static 은 화면 렌더링에 반드시 필요해서 exe 안에 그대로 넣는다.
REM config/, selectors.json, column_map.json, config.json 은 실행 후 사용자가 수정할 수
REM 있어야 하므로 exe 안에 넣지 않고, 빌드 후 dist 폴더로 별도 복사한다(ticket_sync.exe 와
REM 동일한 방식).
python -m PyInstaller --onefile --name ticket_sync_app ^
    --add-data "templates;templates" ^
    --add-data "static;static" ^
    --hidden-import freshdesk_case_sync ^
    webapp.py
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
echo 빌드 완료: dist\ticket_sync_app.exe
echo dist 폴더를 배포하기 전에 config.example.json -^> config.json,
echo .env.example -^> .env 로 복사해서 값을 채워주세요.
echo 더블클릭하면 대시보드가 뜨고 브라우저가 자동으로 열립니다(기본 포트 8000,
echo TICKET_SYNC_PORT 환경변수로 바꿀 수 있음). Docker 컨테이너와 같은 PC에서 동시에
echo 띄우면 포트(기본 8000)가 겹치니 둘 중 하나만 켜두세요.
echo.
echo [중요] Playwright 브라우저 바이너리(Chromium)는 exe 안에 포함되지 않습니다.
echo 배포 대상 PC에도 아래 중 하나가 필요합니다:
echo   (a) 대상 PC에서 한 번 "python -m playwright install chromium" 실행 (Python 필요)
echo   (b) 이 PC의 브라우저 캐시 폴더(보통 %%LOCALAPPDATA%%\ms-playwright)를 통째로
echo       dist\ms-playwright 로 복사하고, 실행 전 환경변수
echo       PLAYWRIGHT_BROWSERS_PATH=.\ms-playwright 를 설정 (Python 미설치 PC에서도 동작)
goto :eof

:error
echo 빌드 중 오류가 발생했습니다.
exit /b 1
