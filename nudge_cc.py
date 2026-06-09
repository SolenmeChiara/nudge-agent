"""Claude Code-driven nudge orchestrator.

DEPRECATED — kept for reference / fallback only. The live system runs
nudge_inject.py (a persistent tmux Claude Code instance); this single-shot
`claude -p` orchestrator is no longer wired into start_nudge.bat. Heads-up
before reactivating: _pick_inject_target reads `_project_name`, a key that
fetch_raw never sets (project info lives under the `project` dict), so its
work!-conversation preference has always silently fallen through to "newest".
Fix that first.

Each cycle:
  1. Pull Claude.ai recent activity via fetch_context.fetch()
  2. Pull recent + high-importance memories and prior nudges from memory.db
  3. Build a context block and pipe it to `claude -p` running in this directory
     (so Claude Code loads CLAUDE.md for persona instructions)
  4. If the response is not SKIP, POST to ntfy and log a memory row
  5. Sleep random 20-60 min, repeat — unless --once

Zero external deps (stdlib only). Run from any cwd; subprocess.run forces
cwd to this script's directory so CLAUDE.md gets picked up.
"""

from __future__ import annotations

import argparse
import json
import random
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fetch_context  # local module, same directory
from config import CFG

SCRIPT_DIR = Path(__file__).resolve().parent
MEMORY_DB = Path(CFG.memory_db)
NTFY_URL = CFG.ntfy_url

BREATH_HOOK_URL = CFG.breath_hook_url
BREATH_HOOK_TIMEOUT = CFG.breath_hook_timeout

MIN_NUDGE_GAP_MINUTES = CFG.min_nudge_gap_minutes
RECENT_LOOKBACK_HOURS = CFG.recent_lookback_hours
RECENT_TOP_N = CFG.recent_top_n
HIGH_IMPORTANCE_TOP_N = CFG.high_importance_top_n
PRIOR_NUDGE_TOP_N = CFG.prior_nudge_top_n

CC_TIMEOUT = 180
NTFY_TIMEOUT = 15
SQLITE_TIMEOUT = 10.0

NUDGE_LABELS_CN = ["[上次]", "[上上次]", "[上上上次]", "[更早]"]

# Tracks the local date (YYYY-MM-DD) of the last cycle so we can decide
# whether to resume the prior `claude -p` session or start fresh at midnight.
LAST_SESSION_DATE_FILE = SCRIPT_DIR / ".last_session_date"

_stop = False


def _sigint(signum, frame):
    global _stop
    _stop = True
    print(f"\n[{stamp()}] Ctrl+C — finishing current cycle then exit")


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- memory.db ----------

def open_db(readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{MEMORY_DB.as_posix()}?mode=ro",
                               uri=True, timeout=SQLITE_TIMEOUT)
    else:
        conn = sqlite3.connect(str(MEMORY_DB), timeout=SQLITE_TIMEOUT)
        conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    return conn


def last_nudge_at(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute(
        "SELECT created_at FROM memories WHERE key LIKE '[nudge]%' "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    try:
        return datetime.fromisoformat(row["created_at"])
    except ValueError:
        return None


def _try_breath_hook() -> str | None:
    """GET the memory MCP breath-hook endpoint. Returns plain text or None."""
    req = urllib.request.Request(
        BREATH_HOOK_URL,
        method="GET",
        headers={"Accept": "text/plain"},
    )
    try:
        with urllib.request.urlopen(req, timeout=BREATH_HOOK_TIMEOUT) as r:
            if r.status != 200:
                return None
            return r.read().decode("utf-8", "replace").strip()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            ConnectionError, OSError):
        return None


def _fetch_recent_memories(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=RECENT_LOOKBACK_HOURS)).isoformat()
    return conn.execute(
        "SELECT key, content, category, importance, created_at "
        "FROM memories WHERE created_at >= ? AND key NOT LIKE '[nudge]%' "
        "ORDER BY created_at DESC LIMIT ?",
        (cutoff, RECENT_TOP_N),
    ).fetchall()


def _fetch_high_importance(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT key, content, category, importance, created_at "
        "FROM memories WHERE importance >= 0.7 AND resolved = 0 "
        "AND key NOT LIKE '[nudge]%' "
        "ORDER BY created_at DESC LIMIT ?",
        (HIGH_IMPORTANCE_TOP_N,),
    ).fetchall()


def _fetch_prior_nudges(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT content, created_at FROM memories "
        "WHERE key LIKE '[nudge]%' ORDER BY created_at DESC LIMIT ?",
        (PRIOR_NUDGE_TOP_N,),
    ).fetchall()


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


def build_continuity_block(prior_nudges, claude_convs_raw) -> str:
    """Format prior nudges with [上次]/[上上次] labels and decide whether
    Sol has been active on Claude.ai *after* the last nudge.
    """
    out = ["## 你的近期 nudge 记录"]
    if not prior_nudges:
        out.append("  (无 — 这是第一次 nudge)")
        return "\n".join(out)

    for i, n in enumerate(prior_nudges):
        label = NUDGE_LABELS_CN[i] if i < len(NUDGE_LABELS_CN) else "[更早]"
        when = (n["created_at"] or "")[:16].replace("T", " ")
        content = (n["content"] or "").strip().replace("\n", " ")
        excerpt = (content[:140] + "…") if len(content) > 140 else content
        out.append(f"{label} {when} → 「{excerpt}」")

    # Activity-since-last-nudge check
    last_iso = prior_nudges[0]["created_at"]
    try:
        last_dt = datetime.fromisoformat(last_iso)
    except (ValueError, TypeError):
        last_dt = None

    if last_dt and claude_convs_raw:
        # Conversations updated after last nudge
        active = []
        for c in claude_convs_raw:
            upd_iso = c.get("updated_at") or ""
            try:
                upd_dt = datetime.fromisoformat(upd_iso.replace("Z", "+00:00"))
            except ValueError:
                continue
            if upd_dt > last_dt:
                active.append((c, upd_dt))
        if active:
            now = datetime.now(timezone.utc)
            top = active[0]  # already sorted desc by updated_at
            title = (top[0].get("name") or "").strip() or "(untitled)"
            ago = _humanize_delta(now, top[0].get("updated_at"))
            out.append(
                f"\nSol 在上次 nudge 之后 **在 Claude.ai 上活跃过** — "
                f"{len(active)} 个对话被更新过；最近一条「{title}」({ago})。"
            )
        else:
            out.append(
                "\nSol 在上次 nudge 之后 **没有在 Claude.ai 上活跃**。"
            )
    elif last_dt and not claude_convs_raw:
        out.append("\n(无法判断 Claude.ai 活跃状态 — 对话数据未取到)")

    return "\n".join(out)


def _humanize_delta(now: datetime, then_iso: str) -> str:
    try:
        then = datetime.fromisoformat((then_iso or "").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return "时间未知"
    delta = (now - then).total_seconds()
    if delta < 60:
        return f"{int(delta)} 秒前"
    if delta < 3600:
        return f"{int(delta / 60)} 分钟前"
    if delta < 86400:
        return f"{delta / 3600:.1f} 小时前"
    return f"{delta / 86400:.1f} 天前"


def fetch_memory_context(claude_convs_raw=None) -> str:
    """Return a markdown block summarising recent memories + prior nudges,
    enhanced with continuity / activity-since-last-nudge analysis when
    Claude.ai conversation data is available.

    For the high-importance section we prefer the memory MCP's `/breath-hook`
    endpoint (curated pinned + decay-weighted unresolved) when reachable;
    fall back to a raw importance≥0.7 query against memory.db.
    """
    out: list[str] = []

    with open_db(readonly=True) as conn:
        recent = _fetch_recent_memories(conn)
        high_db = _fetch_high_importance(conn)
        nudges = _fetch_prior_nudges(conn)

    out.append("## 最近 48 小时记忆（来自 memory.db）\n")
    out.append(_fmt_rows(recent))

    breath_text = _try_breath_hook()
    if breath_text:
        out.append("\n## 高权重记忆 (来自 memory MCP /breath-hook)\n")
        out.append(breath_text)
    else:
        out.append("\n## 未解决的高权重记忆 (memory.db fallback — "
                   "/breath-hook 不可达)\n")
        out.append(_fmt_rows(high_db))

    out.append("\n" + build_continuity_block(nudges, claude_convs_raw))
    return "\n".join(out)


def insert_nudge_memory(content: str, when: datetime) -> str:
    iso = when.isoformat()
    ts_compact = when.strftime("%Y%m%d%H%M%S") + f"{when.microsecond:06d}"
    rand = secrets.randbelow(10000)
    mem_id = f"mem_{ts_compact}_{rand:04d}"
    key = f"[nudge] {when.strftime('%Y-%m-%d %H:%M')}"
    with open_db(readonly=False) as conn:
        conn.execute(
            """
            INSERT INTO memories
              (id, key, content, memory_kind, category, importance,
               session_id, created_at, updated_at, valence, arousal,
               pinned, resolved, digested, activation_count, last_active,
               last_breath_at, consolidated)
            VALUES (?, ?, ?, 'long_term', 'event', 0.3,
                    'nudge-agent', ?, ?, 0.5, 0.3,
                    0, 0, 0, 1.0, '',
                    '', 0)
            """,
            (mem_id, key, content, iso, iso),
        )
        conn.commit()
    return mem_id


# ---------- ntfy ----------

# Threshold above which we promote the first sentence to the ntfy Title header
# so the iOS lock-screen preview shows a meaningful summary even when the body
# gets truncated.
LONG_NUDGE_THRESHOLD = 100
_SENTENCE_TERMINATORS = "。？！.?!\n"


def _first_sentence(text: str) -> str:
    """Return the leading sentence (up to but not including the first
    terminator in `_SENTENCE_TERMINATORS`). Falls back to the full stripped
    text if no terminator is present.
    """
    text = text.strip()
    for i, ch in enumerate(text):
        if ch in _SENTENCE_TERMINATORS:
            head = text[:i].strip()
            if head:
                return head
            break
    return text


def _encode_header_title(title: str) -> str:
    """HTTP headers must be ASCII; ntfy supports RFC 2047 base64-encoded UTF-8
    titles and decodes them server-side, so non-ASCII titles render correctly
    on iOS / Android. ASCII-only titles pass through unchanged.
    """
    try:
        title.encode("ascii")
        return title
    except UnicodeEncodeError:
        import base64
        b64 = base64.b64encode(title.encode("utf-8")).decode("ascii")
        return f"=?UTF-8?B?{b64}?="


def send_ntfy(content: str, title: str = "Nudge", priority: int = 3,
              tags: str = "bulb",
              click_url: str = "claude://") -> tuple[int, str]:
    body = content.encode("utf-8")
    req = urllib.request.Request(
        NTFY_URL, data=body, method="POST",
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "Title": _encode_header_title(title),
            "Priority": str(priority),
            "Tags": tags,
            # Tap → iOS launches the Claude app directly via its registered
            # `claude://` URL scheme (no Safari hop). If the app isn't installed,
            # the tap silently no-ops — switch to https://claude.ai/chat fallback
            # if that becomes a problem.
            "Click": click_url,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=NTFY_TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


# ---------- claude -p invocation ----------

def _read_last_session_date() -> str | None:
    if not LAST_SESSION_DATE_FILE.exists():
        return None
    try:
        return LAST_SESSION_DATE_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _write_last_session_date(date_str: str) -> None:
    try:
        LAST_SESSION_DATE_FILE.write_text(date_str + "\n", encoding="utf-8")
    except OSError as e:
        print(f"[{stamp()}] could not write last_session_date: {e}",
              file=sys.stderr)


def call_claude_code(prompt: str, *, continue_session: bool) -> tuple[int, str, str]:
    """Spawn `claude -p` (optionally with --continue) in this directory.

    Returns (returncode, stdout_text, stderr_text). Setting cwd=SCRIPT_DIR
    ensures CLAUDE.md is loaded as project context.
    """
    claude_path = shutil.which("claude") or "claude"
    argv = [claude_path]
    if continue_session:
        argv.append("--continue")
    argv += ["-p", prompt]
    proc = subprocess.run(
        argv,
        cwd=str(SCRIPT_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CC_TIMEOUT,
    )
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


# ---------- Claude.ai inject ----------

_SKIP_TITLE_KEYWORDS = {"test", "injection", "automated", "nudge-agent"}


def _pick_inject_target(convs: list[dict] | None) -> str | None:
    """Choose which conversation to inject the nudge notification into.

    Priority:
      1. Most-recently-updated work! conversation (excluding test/injection chats)
      2. Most-recently-updated non-test conversation
    Returns a conversation UUID, or None if nothing suitable.
    """
    if not convs:
        return None

    def _is_test(c: dict) -> bool:
        title = (c.get("name") or "").lower()
        return any(kw in title for kw in _SKIP_TITLE_KEYWORDS)

    real = [c for c in convs if not _is_test(c)]
    if not real:
        return None
    work = [c for c in real
            if "work" in (c.get("_project_name") or "").lower()]
    pick = (work or real)[0]
    return pick.get("uuid")


def _try_inject_claude(nudge_text: str, convs: list[dict] | None) -> None:
    """Best-effort inject a notification into Claude.ai.

    Failures are logged but never propagate — inject is an optional
    enhancement on top of the ntfy push.
    """
    try:
        from inject_claude import inject
    except ImportError as e:
        print(f"[{stamp()}] [inject] import failed: {e}", file=sys.stderr)
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    target = _pick_inject_target(convs)

    # Build a rich context message for the in-conversation Claude
    conv_status = ""
    if target and convs:
        for c in convs:
            if c.get("uuid") == target:
                title = (c.get("name") or "").strip()
                tail = c.get("_tail") or []
                if tail:
                    last_msg = tail[-1]
                    conv_status = (
                        f"本对话「{title}」最后一条消息来自 {last_msg['sender']}："
                        f" {last_msg['text'][:100]}"
                    )
                elif title:
                    conv_status = f"本对话「{title}」"
                break

    parts = [
        "[自动消息] Sol 已经离开一段时间了。nudge 已发送到手机。",
        f"nudge 内容：{nudge_text}",
        f"当前时间：{now_str}",
    ]
    if conv_status:
        parts.append(f"对话状态：{conv_status}")
    parts.append(
        "Sol 可能会回来继续这个对话。届时请自然地接上之前的话题，"
        "不需要提及这条自动消息。"
    )
    msg = "\n".join(parts)
    try:
        if target:
            result = inject(msg, conv_id=target)
        else:
            result = inject(msg, new_chat=True)
    except Exception as e:
        print(f"[{stamp()}] [inject] exception: {e}", file=sys.stderr)
        return

    if result.get("ok"):
        where = target[:8] if target else "new"
        print(f"[{stamp()}] [inject] sent to Claude.ai conv {where}...")
    else:
        print(f"[{stamp()}] [inject] failed: {result.get('error')}",
              file=sys.stderr)


# ---------- one cycle ----------

def one_cycle(args: argparse.Namespace) -> int:
    print(f"[{stamp()}] === nudge cycle start ===")

    # cooldown guard (fast path before doing any work)
    if not args.force:
        try:
            with open_db(readonly=True) as conn:
                last = last_nudge_at(conn)
        except sqlite3.Error as e:
            print(f"[{stamp()}] DB read failed: {e}", file=sys.stderr)
            return 3
        if last:
            gap = (datetime.now(timezone.utc) - last).total_seconds() / 60
            if gap < MIN_NUDGE_GAP_MINUTES:
                print(f"[{stamp()}] cooldown — last nudge was {gap:.1f}min "
                      f"ago (< {MIN_NUDGE_GAP_MINUTES}min), skip")
                return 0
            print(f"[{stamp()}] last nudge was {gap:.1f}min ago, proceeding")
        else:
            print(f"[{stamp()}] no prior nudge found, proceeding")
    else:
        print(f"[{stamp()}] --force: bypassing cooldown")

    # Context assembly — pull raw so we can also reason about activity-since
    print(f"[{stamp()}] fetching Claude.ai context...")
    t0 = time.time()
    try:
        claude_convs, err = fetch_context.fetch_raw(limit=10)
    except Exception as e:
        claude_convs, err = None, f"{type(e).__name__}: {e}"
    print(f"[{stamp()}] claude.ai fetched in {time.time()-t0:.1f}s "
          f"({'OK' if claude_convs else 'FAIL: '+str(err)})")
    claude_ai_block = fetch_context.format_block(claude_convs, err)

    print(f"[{stamp()}] reading memory.db context (probing /breath-hook)...")
    memory_block = fetch_memory_context(claude_convs_raw=claude_convs)

    now = datetime.now()
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
    time_line = f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}（{weekday_cn}）"

    prompt = (
        f"{time_line}\n\n"
        f"{claude_ai_block}\n\n"
        f"{memory_block}\n\n"
        f"---\n\n"
        f"请按 CLAUDE.md 的判断标准决定是否要发 nudge。直接输出 nudge 正文或 SKIP，"
        f"不要任何分析、引号、前缀、解释。"
    )

    # Decide whether to chain onto the previous claude session or start fresh.
    # Cross-midnight (local time) → fresh; same day → --continue.
    today_local = datetime.now().strftime("%Y-%m-%d")
    last_date = _read_last_session_date()
    continue_session = (last_date == today_local)
    mode_tag = "--continue" if continue_session else "fresh"
    if not continue_session and last_date:
        print(f"[{stamp()}] date crossed ({last_date} → {today_local}), "
              f"starting fresh CC session")

    print(f"[{stamp()}] calling claude {mode_tag} -p "
          f"({len(prompt)} chars, cwd={SCRIPT_DIR.name})...")
    t0 = time.time()
    try:
        rc, out, err = call_claude_code(prompt, continue_session=continue_session)
    except subprocess.TimeoutExpired:
        print(f"[{stamp()}] claude -p timed out after {CC_TIMEOUT}s",
              file=sys.stderr)
        return 4
    except FileNotFoundError:
        print(f"[{stamp()}] claude CLI not found on PATH", file=sys.stderr)
        return 5
    elapsed = time.time() - t0
    print(f"[{stamp()}] claude {mode_tag} -p returned rc={rc} in {elapsed:.1f}s")

    # Mark today as having had a successful CC invocation so the next cycle
    # can resume. Done even on non-zero rc so we don't keep retry-spawning
    # fresh sessions if the model errors temporarily.
    if rc == 0:
        _write_last_session_date(today_local)
    if err:
        # print stderr regardless — useful for diagnosing rc != 0 cases
        print(f"[{stamp()}] stderr: {err[:500]}", file=sys.stderr)
    if rc != 0:
        print(f"[{stamp()}] non-zero rc, treating as failure", file=sys.stderr)
        return 6

    decision = out.strip().strip("「」\"'").strip()
    print(f"[{stamp()}] decision raw: {decision!r}")

    if not decision or decision.upper().startswith("SKIP"):
        print(f"[{stamp()}] decision = SKIP — nothing to send")
        return 0

    if args.dry_run:
        print(f"[{stamp()}] --dry-run: would push: {decision}")
        return 0

    # Push + persist — for long nudges, surface the first sentence as Title
    # so the lock-screen preview stays meaningful.
    if len(decision) > LONG_NUDGE_THRESHOLD:
        title = _first_sentence(decision) or "Nudge"
        status, resp = send_ntfy(decision, title=title)
    else:
        status, resp = send_ntfy(decision)
    if status != 200:
        print(f"[{stamp()}] ntfy push failed: HTTP {status} — {resp[:200]}",
              file=sys.stderr)
        return 7
    print(f"[{stamp()}] ntfy push OK")

    try:
        mem_id = insert_nudge_memory(decision, datetime.now(timezone.utc))
        print(f"[{stamp()}] wrote memory row {mem_id}")
    except sqlite3.Error as e:
        print(f"[{stamp()}] DB write failed (push already sent): {e}",
              file=sys.stderr)

    # Inject notification into Claude.ai so the in-conversation Claude knows
    # a nudge was sent and can greet Sol naturally when they return.
    _try_inject_claude(decision, claude_convs)

    print(f"[{stamp()}] === nudge cycle done ===")
    return 0


# ---------- loop ----------

def sleep_with_interrupt(seconds: float) -> None:
    end = time.monotonic() + seconds
    while not _stop:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def calc_sleep_seconds(min_minutes: int, max_minutes: int) -> tuple[int, str]:
    """Pick the next-cycle delay based on local time-of-day.

    Night window (22:00–07:30): 3 hours fixed — Sol is likely asleep.
    Day window: random within [min_minutes, max_minutes] (default 20–60).
    Returns (seconds, mode_tag) so the loop can log which schedule it used.
    """
    now = datetime.now()
    hour, minute = now.hour, now.minute
    is_night = hour >= 22 or hour < 7 or (hour == 7 and minute < 30)
    if is_night:
        return 3 * 3600, "night"
    return random.randint(min_minutes * 60, max_minutes * 60), "day"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Claude Code-driven nudge agent")
    parser.add_argument("--once", action="store_true",
                        help="Run a single cycle and exit (no loop)")
    parser.add_argument("--force", action="store_true",
                        help="Bypass the 20-minute cooldown")
    parser.add_argument("--dry-run", action="store_true",
                        help="Decide + print, but do not push to ntfy or write DB")
    parser.add_argument("--min", type=int, default=20,
                        help="Min minutes between cycles (default 20)")
    parser.add_argument("--max", type=int, default=60,
                        help="Max minutes between cycles (default 60)")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _sigint)
    try:
        signal.signal(signal.SIGTERM, _sigint)
    except (ValueError, AttributeError):
        pass

    cycle = 0
    while not _stop:
        cycle += 1
        print(f"\n[{stamp()}] ----- cycle #{cycle} -----")
        try:
            one_cycle(args)
        except Exception as e:
            import traceback
            print(f"[{stamp()}] cycle crashed: {e}", file=sys.stderr)
            traceback.print_exc()

        if args.once or _stop:
            break

        sleep_secs, mode = calc_sleep_seconds(args.min, args.max)
        wait_min = sleep_secs // 60
        wake = datetime.now() + timedelta(seconds=sleep_secs)
        print(f"[{stamp()}] sleeping {wait_min} min ({mode}) — next cycle at "
              f"{wake:%Y-%m-%d %H:%M:%S}")
        sleep_with_interrupt(sleep_secs)

    print(f"[{stamp()}] nudge_cc exited after {cycle} cycle(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
