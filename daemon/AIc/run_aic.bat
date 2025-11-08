@echo off
setlocal enableextensions

rem ── 고정 경로/환경 ──────────────────────────────────────────────────────────
set "APP_ROOT=C:\4DReplay\V5"
set "SRC_DIR=%APP_ROOT%\src"
set "AID_DIR=%APP_ROOT%\daemon\AIc"
set "LOG_DIR=%AID_DIR%\logs"
set "PYTHON_EXE=C:\Progra~1\Python310\python.exe"

set "FFMPEG_BIN=%APP_ROOT%\third_party\ffmpeg\bin"
set "EXTRA_DLLS=%APP_ROOT%\bin"
set "PATH=%FFMPEG_BIN%;%EXTRA_DLLS%;%PATH%"

set "PYTHONFAULTHANDLER=1"
set "FD_SERVICE=1"
set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"
set "FD_LOG_DIR=%LOG_DIR%"
set "KMP_DUPLICATE_LIB_OK=TRUE"
set "TF_CPP_MIN_LOG_LEVEL=1"

rem 로그 폴더 생성
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

rem ── 부트 로깅 ───────────────────────────────────────────────────────────────
set "BOOT_LOG=%LOG_DIR%\_aic_boot.log"
(
  echo ==== %DATE% %TIME% :: AID Daemon Starter ====
  whoami 2^>nul
  echo PWD: %CD%
  echo PYTHON_EXE: %PYTHON_EXE%
  echo APP_ROOT: %APP_ROOT%
  echo SRC_DIR: %SRC_DIR%
  echo LOG_DIR: %LOG_DIR%
  echo FD_SERVICE: %FD_SERVICE%
) >> "%BOOT_LOG%"

rem ── 실행 디렉터리 변경 ─────────────────────────────────────────────────────
cd /d "%SRC_DIR%\AId"

rem ── Python 메인 실행 ───────────────────────────────────────────────────────
echo ==== %DATE% %TIME% :: launching aic_main.py ====>> "%BOOT_LOG%"
"%PYTHON_EXE%" -u "%SRC_DIR%\AId\aic_main.py"
set "RC=%ERRORLEVEL%"
echo ==== %DATE% %TIME% :: aid_main.py exit rc=%RC% ====>> "%BOOT_LOG%"

rem ── 리턴코드 기록 ──────────────────────────────────────────────────────────
echo %RC%> "%FD_LOG_DIR%\_aic.rc"
endlocal & exit /b %RC%
