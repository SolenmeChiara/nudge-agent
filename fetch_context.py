"""Fetch recent Claude.ai conversation activity for nudge context.

Pulls the top N most-recently-updated conversations from the user's main
Claude.ai org and formats them as a plain-text block suitable for direct
inclusion in a Claude Code prompt.

Default: print to stdout so the orchestrator can capture it. Pass --limit N
to change how many conversations are shown.

Zero external deps (stdlib only).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE = "https://claude.ai/api"
TIMEOUT = 30
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _load_env_var(name: str) -> str | None:
    val = os.environ.get(name)
    if val:
        return val.strip()
    for path in [
        SCRIPT_DIR / ".env",
        SCRIPT_DIR.parent / ".env",
        SCRIPT_DIR.parent.parent / ".env",
        SCRIPT_DIR.parent.parent.parent / ".env",
    ]:
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("set "):
                line = line[4:].lstrip()
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == name:
                return v.strip().strip('"').strip("'")
    return None


def _get(path: str, session_key: str) -> tuple[int, object]:
    req = urllib.request.Request(
        f"{BASE}{path}", method="GET",
        headers={
            "Cookie": f"sessionKey={session_key}",
            "User-Agent": UA,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://claude.ai",
            "Referer": "https://claude.ai/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        # Connection refused / DNS / timeout / reset. Callers treat status 0 as
        # failure. Without this a transient blip on a "best-effort" tail fetch
        # would raise straight through fetch_raw and lose the whole conv list.
        return 0, f"connection error: {e}"
    except json.JSONDecodeError as e:
        return 0, f"bad json: {e}"


def _pick_main_org(orgs: list) -> dict | None:
    """Prefer the chat/claude_max org over the api-only one."""
    chat_orgs = [o for o in orgs if "chat" in (o.get("capabilities") or [])]
    return (chat_orgs[0] if chat_orgs else (orgs[0] if orgs else None))


def _utc_to_local(iso: str) -> str:
    """Convert a UTC ISO timestamp to local time string like '14:05 (本地)'."""
    try:
        utc_dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        local_dt = utc_dt.astimezone()
        return local_dt.strftime("%Y-%m-%d %H:%M") + " (本地)"
    except (ValueError, AttributeError):
        return iso[:16].replace("T", " ")


def _humanize_delta(now: datetime, then_iso: str) -> str:
    """Return e.g. '35 分钟前' / '2.3 小时前' / '1 天前'."""
    try:
        then = datetime.fromisoformat(then_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "时间未知"
    delta = (now - then).total_seconds()
    if delta < 60:
        return f"{int(delta)} 秒前"
    if delta < 3600:
        return f"{int(delta / 60)} 分钟前"
    if delta < 86400:
        return f"{delta / 3600:.1f} 小时前"
    return f"{delta / 86400:.1f} 天前"


CONTENT_TOP_N = 5
TAIL_MESSAGES = 3
TAIL_TEXT_LIMIT = 200
TAIL_RATE_DELAY = 1.0


def fetch_conversation_tail(
    org_uuid: str,
    conv_uuid: str,
    session_key: str,
    n_messages: int = TAIL_MESSAGES,
) -> list[dict]:
    """Fetch the last *n_messages* from a single conversation.

    Returns [{"sender": "Sol"|"Claude", "text": "截取前200字…"}].
    Empty list on any failure — tail is a nice-to-have.
    """
    path = (
        f"/organizations/{org_uuid}/chat_conversations/{conv_uuid}"
        f"?tree=True&rendering_mode=messages&render_all_tools=true"
    )
    status, data = _get(path, session_key)
    if status != 200 or not isinstance(data, dict):
        return []

    messages = data.get("chat_messages") or []
    tail = messages[-n_messages:] if messages else []

    result: list[dict] = []
    for msg in tail:
        sender_raw = msg.get("sender", "unknown")
        text = (msg.get("text") or "").strip()
        if not text:
            blocks = msg.get("content") or []
            parts = [b.get("text", "") for b in blocks
                     if isinstance(b, dict) and b.get("type") == "text"]
            text = " ".join(parts).strip()
        if not text:
            continue

        text = text.replace("\n", " ")
        if len(text) > TAIL_TEXT_LIMIT:
            text = text[:TAIL_TEXT_LIMIT] + "…"

        label = "Sol" if sender_raw == "human" else "Claude"
        result.append({"sender": label, "text": text})

    return result


def fetch_conversation_deep(
    conv_uuid: str,
    n_messages: int = 30,
    text_limit: int = 600,
) -> str:
    """Fetch a single conversation's recent messages in detail.

    Returns a formatted markdown block ready for stdout/prompt inclusion.
    Unlike fetch_conversation_tail (which truncates to 200 chars for the
    overview), this gives longer excerpts for deep reading.
    """
    key = _load_env_var("CLAUDE_SESSION_KEY")
    if not key:
        return "(无 CLAUDE_SESSION_KEY)"
    status, orgs = _get("/organizations", key)
    if status != 200 or not isinstance(orgs, list):
        return f"(获取 organizations 失败 HTTP {status})"
    org = _pick_main_org(orgs)
    if not org:
        return "(没有 chat-capable organization)"
    org_uuid = org["uuid"]

    path = (
        f"/organizations/{org_uuid}/chat_conversations/{conv_uuid}"
        f"?tree=True&rendering_mode=messages&render_all_tools=true"
    )
    status, data = _get(path, key)
    if status != 200 or not isinstance(data, dict):
        return f"(获取对话失败 HTTP {status})"

    title = (data.get("name") or "").strip() or "(untitled)"
    messages = data.get("chat_messages") or []
    tail = messages[-n_messages:]

    lines = [f"## 对话深拉：{title}", f"共 {len(messages)} 条消息，显示最近 {len(tail)} 条", ""]
    for msg in tail:
        sender_raw = msg.get("sender", "unknown")
        label = "Sol" if sender_raw == "human" else "Claude"
        text = (msg.get("text") or "").strip()
        if not text:
            blocks = msg.get("content") or []
            parts = [b.get("text", "") for b in blocks
                     if isinstance(b, dict) and b.get("type") == "text"]
            text = " ".join(parts).strip()
        if not text:
            continue
        text = text.replace("\n", " ")
        if len(text) > text_limit:
            text = text[:text_limit] + "…"
        ts = msg.get("created_at", "")
        local_ts = _utc_to_local(ts) if ts else ""
        lines.append(f"[{label}] ({local_ts}) {text}")
        lines.append("")

    return "\n".join(lines)


def _load_tail_state(path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_tail_state(path, state: dict) -> None:
    # State is an optimization only — never let it break the wakeup pipeline.
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def fetch_raw(
    limit: int = 10,
    fetch_content: bool = True,
    content_top_n: int = CONTENT_TOP_N,
    state_path: str | Path | None = None,
) -> tuple[list[dict] | None, str | None]:
    """Pull raw conversation list. Returns (sorted_convs_top_limit, error_msg).

    On success: (list_of_dicts, None) — already sorted by updated_at desc,
    truncated to `limit`. Each dict carries at least uuid/name/updated_at/created_at.
    When *fetch_content* is True, the top *content_top_n* conversations also
    get a ``_tail`` key with the last few messages (see fetch_conversation_tail).
    On any failure: (None, error_msg) where error_msg is a short human-readable string.

    *state_path* (opt-in, used by the injector) points to a JSON file mapping
    conv uuid -> updated_at as of the last rendered context. Conversations whose
    updated_at is unchanged since then get ``_unchanged=True`` instead of a
    ``_tail`` — no tail HTTP request is made for them, and format_block omits
    the entry entirely (a trailing count line replaces the omitted entries).
    Conversations beyond *content_top_n* never get a tail but are tracked the
    same way, so an idle conversation collapses regardless of rank. Within the
    tail window, only conversations whose tail was actually shown (or carried
    over unchanged) are recorded, so a failed tail fetch never suppresses the
    next cycle's preview. The injector deletes the state file on startup, which
    makes the first cycle after a restart render the full list.
    """
    key = _load_env_var("CLAUDE_SESSION_KEY")
    if not key:
        return None, "无 CLAUDE_SESSION_KEY"
    status, orgs = _get("/organizations", key)
    if status != 200 or not isinstance(orgs, list):
        return None, f"获取 organizations 失败 HTTP {status}"
    org = _pick_main_org(orgs)
    if not org:
        return None, "没有 chat-capable organization"
    org_uuid = org["uuid"]
    status, convs = _get(f"/organizations/{org_uuid}/chat_conversations", key)
    if status != 200 or not isinstance(convs, list):
        return None, f"获取 conversations 失败 HTTP {status}"

    def _k(c):
        return c.get("updated_at") or c.get("created_at") or ""
    convs.sort(key=_k, reverse=True)
    top = convs[:limit]

    if fetch_content and key:
        prev_state = _load_tail_state(state_path) if state_path else {}
        new_state: dict[str, str] = {}
        pending: list[dict] = []
        for idx, c in enumerate(top):
            cuuid = c.get("uuid")
            if not cuuid:
                continue
            upd = c.get("updated_at") or ""
            if upd and prev_state.get(cuuid) == upd:
                c["_unchanged"] = True
                new_state[cuuid] = upd
            elif idx < content_top_n:
                pending.append(c)
            else:
                # Beyond the tail window there is nothing to fetch, but the
                # timestamp is still recorded so an idle conversation collapses
                # to the omitted-count line on the next cycle like the rest.
                new_state[cuuid] = upd
        for i, c in enumerate(pending):
            c["_tail"] = fetch_conversation_tail(org_uuid, c["uuid"], key)
            if c["_tail"]:
                new_state[c["uuid"]] = c.get("updated_at") or ""
            if i < len(pending) - 1:
                time.sleep(TAIL_RATE_DELAY)
        if state_path:
            _save_tail_state(state_path, new_state)

    return top, None


def _project_tag(conv: dict) -> str:
    """Return a leading tag like '[⚡work!] ' or '[Project: chating] ' or '' (none).

    The Claude.ai listing endpoint returns `project: {"uuid": ..., "name": ...}`
    when a conversation belongs to a Claude Project, or `None` otherwise.
    A case-insensitive "work" substring promotes the conversation to the
    high-priority ⚡ band; everything else just gets a neutral Project label.
    """
    project = conv.get("project")
    if not isinstance(project, dict):
        return ""
    name = (project.get("name") or "").strip()
    if not name:
        return "[Project] "
    if "work" in name.lower():
        return f"[⚡work!] "
    return f"[Project: {name}] "


def format_block(convs: list[dict] | None, error_msg: str | None,
                 total_count: int | None = None) -> str:
    """Format a conversation list as a markdown block for prompt inclusion."""
    if error_msg or convs is None:
        return f"## 最近 Claude.ai 对话\n\n({error_msg or '未知错误'})\n"
    now = datetime.now(timezone.utc)
    lines = [
        "## 最近 Claude.ai 对话 (按最后活跃时间倒序)",
        "⚡working! 项目的对话优先关注",
        "",
    ]
    skipped = 0
    for c in convs:
        if c.get("_unchanged"):
            skipped += 1
            continue
        title = (c.get("name") or "").replace("\n", " ").strip() or "(untitled)"
        upd = c.get("updated_at") or ""
        ago = _humanize_delta(now, upd) if upd else "时间未知"
        local_ts = _utc_to_local(upd) if upd else "时间未知"
        tag = _project_tag(c)
        cuuid = c.get("uuid") or ""
        lines.append(f"- [{local_ts}, {ago}] {tag}{title}")
        if cuuid:
            lines.append(f"  conv-id: {cuuid}")
        tail = c.get("_tail")
        if tail:
            lines.append("  最近内容：")
            for msg in tail:
                lines.append(f"  [{msg['sender']}] {msg['text']}")
    if skipped:
        lines += [
            "",
            f"（另有 {skipped} 条对话无更新，已整条省略。这是预期行为；"
            "需要对应 conv-id 时，运行 fetch_context.py。）",
        ]
    if total_count is not None:
        lines += ["", f"(账号共 {total_count} 条对话，仅显示前 {len(convs)} 条)"]
    return "\n".join(lines)


def fetch(limit: int = 10) -> str:
    """Convenience wrapper that returns a ready-to-paste markdown block."""
    # For the count footer we'd need the un-truncated length — refetch is wasteful,
    # so just call fetch_raw with a large limit and slice.
    key = _load_env_var("CLAUDE_SESSION_KEY")
    if not key:
        return "## 最近 Claude.ai 对话\n\n(无 CLAUDE_SESSION_KEY，跳过此上下文)\n"
    status, orgs = _get("/organizations", key)
    if status != 200 or not isinstance(orgs, list):
        return f"## 最近 Claude.ai 对话\n\n(获取 organizations 失败：HTTP {status})\n"
    org = _pick_main_org(orgs)
    if not org:
        return "## 最近 Claude.ai 对话\n\n(没有 chat-capable organization)\n"
    org_uuid = org["uuid"]
    status, convs = _get(f"/organizations/{org_uuid}/chat_conversations", key)
    if status != 200 or not isinstance(convs, list):
        return f"## 最近 Claude.ai 对话\n\n(获取 conversations 失败：HTTP {status})\n"
    convs.sort(key=lambda c: c.get("updated_at") or c.get("created_at") or "",
               reverse=True)
    return format_block(convs[:limit], None, total_count=len(convs))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Fetch recent Claude.ai context")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--no-fetch-content", action="store_true",
                        help="Skip fetching message tails (faster)")
    parser.add_argument("--conv-id", type=str, default=None,
                        help="Deep-fetch a single conversation by UUID")
    parser.add_argument("--messages", type=int, default=30,
                        help="Number of messages to fetch in deep mode (default 30)")
    args = parser.parse_args()

    if args.conv_id:
        print(fetch_conversation_deep(args.conv_id, n_messages=args.messages))
    else:
        convs, err = fetch_raw(limit=args.limit,
                               fetch_content=not args.no_fetch_content)
        print(format_block(convs, err))
    return 0


if __name__ == "__main__":
    sys.exit(main())
