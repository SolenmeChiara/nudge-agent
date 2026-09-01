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
import hashlib
import json
import random
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
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
# Runtime droppings live under logs/ instead of littering the project root:
# logs/logs/ holds the append-only injector log, logs/states/ the
# incremental-render state files. git doesn't track empty directories, so a
# fresh clone has neither — create them here rather than letting the first
# write die on ENOENT.
LOG_DIR = SCRIPT_DIR / "logs" / "logs"
STATE_DIR = SCRIPT_DIR / "logs" / "states"
for _runtime_dir in (LOG_DIR, STATE_DIR):
    try:
        _runtime_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
LOG_FILE = LOG_DIR / "nudge_inject.log"
MEMORY_DB = Path(CFG.memory_db)
CONTEXT_FILE = SCRIPT_DIR / "nudge_context.md"
# Fast-changing half of the same snapshot: same wakeup, same data, minus the
# claude.ai conversation list and the memory block (slow-changing and bulky).
# CC reads this on ordinary wakeups and falls back to CONTEXT_FILE after a
# /compact or whenever the light version leaves it guessing.
LITE_FILE = SCRIPT_DIR / "nudge_context_lite.md"
MEMORY_STATE_FILE = STATE_DIR / "memory_state.json"
CONTEXT_STATE_FILE = STATE_DIR / "context_state.json"
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

# SwitchBot Hub 2 = the room's thermometer/hygrometer/light sensor. Pinned by
# id rather than leaning on CFG.switchbot_device_id so that repointing the
# config default at some other device (curtain, bulb) can't silently turn the
# 室内环境 line into nonsense. switchbot_client passes TIMEOUT=10 to requests
# as a scalar, which means 10s to connect *and* 10s to read (20s+ worst case,
# and DNS resolution isn't covered at all) — far too long for the wakeup
# pipeline, so the call runs in a throwaway thread and we give up on it after
# SWITCHBOT_ENV_TIMEOUT.
SWITCHBOT_HUB2_ID = "FD6A33DED601"
SWITCHBOT_ENV_TIMEOUT = 5.0

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


# ---------------------------------------------------------------------------
# Incremental rendering of the memory sections (memory_state.json)
#
# /breath-hook now returns the *complete* breath (memory_mcp passes budget=0),
# which fixed the swallowed-segment bug but means the same PINNED/CORE rows would
# be re-read in full every single wakeup. So the two memory sections are rendered
# like the conversation list already is: an entry whose content is byte-identical
# to what the previous context showed is omitted, and a trailing count line says
# how many were dropped. The state lives in memory_state.json, is deleted on
# injector startup (cycle #1 renders everything), and every read/write is
# best-effort — a missing or corrupt state file degrades to a full render and
# must never break the wakeup pipeline.
# ---------------------------------------------------------------------------

# Segments that are never collapsed: WORKING is the live promise list and WATCH
# holds the crisis observation windows. Both are short and both matter enough
# that a stale "unchanged, omitted" line is worse than the tokens it saves.
# CHRONICLE joined them 2026-08-30: it is memory_mcp's fixed day/week/month/year
# digest strip, and its whole point is being on screen every wakeup. Its rows
# barely ever change, so collapsing would hide it permanently after cycle #1 —
# exactly the opposite of what it is for. Costs ~2.5k characters per context;
# tune with BREATH_CHRONICLE_ROW_CHARS_FULL on the memory MCP side, or drop the
# name from this set to let it collapse like PINNED/CORE/TOP.
BREATH_ALWAYS_FULL = frozenset({"WORKING", "WATCH", "CHRONICLE"})

_BREATH_ID_RE = re.compile(r"^\[id:([^\]]+)\]")
_BREATH_HEADER_RE = re.compile(r"^===\s*(.+?)\s*===$")
# Decay weight is recomputed per request (a continuous function of age), so any
# row scoring above ~2 changes every cycle. Hashing it would mark those rows
# "changed" forever and defeat the collapse — strip it before digesting.
_BREATH_WEIGHT_RE = re.compile(r"\s*\[weight:[^\]]*\]")


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def _load_memory_state(path=None) -> dict:
    """Read memory_state.json. Any problem at all → {} (full render)."""
    path = MEMORY_STATE_FILE if path is None else path
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for section in ("breath", "recent48h"):
        sub = data.get(section)
        out[section] = sub if isinstance(sub, dict) else {}
    return out


def _save_memory_state(state: dict, path=None) -> None:
    """State is an optimization only — never let it break the wakeup pipeline."""
    path = MEMORY_STATE_FILE if path is None else path
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
    except (OSError, TypeError, ValueError):
        pass


def _breath_segment_name(line: str) -> str | None:
    """'=== WORKING (3/7) ===' → 'WORKING'; non-header lines → None."""
    m = _BREATH_HEADER_RE.match(line.strip())
    if not m:
        return None
    return m.group(1).split("(")[0].strip().split(" ")[0].upper()


def _omitted_note(ids: list[str]) -> str:
    """Trailing count line for a breath segment whose rows were collapsed."""
    shown = ids[:12]
    tail = "" if len(ids) == len(shown) else f" 等 {len(ids)} 条"
    return (
        f"（另有 {len(ids)} 条无变更而被省略，为预期行为；"
        f"全文按 id 用 extmcp_get_memory 拉取：{', '.join(shown)}{tail}）"
    )


def render_breath_incremental(text: str, prev: dict | None) -> tuple[str, dict]:
    """Collapse unchanged breath rows. Returns (rendered_text, new_breath_state).

    `text` is the raw /breath-hook payload. `prev` maps mem_id → line hash as of
    the last rendered context; pass None/{} for a full render. Lines carrying no
    ``[id:mem_xxx]`` prefix (segment headers, the scent easter egg, blank lines)
    always survive verbatim, and segments listed in BREATH_ALWAYS_FULL are never
    collapsed. The returned state maps every id seen *this* time — including the
    omitted ones — so a row stays collapsed until its text actually changes.
    """
    prev = prev if isinstance(prev, dict) else {}
    new_state: dict[str, str] = {}
    out: list[str] = []
    body: list[str] = []          # lines of the segment currently being read
    segment: str | None = None
    omitted: list[str] = []
    anchor = [-1]                 # index in `body` of its header / last kept row

    def _flush() -> None:
        """Close the current segment: park its note right after the last row.

        Appending the note at the segment boundary instead would push it past
        the blank separator line — and, for the final segment, past the scent
        easter egg that memory_mcp tacks onto the end of the payload — so it
        would read as if it belonged to whatever comes next.
        """
        if omitted:
            body.insert(anchor[0] + 1, _omitted_note(omitted))
            omitted.clear()
        out.extend(body)
        body.clear()
        anchor[0] = -1

    for line in (text or "").splitlines():
        name = _breath_segment_name(line)
        if name is not None:
            _flush()
            segment = name
            anchor[0] = len(body)
            body.append(line)
            continue
        m = _BREATH_ID_RE.match(line.strip())
        if not m:
            body.append(line)
            continue
        mem_id = m.group(1)
        digest = _hash(_BREATH_WEIGHT_RE.sub("", line.strip(), count=1))
        new_state[mem_id] = digest
        if segment not in BREATH_ALWAYS_FULL and prev.get(mem_id) == digest:
            omitted.append(mem_id)
            continue
        anchor[0] = len(body)
        body.append(line)
    _flush()

    return "\n".join(out), new_state


def _row_identity(r) -> tuple[str, str]:
    """(state key, content hash) for a memory row of the recent-48h section."""
    created = (r["created_at"] or "").strip()
    key = (r["key"] or "").strip()
    content = (r["content"] or "").strip()
    return _hash(f"{created}|{key}"), _hash(content)


def _fmt_rows(rows, prev: dict | None = None) -> tuple[str, dict]:
    """Render memory rows in full. Returns (text, state_of_rows_seen).

    Content is no longer excerpted — the whole point of the incremental pass is
    that a memory can be shown complete once instead of clipped forever. When
    `prev` is a dict, rows whose content hash is unchanged since that state are
    dropped and replaced by a single trailing count line; pass None to render
    everything (the high-importance fallback path does this).
    """
    state: dict[str, str] = {}
    if not rows:
        return "  (无)", state
    lines = []
    skipped_keys: list[str] = []
    for r in rows:
        rkey, chash = _row_identity(r)
        state[rkey] = chash
        if prev is not None and prev.get(rkey) == chash:
            # Keep the human-readable key so the collapsed rows stay findable
            # even if a swallowed turn meant the full text was never read.
            k = " ".join((r["key"] or "").split())
            skipped_keys.append(k[:24] + "…" if len(k) > 24 else k)
            continue
        key = (r["key"] or "").strip().replace("\n", " ")
        content = (r["content"] or "").strip().replace("\n", " ")
        when = _utc_to_local_str(r["created_at"] or "")
        imp = r["importance"] or 0
        cat = r["category"] or ""
        lines.append(f"- [{when}|{cat}|imp={imp:.1f}] {key}: {content}")
    if skipped_keys:
        shown = "、".join(skipped_keys[:8])
        tail = " 等" if len(skipped_keys) > 8 else ""
        lines.append(
            f"（另有 {len(skipped_keys)} 条无变更而被省略，为预期行为。"
            f"被省略的：{shown}{tail}。"
            "可用 extmcp_search_memory / extmcp_get_memory 拉取"
        )
    if not lines:
        return "  (无)", state
    return "\n".join(lines), state


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

    state = _load_memory_state()
    new_state = dict(state)

    out.append("## 最近 48 小时记忆（来自 memory.db）\n")
    recent_text, recent_state = _fmt_rows(recent, prev=state.get("recent48h", {}))
    new_state["recent48h"] = recent_state
    out.append(recent_text)

    breath = _try_breath_hook()
    if breath:
        out.append("\n## 高权重记忆 \n")
        breath_text, breath_state = render_breath_incremental(
            breath, state.get("breath", {}))
        # Only overwrite the breath sub-state when a breath was actually
        # rendered — a failed hook must not wipe it and force a full re-render.
        new_state["breath"] = breath_state
        out.append(breath_text)
    else:
        out.append("\n## 未解决的高权重记忆 (memory.db fallback)\n")
        fallback_text, _ = _fmt_rows(high_db)
        out.append(fallback_text)

    _save_memory_state(new_state)

    # Prior nudges + activity-since-last
    out.append("\n## 近期 nudge 记录")
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
             "处理完这些消息后它们会被标记为已读。", ""]
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
        header = (f"## 你的笔记（mind/journal.md 尾巴 | "
                  f"最后更新 {mtime:%Y-%m-%d %H:%M}，{age}）")
        hint = "（每次唤醒便记得更新。此处只显示最后几行，篇幅太长可能会限制你能看到的条目数。）"
        return "\n".join([header, *tail, hint])
    except OSError:
        return None


def _num(v) -> float | None:
    """Coerce a SwitchBot status field to a number, rejecting bools/junk."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fetch_switchbot_env() -> str | None:
    """Room climate from the SwitchBot Hub 2 (lite context only).

    Best-effort in the strongest sense: any failure — no requests module, no
    token, cloud 5xx, a degraded {'ok': False} envelope, a status body without
    the sensor fields, or simply taking longer than SWITCHBOT_ENV_TIMEOUT —
    returns None and the section is omitted. The worker thread is a daemon, so
    a hung cloud call can never hold up the wakeup or the interpreter exit.
    """
    box: dict[str, object] = {}

    def _worker() -> None:
        try:
            import switchbot_client
            box["res"] = switchbot_client.get_status(SWITCHBOT_HUB2_ID)
        except Exception as e:  # ImportError, config problems, anything
            box["res"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(SWITCHBOT_ENV_TIMEOUT)

    res = box.get("res")
    if not isinstance(res, dict) or not res.get("ok"):
        return None
    status = res.get("status")
    if not isinstance(status, dict):
        return None

    bits: list[str] = []
    temp = _num(status.get("temperature"))
    if temp is not None:
        bits.append(f"温度 {temp:g}°C")
    hum = _num(status.get("humidity"))
    if hum is not None:
        bits.append(f"湿度 {hum:g}%")
    light = _num(status.get("lightLevel"))
    if light is not None:
        bits.append(f"光照 {light:g}/20")
    if not bits:
        return None
    return "## 室内环境\n" + " / ".join(bits)


def build_contexts() -> tuple[str, str]:
    """Assemble the wakeup context in two renderings: (full, lite).

    Every source is fetched exactly once here and the rendered text is shared
    between the two versions — _read_inbox() in particular marks rows seen as
    a side effect, so calling it twice per cycle would hide messages from
    whichever version was rendered second.

    full: byte-for-byte the historical nudge_context.md.
    lite: the fast-changing sections only, plus the room's climate. No
    claude.ai conversation list, no memory block.
    """
    now = datetime.now()
    weekday = WEEKDAY_CN[now.weekday()]
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    # Claude.ai conversations
    try:
        claude_convs, err = fetch_context.fetch_raw(
            limit=10, fetch_content=True,
            state_path=CONTEXT_STATE_FILE)
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
            "**Memory MCP 可能故障或未开启。"
            "考虑 nudge 告知 Sol 并持续提醒，直到确认恢复。**"
        )

    # Phone status (best-effort)
    phone_block = _fetch_phone_status()

    # Backend inbox (messages from frontstage / scheduled tasks)
    inbox_block = _read_inbox()

    # Phone event stream from iOS automations (best-effort; empty -> omitted)
    phone_events_block = _fetch_phone_events()

    # PC presence: idle time, foreground window, Chrome tabs (best-effort)
    try:
        pc_block = pc_status.check()
    except Exception:
        pc_block = None

    # X notifications (best-effort, requires Chrome remote debugging on 9222)
    try:
        x_block = x_notif.check()
    except Exception:
        x_block = None

    journal_block = _read_journal_tail()

    # Room climate from the Hub 2 — lite only (the full version's shape is
    # frozen for backward compatibility). Belt and braces: the helper already
    # swallows everything internally, but even Thread.start() can raise, and a
    # thermometer must never be able to cost us a whole wakeup.
    try:
        env_block = _fetch_switchbot_env()
    except Exception:
        env_block = None

    # ---- sections both versions carry, rendered once ----
    shared: list[str] = []
    if inbox_block:
        shared += ["", inbox_block]
    if phone_block:
        shared += ["", phone_block]
    else:
        shared += ["", "## 手机状态\n（本次唤醒未能获取 iPhone Shortcut 状态，可能 Memory MCP HTTP 未启动或超时）"]
    if phone_events_block:
        shared += ["", phone_events_block]
    if pc_block:
        shared += ["", pc_block]
    if x_block:
        shared += ["", x_block]
    if journal_block:
        shared += ["", journal_block]

    footer = [
        "",
        "---",
        "",
        f"本次唤醒类型：{_wakeup_source} | 预计下次唤醒：{_planned_next_wakeup}",
    ]

    full_parts = [
        f"当前时间：{now_str}（{weekday}）",
        "",
        claude_block,
        "",
        memory_block,
    ]
    full_parts += shared
    full_parts += footer
    full_parts += [
        "本次唤醒上下文构建已完成。你可以按 CLAUDE.md 扫描和判断执行什么工作，做完后剩下的时间是你的。"
        "睡前往 mind/journal.md 留一两行再等待。",
    ]

    lite_parts = [f"当前时间：{now_str}（{weekday}）"]
    if env_block:
        lite_parts += ["", env_block]
    lite_parts += shared
    lite_parts += footer
    lite_parts += [
        "本次唤醒上下文构建已完成。nudge_context_lite.md 已更新；"
        "你可以按 CLAUDE.md 扫描和判断执行什么工作，做完后剩下的时间是你的。"
        "睡前往 mind/journal.md 留一两行再等待。",
    ]

    return "\n".join(full_parts), "\n".join(lite_parts)


# ---------- one cycle ----------

def one_cycle() -> bool:
    """Run one wakeup cycle. Returns True if injection succeeded."""
    global _planned_next_wakeup
    print(f"[{_stamp()}] === wakeup cycle start ===")

    if not tmux_session_alive():
        print(f"[{_stamp()}] tmux session '{TMUX_SESSION}' not found!",
              file=sys.stderr)
        return False

    # Pre-calculate the tentative next wakeup so build_contexts can show it
    sleep_secs, _ = calc_sleep_seconds()
    tentative_wake = datetime.now() + timedelta(seconds=sleep_secs)
    _planned_next_wakeup = tentative_wake.strftime("%Y-%m-%d %H:%M")

    # Build context and write to file
    print(f"[{_stamp()}] building context...")
    t0 = time.time()
    ctx, lite = build_contexts()
    print(f"[{_stamp()}] context built in {time.time()-t0:.1f}s "
          f"({len(ctx)} chars, lite {len(lite)} chars)")
    CONTEXT_FILE.write_text(ctx, encoding="utf-8")
    LITE_FILE.write_text(lite, encoding="utf-8")

    # Wait for CC to be idle
    if not wait_for_idle():
        print(f"[{_stamp()}] CC still busy after polling, injecting anyway")

    # Send the short wakeup message — CC reads the context file itself
    wakeup = (
        "你被唤醒了，"
        "若先前未阅读 nudge_context.md，则先读这个文件。若已经阅读，仅查看 nudge_context_lite.md 即可。"
        "接下来的时间是你的。"
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
    is_night = (hour == 1 and minute >= 30) or (2 <= hour < 6) or (hour == 6 and minute < 40)
    if is_night:
        secs = CFG.night_hours * 3600
        day_start = now.replace(hour=6, minute=40, second=0, microsecond=0)
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
                f"（此消息走即时通道直达，nudge_context.md "
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
        _logf = open(LOG_FILE, "a", encoding="utf-8")
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

    # The tail-state file survives restarts, so without this the first context
    # after a restart would already be incremental. Clearing it here (after the
    # singleton guard, so a losing second instance can't wipe the winner's
    # state) makes cycle #1 always render the full conversation list.
    try:
        CONTEXT_STATE_FILE.unlink(missing_ok=True)
        print(f"[{_stamp()}] context_state.json cleared — "
              "cycle #1 will render the full conversation list")
    except OSError:
        pass

    # Same reasoning for the memory sections (breath rows + recent-48h rows):
    # cycle #1 after a restart shows them in full, later cycles collapse the
    # untouched ones.
    try:
        MEMORY_STATE_FILE.unlink(missing_ok=True)
        print(f"[{_stamp()}] memory_state.json cleared — "
              "cycle #1 will render the full memory sections")
    except OSError:
        pass

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
