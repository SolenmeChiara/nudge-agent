@echo off
echo === Sol Nudge Agent - Full Startup ===
echo.

echo [1/4] Starting Chrome with debug port...
start "" "D:\ClaudeExtentions\MCP\nudge-agent\start_chrome_debug.bat"
timeout /t 3 /nobreak >nul

echo [2/4] Starting Claude in WSL tmux...
wsl -d Ubuntu -- bash -c "tmux has-session -t nudge-agent 2>/dev/null || tmux new-session -d -s nudge-agent -c /mnt/d/ClaudeExtentions/MCP/nudge-agent 'claude'"
timeout /t 5 /nobreak >nul

echo [3/4] Starting nudge injector in background...
cd /d D:\ClaudeExtentions\MCP\nudge-agent
start "" /min py nudge_inject.py

echo [4/4] Attaching to Claude Code CLI...
echo.
echo   Ctrl+B then D to detach (agent keeps running)
echo.
wsl -d Ubuntu -- tmux attach -t nudge-agent
