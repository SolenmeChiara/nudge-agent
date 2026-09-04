#!/usr/bin/env python3
"""Stop hook：把回合末尾的助手回复截一句预览推到 ntfy。

用途：Sol 从手机 Claude app 的 Remote Control 发消息进来时，Claude Code 的
文字回复不会推回手机。这个 hook 在回合结束时自动补一条 ntfy 通知。

只对 RC 来的消息（origin.kind == "human" 且 promptSource == "queued"）生效，
注入器敲进来的（typed）、别的 session 发来的（peer）、系统条目一律跳过。

环境变量：
  RC_ECHO_DRY_RUN=1   不真发，把 payload 打到 stderr
  RC_ECHO_STATE_DIR   覆盖状态目录（默认 <脚本目录>/logs/states）
  RC_ECHO_LOG         覆盖日志路径（默认 <脚本目录>/logs/rc_echo.log）

任何异常都吞掉并 exit 0，绝不阻塞 Claude Code。
"""

import json
import os
import re
import sys
import urllib.request

NTFY_URL = "https://ntfy.sh/"
NTFY_TOPIC = "sol-nudge-private"
NTFY_MARKER = "ntfy.sh/sol-nudge-private"
TIMEOUT = 10

# 尾部读取窗口：transcript 可能有几十 MB，只读末尾这么多字节
TAIL_BYTES = 2 * 1024 * 1024

MAX_PREVIEW = 70
MIN_SENTENCE = 12
SENTENCE_END = "。！？!?"

# 第二道保险：这些开头的提示不是 Sol 在 RC 里说话
SKIP_PREFIXES = (
    "你被唤醒了",
    "[紧急插播]",
    "[戳一戳]",
    "This session is being continued",
    "<task-notification>",
    "Another Claude session sent",
)

MODEL_MAP = (
    ("fable-5-1", "fable5.1"),
    ("fable-5", "fable5"),
    ("opus", "opus4.6"),
    ("sonnet", "sonnet"),
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def now_str():
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/Toronto")).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        try:
            from datetime import datetime

            return datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " (local)"
        except Exception:
            return "?"


def log_path():
    p = os.environ.get("RC_ECHO_LOG")
    if p:
        return p
    return os.path.join(SCRIPT_DIR, "logs", "rc_echo.log")


def state_path():
    d = os.environ.get("RC_ECHO_STATE_DIR") or os.path.join(SCRIPT_DIR, "logs", "states")
    return os.path.join(d, "rc_echo_last")


def load_state(path):
    """{session_id: 上次处理过的 user uuid}。三个 session 共用一个目录，按 session 分账。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    # 兼容早期只存一个裸 uuid 的格式
    return {"default": raw}


def save_state(path, data):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # 只留最近 20 个 session，避免无限长
        items = list(data.items())[-20:]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dict(items), f, ensure_ascii=False)
    except Exception:
        pass


SESSION_TAG = "-"


def log(result, preview=""):
    try:
        p = log_path()
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        line = "{} | {} | {} | {}\n".format(now_str(), SESSION_TAG, result, (preview or "")[:30].replace("\n", " "))
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def read_tail_lines(path):
    """读 transcript 末尾若干字节，返回解析好的条目列表（旧→新）。

    找不到候选提示时由调用方决定是否整文件重读。
    """
    size = os.path.getsize(path)
    start = max(0, size - TAIL_BYTES)
    with open(path, "rb") as f:
        f.seek(start)
        blob = f.read()
    if start > 0:
        # 丢掉可能被截断的第一行
        nl = blob.find(b"\n")
        blob = blob[nl + 1:] if nl >= 0 else b""
    return parse_lines(blob), start


def read_all_lines(path):
    with open(path, "rb") as f:
        return parse_lines(f.read())


def parse_lines(blob):
    rows = []
    for raw in blob.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rows.append(json.loads(raw.decode("utf-8", "replace")))
        except Exception:
            continue
    return rows


def content_blocks(entry):
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return []
    c = msg.get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    if isinstance(c, list):
        return [b for b in c if isinstance(b, dict)]
    return []


def text_of(entry):
    parts = []
    for b in content_blocks(entry):
        if b.get("type") == "text" and isinstance(b.get("text"), str):
            parts.append(b["text"])
    return "".join(parts).strip()


def is_real_prompt(entry):
    """真正的提示：user 条目，不是工具返回，不是 meta，不是子代理内部对话。"""
    if entry.get("type") != "user":
        return False
    if entry.get("isMeta"):
        return False
    if entry.get("isSidechain"):
        return False
    for b in content_blocks(entry):
        if b.get("type") == "tool_result":
            return False
    return True


def normalize_rows(rows):
    """回合进行中送达的 RC 消息不是 user 条目，而是 attachment(queued_command)。
    把它们改写成等价的 user 条目（origin 照抄，promptSource 视为 queued）。"""
    out = []
    for r in rows:
        if r.get("type") == "attachment":
            a = r.get("attachment")
            if isinstance(a, dict) and a.get("type") == "queued_command" and a.get("prompt"):
                r = dict(r)
                r["type"] = "user"
                r["origin"] = a.get("origin")
                r["promptSource"] = "queued"
                r["uuid"] = a.get("source_uuid") or r.get("uuid")
                r["message"] = {"role": "user", "content": str(a.get("prompt"))}
        out.append(r)
    return out


def find_last_prompt(rows):
    for i in range(len(rows) - 1, -1, -1):
        if is_real_prompt(rows[i]):
            return i
    return -1


def origin_kind(entry):
    o = entry.get("origin")
    if isinstance(o, dict):
        return o.get("kind")
    return None


def turn_used_ntfy(assistants):
    for a in assistants:
        for b in content_blocks(a):
            if b.get("type") != "tool_use":
                continue
            if b.get("name") != "Bash":
                continue
            inp = b.get("input")
            if isinstance(inp, dict) and NTFY_MARKER in str(inp.get("command", "")):
                return True
    return False


def last_text_entry(assistants):
    for a in reversed(assistants):
        t = text_of(a)
        if t:
            return a, t
    return None, ""


def short_model(assistants, chosen):
    raw = ""
    if isinstance(chosen, dict) and isinstance(chosen.get("message"), dict):
        raw = chosen["message"].get("model") or ""
    if not raw:
        for a in reversed(assistants):
            m = a.get("message")
            if isinstance(m, dict) and m.get("model"):
                raw = m["model"]
                break
    if not raw:
        return "claude"
    low = raw.lower()
    for needle, short in MODEL_MAP:
        if needle in low:
            return short
    return raw


def make_preview(text):
    lines = []
    for line in text.split("\n"):
        s = line.strip()
        s = re.sub(r"^#{1,6}\s*", "", s)
        s = re.sub(r"^[-*+]\s+", "", s)
        lines.append(s)
    flat = " ".join(lines)
    flat = flat.replace("**", "").replace("`", "").replace("#", "")
    flat = re.sub(r"\s+", " ", flat).strip()
    if not flat:
        return ""
    cut = None
    for idx, ch in enumerate(flat):
        if ch in SENTENCE_END and idx + 1 >= MIN_SENTENCE:
            cut = idx + 1
            break
    if cut is not None:
        flat = flat[:cut]
    if len(flat) > MAX_PREVIEW:
        flat = flat[:MAX_PREVIEW] + "…"
    return flat


def send(payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        NTFY_URL,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        resp.read()


def run():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except Exception:
        log("skip:bad-stdin")
        return
    if not isinstance(payload, dict):
        log("skip:bad-stdin")
        return

    if payload.get("stop_hook_active"):
        return

    global SESSION_TAG

    SESSION_TAG = str(payload.get("session_id") or "-")[:8]

    tpath = payload.get("transcript_path")
    if not tpath or not os.path.isfile(tpath):
        log("skip:no-transcript")
        return

    rows, start = read_tail_lines(tpath)
    rows = normalize_rows(rows)
    idx = find_last_prompt(rows)
    if idx < 0 and start > 0:
        rows = normalize_rows(read_all_lines(tpath))
        idx = find_last_prompt(rows)
    if idx < 0:
        log("skip:no-prompt")
        return

    prompt = rows[idx]

    if origin_kind(prompt) != "human" or prompt.get("promptSource") != "queued":
        log("skip:not-rc")
        return

    ptext = text_of(prompt).lstrip()
    if any(ptext.startswith(p) for p in SKIP_PREFIXES):
        log("skip:prefix")
        return

    assistants = [
        r for r in rows[idx + 1:]
        if r.get("type") == "assistant" and not r.get("isSidechain")
    ]
    if not assistants:
        log("skip:no-assistant")
        return

    if turn_used_ntfy(assistants):
        log("skip:已手动镜像")
        return

    chosen, text = last_text_entry(assistants)
    if not text:
        # 回复文本可能晚于 Stop 落盘；Stop 钩子入参若带 last_assistant_message 则用它兜底
        fallback = payload.get("last_assistant_message")
        if isinstance(fallback, str) and fallback.strip():
            text = fallback
            log("fallback:last_assistant_message")
        else:
            log("skip:no-text")
            return

    preview = make_preview(text)
    if not preview:
        log("skip:empty-preview")
        return

    uuid = prompt.get("uuid") or ""
    sid = str(payload.get("session_id") or "default")
    spath = state_path()
    seen = load_state(spath)
    if uuid and seen.get(sid) == uuid:
        log("skip:dup", preview)
        return

    body = {
        "topic": NTFY_TOPIC,
        "title": "回了（{}）".format(short_model(assistants, chosen)),
        "message": preview,
        "tags": ["speech_balloon"],
        "priority": 4,
        "click": "claude://",
    }

    if os.environ.get("RC_ECHO_DRY_RUN") == "1":
        sys.stderr.write(json.dumps(body, ensure_ascii=False) + "\n")
        log("dry-run", preview)
    else:
        send(body)
        log("sent", preview)

    seen[sid] = uuid
    save_state(spath, seen)


def main():
    try:
        run()
    except Exception as exc:  # 绝不让 hook 把回合卡住
        try:
            log("error:{}".format(type(exc).__name__))
        except Exception:
            pass
    sys.exit(0)


if __name__ == "__main__":
    main()
