@echo off
setlocal
cd /d "C:\4DReplay\AId"
set "PYTHONUNBUFFERED=1"
rem Include root/service in PYTHONPATH
set "PYTHONPATH=C:\4DReplay\AId;C:\4DReplay\AId\src;C:\4DReplay\AId\service"
mkdir "C:\4DReplay\AId\logs" 2>nul

rem Start Flask (dev). For prod, consider waitress + wsgi.py.
"C:\Program Files\Python310\python.exe" "C:\4DReplay\AId\service\aid_agent.py" ^
  --host 0.0.0.0 --port 5086 --token "AID_TOKEN" ^
  --aid-cmd "C:\Program Files\Python310\python.exe C:\4DReplay\AId\src\aid_main.py" ^
  --workdir "C:\4DReplay\AId" ^
  --log-dir "C:\4DReplay\AId\logs"

endlocal
