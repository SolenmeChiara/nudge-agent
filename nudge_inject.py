"""Periodic wakeup injector for the tmux-hosted Claude Code nudge agent.

Architecture change from nudge_cc.py:
  OLD: this script calls `claude -p`, parses SKIP/nudge output, sends ntfy itself
  NEW: this script writes context to a file and pokes the *persistent* Claude Code
       instance living in a tmux session. CC has full tool access and decides
       what to do on its own (send ntfy, inject claude.ai, read memory, etc.)

Each cycle:
  1. Pull Claude.ai activity + memory.db context (reuses fetch_context / memory logic)
  2. Write the context block to ./nudge_context.md
  3. Wait for CC to be idle (not mid-turn)
  4. Send a short wakeup line via `wsl tmux send-keys`
  5. Sleep 20-60 min (day) or 3h (night), repeat

Run on Windows. The tmux session runs in WSL.
"""

from __future__ import annotations

import argparse
import json
import random
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# We share context-building logic with nudge_cc.py via fetch_context + direct
# memory.db reads.  The imports below are local modules in the same directory.
# ---------------------------------------------------------------------------
import fetch_context
import pc_status
import x_notif
from config import CFG

SCRIPT_DIR = Path(__file__).resolve().parent
MEMORY_DB = Path(CFG.memory_db)
CONTEXT_FILE = SCRIPT_DIR / "nudge_context.md"
WAKEUP_OVERRIDE_FILE = SCRIPT_DIR / "next_wakeup.txt"
JOURNAL_FILE = SCRIPT_DIR / "mind" / "journal.md"
JOURNAL_TAIL_LINES = 6
TMUX_SESSION = CFG.tmux_session

BREATH_HOOK_URL = CFG.breath_hook_url
PHONE_STATUS_URL = CFG.phone_status_url
BREATH_HOOK_TIMEOUT = CFG.breath_hook_timeout
SQLITE_TIMEOUT = 10.0
RECENT_LOOKBACK_HOURS = CFG.recent_lookback_hours
RECENT_TOP_N = CFG.recent_top_n
HIGH_IMPORTANCE_TOP_N = CFG.high_importance_top_n
PRIOR_NUDGE_TOP_N = CFG.prior_nudge_top_n
NUDGE_LABELS_CN = ["[上次]", "[上上次]", "[上上上次]", "[更早]"]

BUSY_POLL_INTERVAL = 10
BUSY_POLL_MAX = 60

# Auto-/compact: the tmux CC session is persistent and its history grows
# without bound, so between cycles (while CC is idle) we periodically send
# /compact — it keeps a summary, unlike /clear. CC can also request one
# early by touching request_compact when it feels its own context is long.
COMPACT_INTERVAL_HOURS = 6
COMPACT_REQUEST_FILE = SCRIPT_DIR / "request_compact"

# While sleeping between cycles, peek this often for a freshly-written override
# file (CC/Sol may decide the committed wake time is wrong mid-sleep).
OVERRIDE_RECHECK_INTERVAL = 30

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_stop = False
_planned_next_wakeup = "(未计算)"
_wakeup_source = "随机"  # "随机" or "你上次自定义的"
_last_compact_mono = time.monotonic()  # injector start counts as "fresh"


def _sigint(signum, frame):
    global _stop
    _stop = True
    print(f"\n[{_stamp()}] Ctrl+C — exiting after current sleep")


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- tmux helpers (call WSL from Windows) ----------

def _wsl(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["wsl"] + list(args),
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        timeout=timeout,
    )


def tmux_session_alive() -> bool:
    r = _wsl("tmux", "has-session", "-t", TMUX_SESSION)
    return r.returncode == 0


def tmux_capture_tail(lines: int = 10) -> str:
    r = _wsl("tmux", "capture-pane", "-t", TMUX_SESSION,
             "-p", "-S", str(-lines))
    return r.stdout.strip() if r.returncode == 0 else ""


def tmux_send(text: str) -> bool:
    """Send literal text + Enter to the tmux session."""
    r1 = _wsl("tmux", "send-keys", "-t", TMUX_SESSION, "-l", text)
    if r1.returncode != 0:
        return False
    r2 = _wsl("tmux", "send-keys", "-t", TMUX_SESSION, "Enter")
    return r2.returncode == 0


def is_cc_idle() -> bool:
    """CC is idle when the input prompt box is shown AND it is not actively
    processing.

    IMPORTANT: Claude Code keeps the '❯' (U+276F) input box visible even
    while a turn is running, so "prompt char present" alone is NOT a reliable
    idle signal — it is almost always present. The canonical busy marker is
    the spinner line that carries 'esc to interrupt'. So: busy if that marker
    is on screen; otherwise idle if a prompt box is present.

    The old prompt-only heuristic made wait_for_idle return ~immediately,
    which caused the override file (written near the END of a 60s+ turn) to be
    read before it existed, silently falling back to the random schedule.
    """
    tail = tmux_capture_tail(8)
    if not tail:
        return False
    if "esc to interrupt" in tail:
        return False
    for line in tail.splitlines():
        stripped = line.strip()
        if stripped.startswith("❯") or stripped.startswith(">"):
            return True
    return False


def wait_for_idle(max_polls: int = BUSY_POLL_MAX) -> bool:
    """Poll until CC appears idle. Returns False if timed out."""
    for i in range(max_polls):
        if _stop:
            return False
        if is_cc_idle():
            return True
        if i == 0:
            print(f"[{_stamp()}] CC is busy, waiting...")
        time.sleep(BUSY_POLL_INTERVAL)
    return False


def maybe_compact() -> None:
    """Send /compact to CC when due (fixed interval, or CC touched the
    request file). Runs between cycles only, and only while CC is idle, so an
    in-flight turn is never interrupted. A deferred request file survives to
    the next cycle, so nothing is lost when CC happens to be busy.
    """
    global _last_compact_mono
    requested = COMPACT_REQUEST_FILE.exists()
    elapsed_h = (time.monotonic() - _last_compact_mono) / 3600
    if not requested and elapsed_h < COMPACT_INTERVAL_HOURS:
        return
    reason = "CC requested" if requested else f"{COMPACT_INTERVAL_HOURS}h interval"
    if not is_cc_idle():
        print(f"[{_stamp()}] compact due ({reason}) but CC not idle, deferring")
        return
    if not tmux_send("/compact"):
        print(f"[{_stamp()}] /compact send failed!", file=sys.stderr)
        return
    print(f"[{_stamp()}] /compact sent ({reason})")
    _last_compact_mono = time.monotonic()
    COMPACT_REQUEST_FILE.unlink(missing_ok=True)
    # Let compaction finish before the next sleep is computed, so a wakeup
    # never lands mid-compact.
    time.sleep(5)
    wait_for_idle()


# ---------- memory.db (read-only, same queries as nudge_cc.py) ----------

# Probed once (first query) and cached for the process lifetime. The server
# adds `tier` to memories via a migration that may land after this injector is
# already running, so tier-aware SQL must fall back to the legacy form until a
# restart re-probes. See _memories_has_tier.
_tier_column_present: bool | None = None


def _open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"file:{MEMORY_DB.as_posix()}?mode=ro",
        uri=True, timeout=SQLITE_TIMEOUT,
    )
    conn.row_factory = sqlite3.Row
    return conn


def _memories_has_tier(conn: sqlite3.Connection) -> bool:
    """Whether the memories table has a `tier` column (probed once, cached).

    Uses PRAGMA table_info, which lists the schema and never raises for a
    missing column — unlike referencing `tier` in a WHERE clause, which would
    throw OperationalError. That keeps us from having to wrap real queries in a
    blanket try/except that could mask genuine errors. The result is cached in
    a module global; a process restart re-probes, which is exactly when the
    server-side migration is expected to have landed.
    """
    global _tier_column_present
    if _tier_column_present is None:
        cols = {row[1] for row in
                conn.execute("PRAGMA table_info(memories)").fetchall()}
        _tier_column_present = "tier" in cols
    return _tier_column_present


def _utc_to_local_str(ts: str) -> str:
    """Convert a UTC ISO timestamp to a local-time 'YYYY-MM-DD HH:MM' string.

    memory.db stores created_at as UTC — usually with a '+00:00' offset, but
    ancient rows are naive (no offset). A naive value is treated as UTC, then
    converted to this machine's local zone (Sol's Toronto time). Empty or
    unparseable input is returned unchanged. Kept self-contained (no import
    from fetch_context) so the injector stays standalone.
    """
    if not ts:
        return ts
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def _try_breath_hook() -> str | None:
    req = urllib.request.Request(
        BREATH_HOOK_URL, method="GET",
        headers={"Accept": "text/plain"},
    )
    try:
        with urllib.request.urlopen(req, timeout=BREATH_HOOK_TIMEOUT) as r:
            return r.read().decode("utf-8", "replace").strip() if r.status == 200 else None
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, ConnectionError, OSError):
        return None


def _fmt_rows(rows) -> str:
    if not rows:
        return "  (无)"
    lines = []
    for r in rows:
        key = (r["key"] or "").strip().replace("\n", " ")
        content = (r["content"] or "").strip().replace("\n", " ")
        excerpt = (content[:120] + "…") if len(content) > 120 else content
        when = _utc_to_local_str(r["created_at"] or "")
        imp = r["importance"] or 0
        cat = r["category"] or ""
        lines.append(f"- [{when}|{cat}|imp={imp:.1f}] {key}: {excerpt}")
    return "\n".join(lines)


def _build_memory_block(claude_convs_raw) -> str:
    out: list[str] = []
    with _open_db() as conn:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=RECENT_LOOKBACK_HOURS)).isoformat()
        recent = conn.execute(
            "SELECT key, content, category, importance, created_at "
            "FROM memories WHERE created_at >= ? "
            "AND NOT (category='nudge' OR key LIKE '[nudge]%') "
            "ORDER BY created_at DESC LIMIT ?",
            (cutoff, RECENT_TOP_N),
        ).fetchall()
        # Isolate archived/seabed memories from the fallback high-importance
        # list — but only when the tier column exists (older DBs / pre-migration
        # restarts have no such column, so we omit the filter there).
        tier_filter = (
            " AND COALESCE(tier,'') NOT IN ('archive','seabed')"
            if _memories_has_tier(conn) else ""
        )
        high_db = conn.execute(
            "SELECT key, content, category, importance, created_at "
            "FROM memories WHERE importance >= 0.7 AND resolved = 0 "
            "AND NOT (category='nudge' OR key LIKE '[nudge]%')"
            f"{tier_filter} "
            "ORDER BY created_at DESC LIMIT ?",
            (HIGH_IMPORTANCE_TOP_N,),
        ).fetchall()
        nudges = conn.execute(
            "SELECT content, created_at FROM memories "
            "WHERE (category='nudge' OR key LIKE '[nudge]%') "
            "ORDER BY created_at DESC LIMIT ?",
            (PRIOR_NUDGE_TOP_N,),
        ).fetchall()

    out.append("## 最近 48 小时记忆（来自 memory.db）\n")
    out.append(_fmt_rows(recent))

    breath = _try_breath_hook()
    if breath:
        out.append("\n## 高权重记忆 (来自 memory MCP /breath-hook)\n")
        out.append(breath)
    else:
        out.append("\n## 未解决的高权重记忆 (memory.db fallback)\n")
        out.append(_fmt_rows(high_db))

    # Prior nudges + activity-since-last
    out.append("\n## 你的近期 nudge 记录")
    if not nudges:
        out.append("  (无)")
    else:
        for i, n in enumerate(nudges):
            label = NUDGE_LABELS_CN[i] if i < len(NUDGE_LABELS_CN) else "[更早]"
            when = _utc_to_local_str(n["created_at"] or "")
            content = (n["content"] or "").strip().replace("\n", " ")
            excerpt = (content[:140] + "…") if len(content) > 140 else content
            out.append(f"{label} {when} → 「{excerpt}」")

    return "\n".join(out)


def _read_inbox() -> str | None:
    """Read pending backend_inbox messages, format them, and mark them seen.

    Returns a formatted block or None if the inbox is empty. Uses a writable
    connection (unlike the rest of this module which reads memory.db read-only).
    """
    try:
        conn = sqlite3.connect(str(MEMORY_DB), timeout=SQLITE_TIMEOUT)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
    except sqlite3.Error:
        return None

    try:
        rows = conn.execute(
            "SELECT id, created_at, source, message FROM backend_inbox "
            "WHERE status = 'pending' ORDER BY id ASC"
        ).fetchall()
    except sqlite3.Error:
        # table may not exist yet (old DB)
        conn.close()
        return None

    if not rows:
        conn.close()
        return None

    lines = ["## 收件箱（前台/定时任务发给你的消息）",
             "处理完这些消息后它们会被标记为已读。请根据内容行动（推送/注入/存记忆）。", ""]
    ids = []
    for r in rows:
        ids.append(r["id"])
        when = _utc_to_local_str(r["created_at"] or "")
        src = r["source"] or "unknown"
        msg = (r["message"] or "").strip()
        lines.append(f"- [{when} | 来源:{src}] {msg}")

    # Mark seen
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "UPDATE backend_inbox SET status='seen', seen_at=? WHERE id=?",
        [(now_iso, i) for i in ids],
    )
    conn.commit()
    conn.close()
    return "\n".join(lines)


# ---------- context assembly ----------

def _fetch_phone_status() -> str | None:
    """GET /phone-status from memory MCP. Returns a formatted block or None."""
    req = urllib.request.Request(
        PHONE_STATUS_URL, method="GET",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=BREATH_HOOK_TIMEOUT) as r:
            if r.status != 200:
                return None
            data = json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, ConnectionError, OSError, json.JSONDecodeError):
        return None

    if data.get("error"):
        return None

    ts = data.get("timestamp", "")
    ago = ""
    if ts:
        try:
            then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            delta_min = (datetime.now(timezone.utc) - then).total_seconds() / 60
            if delta_min < 60:
                ago = f"{int(delta_min)} 分钟前"
            elif delta_min < 1440:
                ago = f"{delta_min/60:.1f} 小时前"
            else:
                ago = f"{delta_min/1440:.0f} 天前"
        except (ValueError, TypeError):
            pass

    lines = [f"## 手机状态 (最近更新: {ago or ts})"]
    if data.get("battery_level") is not None:
        charging = " (充电中)" if data.get("battery_charging") else ""
        lines.append(f"电量: {data['battery_level']}%{charging}")
    if data.get("focus_mode"):
        lines.append(f"专注模式: {data['focus_mode']}")
    if data.get("device_locked") is not None:
        lines.append(f"屏幕: {'锁定中' if data['device_locked'] else '解锁使用中'}")
    if data.get("current_app"):
        lines.append(f"当前 App: {data['current_app']}")
    if data.get("now_playing"):
        lines.append(f"正在听: {data['now_playing']}")
    if data.get("screen_time_minutes") is not None:
        h, m = divmod(data["screen_time_minutes"], 60)
        lines.append(f"屏幕使用时间: 今天 {h}h{m:02d}m")
    if data.get("location"):
        lines.append(f"位置: {data['location']}")
    if data.get("weather") or data.get("temperature") is not None:
        wx = data.get("weather", "")
        temp = f"{data['temperature']}°C" if data.get("temperature") is not None else ""
        lines.append(f"天气: {' '.join(filter(None, [temp, wx]))}")
    if data.get("steps") is not None:
        lines.append(f"今日步数: {data['steps']}")
    if data.get("sleep_hours") is not None:
        lines.append(f"睡眠: {data['sleep_hours']:.1f}h")
    if data.get("heart_rate") is not None:
        lines.append(f"心率: {data['heart_rate']} bpm")
    if data.get("calendar_events"):
        lines.append(f"日程: {data['calendar_events']}")

    return "\n".join(lines) if len(lines) > 1 else None


# iOS Shortcuts automations POST these event names to /phone-event.
# Unknown names render as-is, so new automations work without touching this.
_PHONE_EVENT_LABELS = {
    "alarm_stopped": "闹钟停止（起床了）",
    "sleep_focus_on": "睡眠专注开启（准备睡了）",
    "sleep_focus_off": "睡眠专注关闭",
    "wifi_home_join": "连上家里 Wi-Fi（到家）",
    "wifi_home_leave": "离开家里 Wi-Fi（出门）",
    "charging_start": "开始充电",
    "charging_stop": "结束充电",
    "low_battery": "电量低",
}


def _fetch_phone_events() -> str | None:
    """GET /phone-event from memory MCP. Returns a timeline block or None.

    An empty stream is normal (no automations fired recently) — the block
    is simply omitted rather than rendered as an error."""
    req = urllib.request.Request(
        CFG.phone_event_url, method="GET",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=BREATH_HOOK_TIMEOUT) as r:
            if r.status != 200:
                return None
            data = json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, ConnectionError, OSError, json.JSONDecodeError):
        return None

    events = data.get("events") or []
    if not events:
        return None

    def _when(ev: dict) -> str:
        ts = ev.get("timestamp", "")
        try:
            return datetime.fromisoformat(
                ts.replace("Z", "+00:00")
            ).astimezone().strftime("%m-%d %H:%M")
        except (ValueError, TypeError):
            return ts[:16]

    # screen_share carries a whole screen's OCR text and may arrive often.
    # It renders as a one-line count + teaser so the agent can decide
    # whether to pull the full texts — attention stays opt-in.
    # poke (Sol's triple back-tap "hey, you there?") stays on the timeline,
    # one line per tap, with a short glimpse of what was on screen.
    shares = [ev for ev in events if ev.get("event") == "screen_share"]
    timeline = [ev for ev in events if ev.get("event") != "screen_share"]

    lines = ["## 手机事件 (最近 48h，新的在前)"]
    poked_with_screen = False
    for ev in timeline:
        name = ev.get("event", "?")
        if name == "poke":
            d = (ev.get("detail") or "").replace("\n", " ").strip()
            teaser = d[:60] + ("…" if len(d) > 60 else "")
            suffix = f"（当时屏幕:「{teaser}」）" if teaser else ""
            lines.append(f"- {_when(ev)} Sol 戳了你一下📱{suffix}")
            poked_with_screen = poked_with_screen or bool(teaser)
            continue
        label = _PHONE_EVENT_LABELS.get(name, name)
        detail = f"（{ev['detail']}）" if ev.get("detail") else ""
        lines.append(f"- {_when(ev)} {label}{detail}")
    if shares:
        newest = shares[0]
        teaser = (newest.get("detail") or "").replace("\n", " ")[:80]
        lines.append(
            f"- 屏幕分享 ×{len(shares)}（最新 {_when(newest)}:"
            f"「{teaser}…」）想看全文: "
            f"curl 'http://localhost:3456/phone-event?hours=48&limit=20'"
        )
    elif poked_with_screen:
        lines.append(
            "（poke 屏幕全文: curl 'http://localhost:3456/phone-event?hours=48&limit=20'）"
        )
    return "\n".join(lines) if len(lines) > 1 else None


def _read_journal_tail() -> str | None:
    """Last few non-empty lines of CC's own bedside journal (mind/journal.md).

    This is what yesterday's CC left for today's CC — personal continuity
    that survives /compact, unlike session history. The header carries the
    file's last-modified age so a time-blind reader knows how stale it is."""
    try:
        if not JOURNAL_FILE.exists():
            return None
        lines = [l for l in JOURNAL_FILE.read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        tail = lines[-JOURNAL_TAIL_LINES:]
        if not tail:
            return None
        mtime = datetime.fromtimestamp(JOURNAL_FILE.stat().st_mtime)
        age_h = (datetime.now() - mtime).total_seconds() / 3600
        age = f"{age_h/24:.1f} 天前" if age_h >= 48 else f"{age_h:.1f} 小时前"
        header = (f"## 你的睡前笔记（mind/journal.md 尾巴 | "
                  f"最后更新 {mtime:%Y-%m-%d %H:%M}，{age}）")
        hint = "（睡前记得更新。一两行就好——这里只显示最后几行，写太长会被截掉。）"
        return "\n".join([header, *tail, hint])
    except OSError:
        return None


def build_context() -> str:
    """Assemble the full wakeup context block."""
    now = datetime.now()
    weekday = WEEKDAY_CN[now.weekday()]
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    # Claude.ai conversations
    try:
        claude_convs, err = fetch_context.fetch_raw(limit=10, fetch_content=True)
    except Exception as e:
        claude_convs, err = None, f"{type(e).__name__}: {e}"
    if err:
        # Make degraded cycles greppable — without this line the only trace
        # of a claude.ai fetch failure is the context being suspiciously fast
        # and small (23% of historical cycles, per the 2026-07-02 audit).
        print(f"[{_stamp()}] claude.ai fetch DEGRADED: {err}")
    claude_block = fetch_context.format_block(claude_convs, err)

    # Memory + nudge history — degrade gracefully if memory.db is unreachable
    # so the wakeup still fires (and CC can see the failure and act on it per
    # CLAUDE.md's "异常情况" rule) instead of the whole cycle crashing silently.
    try:
        memory_block = _build_memory_block(claude_convs)
    except Exception as e:
        memory_block = (
            "## 记忆上下文\n"
            f"（读取 memory.db 失败：{type(e).__name__}: {e}）\n"
            "**Memory MCP 可能挂了——按 CLAUDE.md「异常情况」处理，"
            "考虑 nudge 告知 Sol 并持续提醒直到恢复。**"
        )

    # Phone status (best-effort)
    phone_block = _fetch_phone_status()

    # Backend inbox (messages from frontstage / scheduled tasks)
    inbox_block = _read_inbox()

    parts = [
        f"当前时间：{now_str}（{weekday}）",
        "",
        claude_block,
        "",
        memory_block,
    ]
    if inbox_block:
        parts += ["", inbox_block]
    if phone_block:
        parts += ["", phone_block]
    else:
        parts += ["", "## 手机状态\n（本次唤醒未能获取 iPhone Shortcut 状态，可能 Memory MCP HTTP 未启动或超时）"]

    # Phone event stream from iOS automations (best-effort; empty -> omitted)
    phone_events_block = _fetch_phone_events()
    if phone_events_block:
        parts += ["", phone_events_block]

    # PC presence: idle time, foreground window, Chrome tabs (best-effort)
    try:
        pc_block = pc_status.check()
    except Exception:
        pc_block = None
    if pc_block:
        parts += ["", pc_block]

    # X notifications (best-effort, requires Chrome remote debugging on 9222)
    try:
        x_block = x_notif.check()
    except Exception:
        x_block = None
    if x_block:
        parts += ["", x_block]

    journal_block = _read_journal_tail()
    if journal_block:
        parts += ["", journal_block]

    parts += [
        "",
        "---",
        "",
        f"本次唤醒类型：{_wakeup_source} | 预计下次唤醒：{_planned_next_wakeup}",
        "你被唤醒了。先值班（按 CLAUDE.md 扫描和判断），值完班剩下的时间是你的。"
        "睡前往 mind/journal.md 留一两行再等待。",
    ]
    return "\n".join(parts)


# ---------- one cycle ----------

def one_cycle() -> bool:
    """Run one wakeup cycle. Returns True if injection succeeded."""
    global _planned_next_wakeup
    print(f"[{_stamp()}] === wakeup cycle start ===")

    if not tmux_session_alive():
        print(f"[{_stamp()}] tmux session '{TMUX_SESSION}' not found!",
              file=sys.stderr)
        return False

    # Pre-calculate the tentative next wakeup so build_context can show it
    sleep_secs, _ = calc_sleep_seconds()
    tentative_wake = datetime.now() + timedelta(seconds=sleep_secs)
    _planned_next_wakeup = tentative_wake.strftime("%Y-%m-%d %H:%M")

    # Build context and write to file
    print(f"[{_stamp()}] building context...")
    t0 = time.time()
    ctx = build_context()
    print(f"[{_stamp()}] context built in {time.time()-t0:.1f}s "
          f"({len(ctx)} chars)")
    CONTEXT_FILE.write_text(ctx, encoding="utf-8")

    # Wait for CC to be idle
    if not wait_for_idle():
        print(f"[{_stamp()}] CC still busy after polling, injecting anyway")

    # Send the short wakeup message — CC reads the context file itself
    wakeup = (
        "你被唤醒了。上下文已更新到 nudge_context.md，请 Read 后先值班；"
        "值完班是你自己的时间。"
    )
    ok = tmux_send(wakeup)
    if ok:
        print(f"[{_stamp()}] wakeup injected into tmux")
    else:
        print(f"[{_stamp()}] tmux send-keys failed!", file=sys.stderr)

    print(f"[{_stamp()}] === wakeup cycle done ===")
    return ok


# ---------- sleep ----------

def calc_sleep_seconds() -> tuple[int, str]:
    now = datetime.now()
    hour, minute = now.hour, now.minute
    is_night = hour >= 22 or hour < 7 or (hour == 7 and minute < 30)
    if is_night:
        secs = CFG.night_hours * 3600
        day_start = now.replace(hour=7, minute=30, second=0, microsecond=0)
        if now.hour >= 22:
            day_start += timedelta(days=1)
        to_day_start = (day_start - now).total_seconds()
        if 0 < to_day_start < secs:
            return int(to_day_start), "night→dawn"
        return secs, "night"
    return random.randint(CFG.day_min_minutes * 60, CFG.day_max_minutes * 60), "day"


def read_wakeup_override() -> datetime | None:
    """Read and consume the CC-written override file.

    Returns a future datetime if valid, None otherwise. The file is always
    deleted after reading (one-shot).
    """
    if not WAKEUP_OVERRIDE_FILE.exists():
        return None
    try:
        raw = WAKEUP_OVERRIDE_FILE.read_text(encoding="utf-8-sig").strip()
        WAKEUP_OVERRIDE_FILE.unlink(missing_ok=True)
        if not raw:
            return None
        # Accept YYYY-MM-DD HH:MM or YYYY.MM.DD HH:MM
        raw = raw.replace(".", "-")
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M")
        now = datetime.now()
        if dt <= now:
            print(f"[{_stamp()}] override {raw} is in the past, ignoring")
            return None
        secs = (dt - now).total_seconds()
        if secs < 60:
            print(f"[{_stamp()}] override {raw} is less than 1 minute away, ignoring")
            return None
        return dt
    except (ValueError, OSError) as e:
        print(f"[{_stamp()}] override file invalid: {e}", file=sys.stderr)
        try:
            WAKEUP_OVERRIDE_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def deliver_urgent_messages() -> None:
    """Inject pending urgent inbox messages straight into CC's chat.

    The express lane: urgent rows skip the wakeup cycle entirely and land as
    a user message via tmux within one sleep-poll interval (~30s). Only rows
    successfully injected are marked seen — if CC is busy or tmux fails, the
    row stays pending and is retried next poll (and, as a final fallback,
    the regular inbox block picks it up at the next full wakeup).
    """
    try:
        conn = sqlite3.connect(str(MEMORY_DB), timeout=SQLITE_TIMEOUT)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        rows = conn.execute(
            "SELECT id, created_at, source, message FROM backend_inbox "
            "WHERE status = 'pending' AND priority = 'urgent' ORDER BY id ASC"
        ).fetchall()
    except sqlite3.Error:
        return

    if not rows:
        conn.close()
        return

    if not is_cc_idle():
        print(f"[{_stamp()}] urgent message(s) waiting but CC busy; will retry")
        conn.close()
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    for r in rows:
        when = _utc_to_local_str(r["created_at"] or "")
        src = r["source"] or "unknown"
        # tmux send-keys treats newlines as submits — flatten to one line
        msg = " / ".join(
            part.strip() for part in (r["message"] or "").splitlines()
            if part.strip()
        )
        text = (f"[紧急插播 · 来自 {src} · {when}] {msg} "
                f"（此消息走即时通道直达，无需等唤醒周期；nudge_context.md "
                f"未刷新，处理完不用管收件箱。）")
        if tmux_send(text):
            conn.execute(
                "UPDATE backend_inbox SET status='seen', seen_at=? WHERE id=?",
                (now_iso, r["id"]),
            )
            conn.commit()
            print(f"[{_stamp()}] urgent #{r['id']} from {src} injected into CC")
            time.sleep(2)  # let CC's input settle between messages
        else:
            print(f"[{_stamp()}] urgent #{r['id']} tmux send failed; stays pending",
                  file=sys.stderr)
            break
    conn.close()


def sleep_with_interrupt(seconds: float) -> None:
    """Sleep until the target time, but periodically peek for a NEW override.

    When the injector commits to a sleep it has already consumed (deleted) the
    override file. If CC — or Sol, by talking to the tmux session directly —
    decides the committed wake time is wrong and writes a fresh next_wakeup.txt
    mid-sleep, we pick it up here (within OVERRIDE_RECHECK_INTERVAL seconds) and
    re-target the wake on the fly. Without this, the new file would sit unread
    until the current sleep expired, so a "1 hour is too long, wake me in 20
    min" correction couldn't shorten a sleep already underway.

    The same poll cadence drives the urgent-message express lane.
    """
    global _wakeup_source, _planned_next_wakeup
    end = time.monotonic() + seconds
    last_check = time.monotonic()
    while not _stop:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))
        now_mono = time.monotonic()
        if now_mono - last_check >= OVERRIDE_RECHECK_INTERVAL:
            last_check = now_mono
            deliver_urgent_messages()
            new_dt = read_wakeup_override()
            if new_dt:
                new_secs = max(0.0, (new_dt - datetime.now()).total_seconds())
                end = time.monotonic() + new_secs
                _wakeup_source = "你上次自定义的"
                _planned_next_wakeup = new_dt.strftime("%Y-%m-%d %H:%M")
                print(f"[{_stamp()}] mid-sleep override → re-targeting wake to "
                      f"{new_dt:%Y-%m-%d %H:%M} ({int(new_secs)//60} min from now)")


# ---------- main ----------

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    # Tee stdout/stderr to a log file. The injector usually runs in a
    # minimized console whose output is never seen — without this, override
    # decisions ("accepted" / "in the past, ignoring") leave no record on disk.
    class _Tee:
        def __init__(self, *streams):
            self._streams = streams

        def write(self, s):
            for st in self._streams:
                try:
                    st.write(s)
                    st.flush()
                except Exception:
                    pass

        def flush(self):
            for st in self._streams:
                try:
                    st.flush()
                except Exception:
                    pass

    try:
        _logf = open(SCRIPT_DIR / "nudge_inject.log", "a", encoding="utf-8")
        _logf.write(f"\n===== injector started {_stamp()} =====\n")
        _logf.flush()
        sys.stdout = _Tee(sys.stdout, _logf)
        sys.stderr = _Tee(sys.stderr, _logf)
    except OSError:
        pass

    # Singleton guard: bind a localhost port for the process lifetime. A second
    # instance fails the bind and exits instead of racing this one on
    # next_wakeup.txt and double-waking CC (2026-07-13: two injectors ran in
    # parallel all morning after a manual start + start_nudge.bat start).
    global _singleton_sock
    try:
        _singleton_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _singleton_sock.bind(("127.0.0.1", 48765))
        _singleton_sock.listen(1)
    except OSError:
        print(f"[{_stamp()}] another injector instance is already running "
              "(port 48765 in use) — exiting to avoid double wakeups")
        return 1

    parser = argparse.ArgumentParser(
        description="Periodic wakeup injector for tmux-hosted nudge agent")
    parser.add_argument("--once", action="store_true",
                        help="Inject once and exit")
    args = parser.parse_args()

    global _wakeup_source

    signal.signal(signal.SIGINT, _sigint)
    try:
        signal.signal(signal.SIGTERM, _sigint)
    except (ValueError, AttributeError):
        pass

    # Clear stale override from a previous session on startup
    if WAKEUP_OVERRIDE_FILE.exists():
        print(f"[{_stamp()}] clearing stale override from previous session")
        try:
            WAKEUP_OVERRIDE_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    cycle = 0
    while not _stop:
        cycle += 1
        print(f"\n[{_stamp()}] ----- cycle #{cycle} -----")
        try:
            one_cycle()
        except Exception as e:
            import traceback
            print(f"[{_stamp()}] cycle crashed: {e}", file=sys.stderr)
            traceback.print_exc()

        if args.once or _stop:
            break

        # Wait for CC to actually start processing (it goes non-idle once it
        # begins reading context), then wait for it to finish. This avoids the
        # race where wait_for_idle returns instantly because CC hasn't started
        # yet, causing us to miss a late-written override.
        for _ in range(10):
            if not is_cc_idle():
                break
            time.sleep(3)
        wait_for_idle(max_polls=60)

        # Periodic history compaction, now that CC has finished its turn
        maybe_compact()

        # Check for CC override
        override_dt = read_wakeup_override()
        if override_dt:
            sleep_secs = int((override_dt - datetime.now()).total_seconds())
            wake = override_dt
            mode = "cc-override"
            _wakeup_source = "你上次自定义的"
            print(f"[{_stamp()}] CC override accepted → "
                  f"next wakeup at {wake:%Y-%m-%d %H:%M}")
        else:
            sleep_secs, mode = calc_sleep_seconds()
            wake = datetime.now() + timedelta(seconds=sleep_secs)
            _wakeup_source = "随机"

        print(f"[{_stamp()}] sleeping {sleep_secs//60} min ({mode}) — "
              f"next cycle at {wake:%H:%M:%S}")
        sleep_with_interrupt(sleep_secs)

    print(f"[{_stamp()}] nudge_inject exited after {cycle} cycle(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
