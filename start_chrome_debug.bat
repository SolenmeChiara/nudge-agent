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
REM Safe to leave this Chrome running alongside Sol's normal Chrome — they
REM use different user-data-dirs so they won't fight over profile locks.

"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --remote-debugging-port=9222 ^
  --user-data-dir="D:\ClaudeExtentions\MCP\nudge-agent\chrome-data" ^
  --restore-last-session ^
  https://claude.ai/
