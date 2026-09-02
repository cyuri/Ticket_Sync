@echo off
REM RestartLocalRunner.exe 빌드 스크립트 (Windows, PyInstaller)
REM 사용법: build_restart_exe.bat  (프로젝트 루트에서 실행, .venv 활성화된 상태 권장)

setlocal

echo [1/2] psutil 설치 확인...
python -m pip install psutil>=6.0
if errorlevel 1 goto :error

echo [2/2] PyInstaller로 exe 빌드...
python -m PyInstaller --onefile --name RestartLocalRunner restart_local_runner.py
if errorlevel 1 goto :error

echo.
echo 빌드 완료: dist\RestartLocalRunner.exe
echo 이 exe 는 어디로 옮겨서 실행해도 되지만(더블클릭), 대상 PC의 프로젝트 경로가
echo restart_local_runner.py 안의 PROJECT_DIR 과 다르면 그 값을 먼저 고쳐서 다시 빌드하세요.
goto :eof

:error
echo 빌드 중 오류가 발생했습니다.
exit /b 1
