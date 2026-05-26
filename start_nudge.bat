@echo off
echo === Sol Nudge Agent - Full Startup ===
echo.

echo [1/3] Starting Chrome with debug port...
start "" "D:\ClaudeExtentions\MCP\nudge-agent\start_chrome_debug.bat"
timeout /t 3 /nobreak >nul

echo [2/3] Starting Claude in WSL tmux...
wsl -d Ubuntu -- bash -c "tmux has-session -t nudge-agent 2>/dev/null || tmux new-session -d -s nudge-agent -c /mnt/d/ClaudeExtentions/MCP/nudge-agent 'claude'"
timeout /t 5 /nobreak >nul

echo [3/3] Starting nudge injector...
echo (Press Ctrl+C to stop)
echo.
cd /d D:\ClaudeExtentions\MCP\nudge-agent
py nudge_inject.py
