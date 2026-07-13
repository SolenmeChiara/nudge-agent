@echo off
setlocal

set "AGENT_DIR=%~dp0"
set "AGENT_DIR=%AGENT_DIR:~0,-1%"
set "HEALTH_DIR=%AGENT_DIR%\..\Health-data-csv-MCP\server"

REM Convert Windows path to WSL path: D:\foo\bar -> /mnt/d/foo/bar
set "DRIVE=%AGENT_DIR:~0,1%"
call :LOWER %DRIVE% DRIVE_LOWER
set "WSL_PATH=/mnt/%DRIVE_LOWER%/%AGENT_DIR:~3%"
set "WSL_PATH=%WSL_PATH:\=/%"

echo === Nudge Agent - Full Startup ===
echo   Windows: %AGENT_DIR%
echo   WSL:     %WSL_PATH%
echo.

echo [0/5] Cleaning up old processes...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='py.exe' OR Name='cloudflared.exe'\" | Where-Object { $_.CommandLine -like '*nudge_inject*' -or $_.CommandLine -like '*src.ingest*' -or $_.CommandLine -like '*tunnel*localhost:8765*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
wsl -d Ubuntu -- tmux kill-session -t nudge-agent 2>NUL
if exist "%AGENT_DIR%\next_wakeup.txt" del "%AGENT_DIR%\next_wakeup.txt"
echo   Done.
echo.

echo [1/5] Starting Chrome with debug port...
start "" "%AGENT_DIR%\start_chrome_debug.bat"
timeout /t 3 /nobreak >nul

echo [2/5] Starting Health ingester (port 8765)...
if exist "%HEALTH_DIR%\src\ingest.py" (
    start "" /min cmd /c "cd /d "%HEALTH_DIR%" && python -m src.ingest"
    echo   Health ingester started.
) else (
    echo   Health-data-csv-MCP not found, skipping.
)
timeout /t 2 /nobreak >nul

REM Cloudflare tunnel step removed 2026-07-13: HAE uploads go to the fixed
REM Tailscale address, so the quick tunnel was a zombie (random URL each
REM restart, nothing pointed at it). Cleanup in [0/5] still reaps strays.

echo [3/5] Starting Claude in WSL tmux (bypassPermissions: unattended agent,
echo        a permission prompt nobody answers would deadlock the whole night)...
wsl -d Ubuntu -- tmux new-session -d -s nudge-agent -c "%WSL_PATH%" "claude --permission-mode bypassPermissions"
timeout /t 5 /nobreak >nul

echo [4/5] Starting nudge injector in background...
cd /d "%AGENT_DIR%"
start "" /min py nudge_inject.py

echo [5/5] Attaching to Claude Code CLI...
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
