@echo off
setlocal

REM Start nudge-agent as a persistent Claude Code instance inside WSL tmux.
REM The tmux session stays alive even after this window closes.
REM
REM To attach:  wsl tmux attach -t nudge-agent
REM To detach:  Ctrl+B then D
REM To kill:    wsl tmux kill-session -t nudge-agent

set "AGENT_DIR=%~dp0"
set "AGENT_DIR=%AGENT_DIR:~0,-1%"
set "DRIVE=%AGENT_DIR:~0,1%"
call :LOWER %DRIVE% DRIVE_LOWER
set "WSL_PATH=/mnt/%DRIVE_LOWER%/%AGENT_DIR:~3%"
set "WSL_PATH=%WSL_PATH:\=/%"

echo Killing existing session (if any)...
wsl tmux kill-session -t nudge-agent 2>NUL

echo Starting nudge-agent in WSL tmux...
wsl -d Ubuntu tmux new-session -d -s nudge-agent "bash -lc 'cd %WSL_PATH% && claude --permission-mode bypassPermissions'"

echo.
echo  nudge-agent started in tmux session 'nudge-agent'
echo  To attach:  wsl tmux attach -t nudge-agent
echo  To detach:  Ctrl+B then D
echo.
pause

endlocal
exit /b

:LOWER
set "%2=%~1"
for %%a in (a b c d e f g h i j k l m n o p q r s t u v w x y z) do (
    call set "%2=%%%2:%%a=%%a%%"
)
exit /b
