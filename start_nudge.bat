@echo off
setlocal

set "AGENT_DIR=%~dp0"
set "AGENT_DIR=%AGENT_DIR:~0,-1%"

REM Convert Windows path to WSL path: D:\foo\bar -> /mnt/d/foo/bar
set "DRIVE=%AGENT_DIR:~0,1%"
call :LOWER %DRIVE% DRIVE_LOWER
set "WSL_PATH=/mnt/%DRIVE_LOWER%/%AGENT_DIR:~3%"
set "WSL_PATH=%WSL_PATH:\=/%"

echo === Nudge Agent - Full Startup ===
echo   Windows: %AGENT_DIR%
echo   WSL:     %WSL_PATH%
echo.

echo [0/4] Cleaning up old processes...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='py.exe'\" | Where-Object { $_.CommandLine -like '*nudge_inject*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
wsl -d Ubuntu -- tmux kill-session -t nudge-agent 2>NUL
echo   Done.
echo.

echo [1/4] Starting Chrome with debug port...
start "" "%AGENT_DIR%\start_chrome_debug.bat"
timeout /t 3 /nobreak >nul

echo [2/4] Starting Claude in WSL tmux...
wsl -d Ubuntu -- tmux new-session -d -s nudge-agent -c "%WSL_PATH%" "claude"
timeout /t 5 /nobreak >nul

echo [3/4] Starting nudge injector in background...
cd /d "%AGENT_DIR%"
start "" /min py nudge_inject.py

echo [4/4] Attaching to Claude Code CLI...
echo.
echo   Ctrl+B then D to detach (agent keeps running)
echo.
wsl -d Ubuntu -- tmux attach -t nudge-agent

endlocal
exit /b

:LOWER
set "%2=%~1"
for %%a in (a b c d e f g h i j k l m n o p q r s t u v w x y z) do (
    call set "%2=%%%2:%%a=%%a%%"
)
exit /b
