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

    # Screen peek (agent-initiated iPhone screenshot via trigger mail).
    # smtp_user/password/mail_to live in config.json only (gitignored).
    peek_base_url: str = "http://localhost:3456"
    peek_smtp_host: str = "smtp.gmail.com"
    peek_smtp_port: int = 465
    peek_smtp_user: str = ""
    peek_smtp_password: str = ""
    peek_mail_to: str = ""
    peek_mail_subject: str = "sol-PEEK"

    # send_doc.py: inbox that rendered markdown documents get mailed to.
    # Deliberately separate from peek_mail_to, which triggers an iPhone
    # shortcut. Real address lives in config.json only (gitignored).
    doc_mail_to: str = ""

    # SwitchBot cloud API v1.1. device_id is the Hub 2 (indoor temp /
    # humidity / light level) — the default target of `status`. The
    # Curtain3 is driven by passing its own device id to
    # switchbot_client.send_command; the bedside lamp is Hue, not this.
    # token/secret live in config.json only (gitignored). Empty = "not
    # configured" -> switchbot_client degrades gracefully.
    switchbot_token: str = ""
    switchbot_secret: str = ""
    switchbot_device_id: str = ""
    switchbot_base_url: str = "https://api.switch-bot.com"

    # Philips Hue Bridge, local CLIP v2 API (LAN HTTPS, self-signed cert).
    # bridge_ip/application_key/client_key live in config.json only
    # (gitignored); they came out of a one-off link-button pairing against
    # the bridge — the pairing script is no longer in the repo, so to
    # re-pair you press the link button and POST to the bridge by hand.
    # Empty = "not configured" -> hue_client degrades gracefully.
    # client_key is the Entertainment-API streaming secret: unused by
    # hue_client.py today, kept because config.json carries it.
    hue_bridge_ip: str = ""
    hue_application_key: str = ""
    hue_client_key: str = ""
    hue_default_light_id: str = ""

    # tmux session name
    tmux_session: str = "nudge-agent"

    # Timing
    day_min_minutes: int = 20
    day_max_minutes: int = 60
    night_hours: int = 3

    # Context building
    recent_lookback_hours: int = 48
    recent_top_n: int = 6
    high_importance_top_n: int = 5
    prior_nudge_top_n: int = 3


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
