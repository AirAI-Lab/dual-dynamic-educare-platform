@echo off
cd /d "%~dp0"
python -m backend.main --host 127.0.0.1 --port 8000
pause
