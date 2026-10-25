@echo off
rem Starts the AI Black Hole live server in its own window and opens the page.
cd /d "%~dp0"
start "AI Black Hole live server" /min python live_server.py %*
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:8787/"
