@echo off
echo === Nudge Agent (WSL tmux) ===
echo.

echo Step 1: Ensuring Claude is running in tmux...
wsl -d Ubuntu -- bash -c "tmux has-session -t nudge-agent 2>/dev/null || tmux new-session -d -s nudge-agent 'cd /mnt/d/ClaudeExtentions/MCP/nudge-agent && claude'"
echo   tmux session 'nudge-agent' is up.
echo.

echo Step 2: Starting nudge injector loop...
echo   (Ctrl+C to stop)
echo.
cd /d D:\ClaudeExtentions\MCP\nudge-agent
py nudge_inject.py
