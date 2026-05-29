@echo off
REM Launch a dedicated Chrome instance for nudge-agent automation, with CDP
REM remote-debugging-port open on 9222.
REM
REM Chrome 136+ silently disables --remote-debugging-port whenever the
REM --user-data-dir matches Chrome's default location (anti-malware
REM mitigation). To work around that we keep a separate profile under
REM nudge-agent\chrome-data\ — first launch will be a fresh Chrome where
REM you sign into Claude.ai once; cookies persist across restarts.
REM
REM Safe to leave this Chrome running alongside your normal Chrome — they
REM use different user-data-dirs so they won't fight over profile locks.

set "CHROME="
for %%P in (
    "C:\Program Files\Google\Chrome\Application\chrome.exe"
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
    "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
) do if exist %%P set "CHROME=%%~P"

if "%CHROME%"=="" (
    echo Chrome not found. Please install Chrome or update this script.
    pause
    exit /b 1
)

"%CHROME%" ^
  --remote-debugging-port=9222 ^
  --user-data-dir="%~dp0chrome-data" ^
  --restore-last-session ^
  https://claude.ai/
