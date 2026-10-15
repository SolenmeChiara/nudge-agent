"""把 Claude Code session 转录（JSONL）导出成可读文本，按 session、按天切文件。

用法：
  python3 extract_sessions.py --days 7
  python3 extract_sessions.py --start "2026-09-21 21:30" --end "2026-09-30 13:00"
  python3 extract_sessions.py --days 3 --out /tmp/dump --skip 0896cf3d

输出：<out>/<session前8位>_<MMDD>.txt（默认 logs/session_dump/），每行 `[MM-DD HH:MM] [来源] 文字`，
时间为 EDT。来源：S=S 本人发言；Claude/<模型>=循环的回复文字；cross-session=其他循环的消息（二手）。
工具调用只保留存记忆、ntfy 推送、SendMessage 的痕迹，工具结果一律不收。
"""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_DIR = Path("/home/solenmechiara/.claude/projects/-mnt-d-ClaudeExtentions-MCP-nudge-agent")
DEFAULT_OUT = Path(__file__).resolve().parent / "logs" / "session_dump"
EDT = timezone(timedelta(hours=-4))
NOISE_MARKERS = ("UserPromptSubmit hook additional context", "<system-reminder>")


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, help="从现在往前推 N 天（与 --start 二选一）")
    ap.add_argument("--start", help="起点，EDT，YYYY-MM-DD HH:MM")
    ap.add_argument("--end", help="终点，EDT，默认现在")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    ap.add_argument("--skip", nargs="*", default=[], help="要排除的 session（前缀即可，例如当前正在跑的这个）")
    a = ap.parse_args()
    now = datetime.now(EDT)
    a.end_dt = datetime.strptime(a.end, "%Y-%m-%d %H:%M").replace(tzinfo=EDT) if a.end else now
    if a.start:
        a.start_dt = datetime.strptime(a.start, "%Y-%m-%d %H:%M").replace(tzinfo=EDT)
    elif a.days:
        a.start_dt = now - timedelta(days=a.days)
    else:
        ap.error("需要 --days 或 --start")
    return a


def text_of(content, role):
    if isinstance(content, str):
        return content
    out = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, str):
                out.append(b)
            elif isinstance(b, dict):
                t = b.get("type")
                if t == "text":
                    out.append(b.get("text", ""))
                elif t == "tool_use" and role == "assistant":
                    name = b.get("name", "?")
                    inp = b.get("input", {}) or {}
                    cmd = str(inp.get("command", ""))
                    if "save_memory" in name or "quicksave" in name:
                        out.append(f"[tool {name}: key={str(inp.get('key', inp.get('content', '')))[:100]}]")
                    elif name == "Bash" and "curl" in cmd and "ntfy" in cmd:
                        out.append(f"[ntfy push: {cmd[:220]}]")
                    elif name == "SendMessage":
                        out.append(f"[SendMessage to {inp.get('to', '?')}: {str(inp.get('message', ''))[:300]}]")
    return "\n".join(out)


def model_short(m):
    m = (m or "").lower()
    for k in ("fable", "opus", "sonnet", "haiku"):
        if k in m:
            return k + m.split(k)[-1][:6].replace("-", ".")
    return m[:12]


def user_line(ts, txt):
    if "<cross-session-message" in txt:
        if "你被唤醒了" in txt and len(txt) < 900:
            return None  # 唤醒模板，纯噪音
        src = "cross-session"
        txt = txt.replace("Another Claude session sent a message:\n", "")
        txt = txt.split("This came from another Claude session")[0].strip()
    else:
        src = "S"
    for marker in NOISE_MARKERS:
        if marker in txt and not txt.startswith(marker):
            txt = txt.split(marker)[0].strip()
    return f"[{ts:%m-%d %H:%M}] [{src}] {txt[:2200]}" if txt else None


def main():
    a = parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    summary = []
    for p in sorted(PROJECT_DIR.glob("*.jsonl")):
        sid = p.stem
        if any(sid.startswith(s) for s in a.skip) or p.stat().st_mtime < a.start_dt.timestamp():
            continue
        title, per_day = sid, {}
        with open(p, encoding="utf-8") as f:
            for raw in f:
                try:
                    o = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                t = o.get("type")
                if t == "custom-title":
                    title = o.get("customTitle", title)
                    continue
                if t not in ("user", "assistant") or o.get("isMeta"):
                    continue
                try:
                    ts = datetime.fromisoformat(o.get("timestamp", "").replace("Z", "+00:00")).astimezone(EDT)
                except ValueError:
                    continue
                if not (a.start_dt <= ts < a.end_dt):
                    continue
                msg = o.get("message", {}) or {}
                txt = text_of(msg.get("content", ""), t).strip()
                if not txt:
                    continue
                if t == "user":
                    line = user_line(ts, txt)
                else:
                    line = f"[{ts:%m-%d %H:%M}] [Claude/{model_short(msg.get('model', ''))}] {txt[:2200]}"
                if line:
                    per_day.setdefault(ts.strftime("%m%d"), []).append(line)
        for day, lines in sorted(per_day.items()):
            fp = out / f"{sid[:8]}_{day}.txt"
            fp.write_text(f"=== session {sid} | title: {title} | day {day} | {len(lines)} entries (EDT) ===\n\n"
                          + "\n\n".join(lines) + "\n", encoding="utf-8")
            summary.append((sid[:8], title, day, len(lines), fp.stat().st_size // 1024))
    print("session title day entries KB")
    for s in summary:
        print(*s)
    print("TOTAL KB", sum(s[4] for s in summary), "->", out)


if __name__ == "__main__":
    main()
