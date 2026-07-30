"""X (Twitter) notification monitor via Chrome DevTools Protocol.

Reads the X tab title from Chrome's CDP endpoint to extract the unread
notification count.  No page interaction, no DOM access -- just the tab
list metadata that CDP exposes at /json.

Threshold logic (default 5):
  - Track an "ack baseline" (the count when the agent last acknowledged).
  - When (current_count - baseline) >= threshold, surface it in context.
  - Agent acks by creating x_notif_ack (any content); consumed next cycle.
  - If current_count drops below baseline (user cleared notifs on X),
    baseline resets to 0 automatically.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from config import CFG

SCRIPT_DIR = Path(__file__).resolve().parent
# Runtime state lives under logs/states/ (see nudge_inject.py); git doesn't
# track empty directories, so create it before the first write.
STATE_DIR = SCRIPT_DIR / "logs" / "states"
try:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
STATE_FILE = STATE_DIR / "x_notif_state.json"
# Stays in the project root on purpose: the agent is told to `touch` it there.
ACK_FILE = SCRIPT_DIR / "x_notif_ack"
THRESHOLD = 5


def _read_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"ack_baseline": 0, "last_count": 0}


def _write_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def _get_x_tab_notif_count() -> int | None:
    """Query CDP /json for X tab title, return notification count.

    Returns int (0 if title has no number), or None if CDP is
    unreachable or no X tab exists.
    """
    try:
        req = urllib.request.Request(
            f"{CFG.cdp_url}/json",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            tabs = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, ConnectionError, OSError, json.JSONDecodeError):
        return None

    for tab in tabs:
        url = tab.get("url", "")
        if "x.com" not in url and "twitter.com" not in url:
            continue
        title = tab.get("title", "")
        m = re.match(r"\((\d+)\)", title)
        return int(m.group(1)) if m else 0

    return None


def check() -> str | None:
    """Return a context block if X notifications reached threshold, else None.

    Safe to call every cycle -- handles ack consumption and state updates.
    """
    state = _read_state()
    baseline = state.get("ack_baseline", 0)

    # Consume ack marker if agent left one
    if ACK_FILE.exists():
        try:
            ACK_FILE.unlink()
        except OSError:
            pass
        state["ack_baseline"] = state.get("last_count", 0)
        baseline = state["ack_baseline"]
        _write_state(state)

    count = _get_x_tab_notif_count()
    if count is None:
        return None

    state["last_count"] = count

    # Notifications cleared externally (user viewed on X) -> reset baseline
    if count < baseline:
        baseline = 0
        state["ack_baseline"] = 0

    _write_state(state)

    new = count - baseline
    if new >= THRESHOLD:
        return (
            f"## X 通知\n"
            f"@UndeBmutter 有 **{count}** 条未读通知"
            f"（新增 {new} 条）。\n"
            f"处理后执行: `touch /mnt/d/ClaudeExtentions/MCP/nudge-agent/x_notif_ack`"
        )
    return None
