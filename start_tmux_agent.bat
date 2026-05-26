@echo off
REM Start nudge-agent as a persistent Claude Code instance inside WSL tmux.
REM The tmux session stays alive even after this window closes.
REM
REM To attach:  wsl tmux attach -t nudge-agent
REM To detach:  Ctrl+B then D
REM To kill:    wsl tmux kill-session -t nudge-agent

echo Killing existing session (if any)...
wsl tmux kill-session -t nudge-agent 2>NUL

echo Starting nudge-agent in WSL tmux...
wsl -d Ubuntu tmux new-session -d -s nudge-agent "cd /mnt/d/ClaudeExtentions/MCP/nudge-agent && claude"

echo.
echo  nudge-agent started in tmux session 'nudge-agent'
echo  To attach:  wsl tmux attach -t nudge-agent
echo  To detach:  Ctrl+B then D
echo.
pause
