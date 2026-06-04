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
from config import CFG

SCRIPT_DIR = Path(__file__).resolve().parent
MEMORY_DB = Path(CFG.memory_db)
CONTEXT_FILE = SCRIPT_DIR / "nudge_context.md"
WAKEUP_OVERRIDE_FILE = SCRIPT_DIR / "next_wakeup.txt"
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

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_stop = False
_planned_next_wakeup = "(未计算)"
_wakeup_source = "随机"  # "随机" or "你上次自定义的"


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
    """Heuristic: CC is idle when any of the last few visible lines contains
    the interactive prompt character. Claude Code uses '❯' (U+276F) and
    renders a status bar + separator below it, so the prompt is typically
    2-3 lines above the bottom of the pane.
    """
    tail = tmux_capture_tail(8)
    if not tail:
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


# ---------- memory.db (read-only, same queries as nudge_cc.py) ----------

def _open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"file:{MEMORY_DB.as_posix()}?mode=ro",
        uri=True, timeout=SQLITE_TIMEOUT,
    )
    conn.row_factory = sqlite3.Row
    return conn


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
        when = (r["created_at"] or "")[:16].replace("T", " ")
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
            "FROM memories WHERE created_at >= ? AND key NOT LIKE '[nudge]%' "
            "ORDER BY created_at DESC LIMIT ?",
            (cutoff, RECENT_TOP_N),
        ).fetchall()
        high_db = conn.execute(
            "SELECT key, content, category, importance, created_at "
            "FROM memories WHERE importance >= 0.7 AND resolved = 0 "
            "AND key NOT LIKE '[nudge]%' "
            "ORDER BY created_at DESC LIMIT ?",
            (HIGH_IMPORTANCE_TOP_N,),
        ).fetchall()
        nudges = conn.execute(
            "SELECT content, created_at FROM memories "
            "WHERE key LIKE '[nudge]%' ORDER BY created_at DESC LIMIT ?",
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
            when = (n["created_at"] or "")[:16].replace("T", " ")
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
        when = (r["created_at"] or "")[:16].replace("T", " ")
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
    if data.get("current_app"):
        lines.append(f"当前 App: {data['current_app']}")
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
    claude_block = fetch_context.format_block(claude_convs, err)

    # Memory + nudge history
    memory_block = _build_memory_block(claude_convs)

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
    parts += [
        "",
        "---",
        "",
        "你被唤醒了。根据以上信息，自由行动。你可以：",
        "- 给 Sol 发 nudge 推送",
        "- 往 Claude.ai 对话框注入消息",
        "- 整理或写入记忆（查看 session 后觉得有值得记住的就整理一下）",
        "- 拉取某个对话的完整内容深入了解",
        "- 或者如果一切正常，什么都不做",
        "- 自定义下次唤醒时间（见下方说明）",
        "",
        f"本次唤醒类型：{_wakeup_source}",
        f"预计下次唤醒：{_planned_next_wakeup}（随机）",
        "如果你想修改下次唤醒时间，把时间戳写入 next_wakeup.txt（覆盖写入，不是追加）：",
        '  echo "2026-06-03 15:30" > next_wakeup.txt',
        "格式：YYYY-MM-DD HH:MM（24小时制，本地时间）。写入后该时间仅生效一次，之后恢复随机。",
        "注意：自定义期间你无法被唤醒——如果你设了 2 小时后，这段时间内你完全处于睡眠状态，无法监督 Sol 或发送任何提醒。",
        "",
        "完成所有行动后，直接等待下一次输入即可。",
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
        "你被唤醒了。上下文已更新到 nudge_context.md，请用 Read 工具读取后自由行动。"
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
        secs = 3 * 3600
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


def sleep_with_interrupt(seconds: float) -> None:
    end = time.monotonic() + seconds
    while not _stop:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


# ---------- main ----------

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Periodic wakeup injector for tmux-hosted nudge agent")
    parser.add_argument("--once", action="store_true",
                        help="Inject once and exit")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _sigint)
    try:
        signal.signal(signal.SIGTERM, _sigint)
    except (ValueError, AttributeError):
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

        # Wait for CC to finish processing before deciding sleep duration,
        # so CC has time to write next_wakeup.txt if it wants to override.
        wait_for_idle(max_polls=30)

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
