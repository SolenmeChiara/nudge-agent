"""Centralized configuration loader for nudge-agent.

Reads config.json (gitignored, user-specific) with fallback to
config.example.json for defaults. All other modules import from here
instead of hardcoding paths, URLs, or credentials.

Usage:
    from config import CFG
    print(CFG.ntfy_url)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


@dataclass
class Config:
    # ntfy push notifications
    ntfy_url: str = "https://ntfy.sh/your-topic-here"

    # Memory MCP database
    memory_db: str = str(SCRIPT_DIR / "memory.db")

    # Memory MCP HTTP endpoints (optional, used when HTTP server is running)
    breath_hook_url: str = "http://localhost:3456/breath-hook?limit=8"
    phone_status_url: str = "http://localhost:3456/phone-status"
    phone_event_url: str = "http://localhost:3456/phone-event?hours=48&limit=12"
    breath_hook_timeout: int = 3

    # Chrome DevTools Protocol
    cdp_url: str = "http://localhost:9222"

    # tmux session name
    tmux_session: str = "nudge-agent"

    # Claude.ai session key env var name (the key itself lives in .env)
    claude_session_key_env: str = "CLAUDE_SESSION_KEY"

    # Timing
    min_nudge_gap_minutes: int = 20
    day_min_minutes: int = 20
    day_max_minutes: int = 60
    night_hours: int = 3

    # Context building
    recent_lookback_hours: int = 48
    recent_top_n: int = 6
    high_importance_top_n: int = 5
    prior_nudge_top_n: int = 3
    content_top_n: int = 5
    tail_messages: int = 3
    tail_text_limit: int = 200


def _load_config() -> Config:
    """Load config.json if it exists, else fall back to config.example.json."""
    cfg_path = SCRIPT_DIR / "config.json"
    if not cfg_path.exists():
        cfg_path = SCRIPT_DIR / "config.example.json"
    if not cfg_path.exists():
        return Config()

    with open(cfg_path, encoding="utf-8") as f:
        raw = json.load(f)

    # Map JSON keys to Config fields (JSON uses snake_case matching dataclass)
    kwargs = {}
    for fld in Config.__dataclass_fields__:
        if fld in raw:
            kwargs[fld] = raw[fld]
    return Config(**kwargs)


CFG = _load_config()
