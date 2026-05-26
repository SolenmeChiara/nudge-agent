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

SCRIPT_DIR = Path(__file__).resolve().parent
MEMORY_DB = Path(r"D:\ClaudeExtentions\MCP\Sol-Memory-mcp\memory.db")
CONTEXT_FILE = SCRIPT_DIR / "nudge_context.md"
TMUX_SESSION = "nudge-agent"

BREATH_HOOK_URL = "http://localhost:3456/breath-hook?limit=8"
BREATH_HOOK_TIMEOUT = 3
SQLITE_TIMEOUT = 10.0
RECENT_LOOKBACK_HOURS = 48
RECENT_TOP_N = 6
HIGH_IMPORTANCE_TOP_N = 5
PRIOR_NUDGE_TOP_N = 3
NUDGE_LABELS_CN = ["[上次]", "[上上次]", "[上上上次]", "[更早]"]

BUSY_POLL_INTERVAL = 10
BUSY_POLL_MAX = 60

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_stop = False


def _sigint(signum, frame):
    global _stop
    _stop = True
    print(f"\n[{_stamp()}] Ctrl+C — exiting after current sleep")


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- tmux helpers (call WSL from Windows) ----------

def _wsl(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["wsl", "-d", "Ubuntu"] + list(args),
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


# ---------- context assembly ----------

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

    parts = [
        f"当前时间：{now_str}（{weekday}）",
        "",
        claude_block,
        "",
        memory_block,
        "",
        "---",
        "",
        "你被唤醒了。根据以上信息，自由行动。你可以：",
        "- 给 Sol 发 nudge 推送",
        "- 往 Claude.ai 对话框注入消息",
        "- 整理或写入记忆",
        "- 拉取某个对话的完整内容深入了解",
        "- 或者如果一切正常，什么都不做",
        "",
        "完成所有行动后，直接等待下一次输入即可。",
    ]
    return "\n".join(parts)


# ---------- one cycle ----------

def one_cycle() -> bool:
    """Run one wakeup cycle. Returns True if injection succeeded."""
    print(f"[{_stamp()}] === wakeup cycle start ===")

    if not tmux_session_alive():
        print(f"[{_stamp()}] tmux session '{TMUX_SESSION}' not found!",
              file=sys.stderr)
        return False

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
    if hour >= 22 or hour < 7 or (hour == 7 and minute < 30):
        return 3 * 3600, "night"
    return random.randint(20 * 60, 60 * 60), "day"


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

        sleep_secs, mode = calc_sleep_seconds()
        wake = datetime.now() + timedelta(seconds=sleep_secs)
        print(f"[{_stamp()}] sleeping {sleep_secs//60} min ({mode}) — "
              f"next cycle at {wake:%H:%M:%S}")
        sleep_with_interrupt(sleep_secs)

    print(f"[{_stamp()}] nudge_inject exited after {cycle} cycle(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
