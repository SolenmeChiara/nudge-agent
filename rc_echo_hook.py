#!/usr/bin/env python3
"""Stop hook：把回合末尾的助手回复截一句预览推到 ntfy。

用途：Sol 从手机 Claude app 的 Remote Control 发消息进来时，Claude Code 的
文字回复不会推回手机。这个 hook 在回合结束时自动补一条 ntfy 通知。

只对 RC 来的消息（origin.kind == "human" 且 promptSource == "queued"）生效，
注入器敲进来的（typed）、别的 session 发来的（peer）、系统条目一律跳过。

选回合不能只看「最后一条提示」，transcript 里有三种错位：
F（落盘慢）：本回合的 assistant 条目比 Stop 晚 2–7 秒才写进去，尾部只剩一条光秃秃的
  提示，得等一会儿重读。
B（回合后追加）：跨 session 消息、任务通知会在回合刚结束时追加成 user 条目，把真正该
  镜像的那个回合挤到后面去。
C（compact 续写）：compact 之后的续写条目插在提示和它的回复中间，回复看起来像是在答
  这条续写。续写不算提示（isCompactSummary），直接排除。
对策：assistant 先顺 parentUuid 认自己的提示、认不出再按位置退；镜像最后一条 assistant
所属的那个提示，尾部没人应答的提示只用来决定要不要重读。

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
import time
import urllib.request

NTFY_URL = "https://ntfy.sh/"
NTFY_TOPIC = "sol-nudge-private"
NTFY_MARKER = "ntfy.sh/sol-nudge-private"
TIMEOUT = 6
# Stop 触发时本回合的 assistant 条目可能还没落盘，等这么久重读几次
RETRY_TIMES = 3
RETRY_WAIT = 1.2

# 尾部读取窗口：transcript 可能有几十 MB，只读末尾这么多字节
TAIL_BYTES = 2 * 1024 * 1024

# 顺 parentUuid 往上追归属时最多走这么多步
MAX_CHAIN = 500

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
            obj = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            continue
        # 只收字典行：transcript 里混进裸标量（合法 JSON 但不是对象）时，
        # 下游 .get() 会抛 AttributeError 把整次镜像吞掉
        if isinstance(obj, dict):
            rows.append(obj)
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
    """真正的提示：user 条目，不是工具返回，不是 meta，不是子代理内部对话，不是 compact 续写。"""
    if entry.get("type") != "user":
        return False
    if entry.get("isMeta"):
        return False
    if entry.get("isCompactSummary"):
        # compact 续写是上下文重建产物，会插在提示和它的回复中间，不是提示
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


def real_prompt_indices(rows):
    return [i for i, r in enumerate(rows) if is_real_prompt(r)]


def uuid_index(rows):
    idx = {}
    for i, r in enumerate(rows):
        u = r.get("uuid")
        if u and u not in idx:
            idx[u] = i
    return idx


def preceding_prompts(rows, idxs):
    """每条 row 前面最近的那条真提示下标（不含自己），没有就是 -1。"""
    mark = set(idxs)
    out, last = [], -1
    for i in range(len(rows)):
        out.append(last)
        if i in mark:
            last = i
    return out


def prompt_of(rows, i, by_uuid, prev_prompt, memo):
    """这条 assistant 属于哪个提示：先顺 parentUuid 往上追第一条真提示，追不到再按位置退。

    追不到的情形：父条目不在读入的窗口里、链断了、或者像 compact 续写那样一路追到
    parentUuid 为 null 也没碰到提示。
    """
    chain, seen = [], set()
    cur = rows[i].get("parentUuid")
    owner, steps = -1, 0
    while cur and cur not in seen and steps < MAX_CHAIN:
        if cur in memo:
            owner = memo[cur]
            break
        seen.add(cur)
        j = by_uuid.get(cur)
        if j is None:
            break
        if is_real_prompt(rows[j]):
            owner = j
            break
        chain.append(cur)
        cur = rows[j].get("parentUuid")
        steps += 1
    if owner >= 0:
        for u in chain:
            memo[u] = owner
        return owner
    return prev_prompt[i]


def scan_turns(rows):
    """切回合，返回（最后一条 assistant 所属的提示下标, 归它的助手条目, 它之后没人应答的提示下标）。

    归属先走 parentUuid 链、再退位置，这样即便中间插进 compact 续写（C）或者回合结束后
    追进来的条目（B），assistant 也还认得自己的提示。
    """
    idxs = real_prompt_indices(rows)
    by_uuid = uuid_index(rows)
    prev_prompt = preceding_prompts(rows, idxs)
    owned = {i: [] for i in idxs}
    memo = {}
    last = -1
    for i, r in enumerate(rows):
        if r.get("type") != "assistant" or r.get("isSidechain"):
            continue
        owner = prompt_of(rows, i, by_uuid, prev_prompt, memo)
        if owner not in owned:
            continue
        owned[owner].append(r)
        last = owner
    trailing = [i for i in idxs if i > last and not owned[i]]
    return last, owned.get(last, []), trailing


def load_rows(tpath):
    """先读尾部窗口；窗口里一条真提示都没有才整文件重读。"""
    rows, start = read_tail_lines(tpath)
    rows = normalize_rows(rows)
    if start > 0 and not real_prompt_indices(rows):
        rows = normalize_rows(read_all_lines(tpath))
    return rows


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


def is_rc_prompt(entry):
    return origin_kind(entry) == "human" and entry.get("promptSource") == "queued"


def has_skip_prefix(entry):
    t = text_of(entry).lstrip()
    return any(t.startswith(p) for p in SKIP_PREFIXES)


def handled(prompt, assistants, seen_uuid):
    """这个回合不用再镜像：不是 RC、已镜像过、开头在黑名单里、或者本回合自己发过 ntfy。"""
    if not is_rc_prompt(prompt):
        return True
    if seen_uuid and prompt.get("uuid") == seen_uuid:
        return True
    if has_skip_prefix(prompt):
        return True
    return turn_used_ntfy(assistants)


def retry_mark(n):
    return "+r%d" % n if n else ""


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


def report_skip(prompt, assistants, sid, spath, seen):
    """没东西可镜像时照旧写跳过日志，顺序与老版本一致：not-rc → prefix → 已手动镜像 → dup。"""
    if not is_rc_prompt(prompt):
        log("skip:not-rc")
        return
    if has_skip_prefix(prompt):
        log("skip:prefix")
        return
    if turn_used_ntfy(assistants):
        log("skip:已手动镜像")
        # 手动发过的回合也记账，下一次 Stop 才不会把它当「漏发的旧回合」翻出来补发
        uuid = prompt.get("uuid") or ""
        if uuid:
            seen[sid] = uuid
            save_state(spath, seen)
        return
    _, text = last_text_entry(assistants)
    log("skip:dup", make_preview(text))


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

    sid = str(payload.get("session_id") or "default")
    spath = state_path()
    seen = load_state(spath)
    seen_uuid = seen.get(sid)

    # 选回合。9/5 实测三种错位（F 落盘慢 / B 回合后追加 / C compact 续写插在中间），
    # 老代码一律拿「最后一条提示」当本回合，于是报 skip:not-rc 把该镜像的回合漏掉。
    # 对策见模块头：归属走 parentUuid，镜像最后一条 assistant 认领的那个提示，
    # 尾巴上没人应答的提示仅用来决定要不要重读。
    rows = load_rows(tpath)
    retried = 0
    idx, assistants, prev = -1, [], False
    while True:
        last, last_assts, trailing = scan_turns(rows)
        if last >= 0 and not handled(rows[last], last_assts, seen_uuid):
            # 已知取舍：若更早的某个 RC 回合当初发送失败一直没记账，这里会先把它补发出来
            # （带 +prev），本回合顺延到下一次 Stop。一次 Stop 只发一条，不做补偿队列。
            idx, assistants, prev = last, last_assts, bool(trailing)
            break
        pending = [t for t in trailing if not handled(rows[t], [], seen_uuid)]
        if pending and retried < RETRY_TIMES:
            time.sleep(RETRY_WAIT)
            retried += 1
            rows = load_rows(tpath)
            continue
        if pending:
            # 重读用尽仍没等到 assistant：拿最早那条「挂起」的提示配 last_assistant_message 兜底。
            # 只能取 pending[0]，不能取 trailing[0]：尾部排成 [非 RC 追加行, 待镜像 RC] 时，
            # trailing[0] 落在那条非 RC 上，会直接 skip:not-rc 把该补的镜像丢掉。
            idx = pending[0]
            fb = payload.get("last_assistant_message")
            if not (isinstance(fb, str) and fb.strip()):
                log("skip:no-assistant" + retry_mark(retried))
                return
            break
        rep = last if last >= 0 else (trailing[-1] if trailing else -1)
        if rep < 0:
            log("skip:no-prompt")
            return
        report_skip(rows[rep], last_assts if rep == last else [], sid, spath, seen)
        return

    prompt = rows[idx]
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

    body = {
        "topic": NTFY_TOPIC,
        "title": "回了（{}）".format(short_model(assistants, chosen)),
        "message": preview,
        "tags": ["speech_balloon"],
        "priority": 4,
        "click": "claude://",
    }

    mark = retry_mark(retried) + ("+prev" if prev else "")
    if os.environ.get("RC_ECHO_DRY_RUN") == "1":
        sys.stderr.write(json.dumps(body, ensure_ascii=False) + "\n")
        log("dry-run" + mark, preview)
    else:
        send(body)
        log("sent" + mark, preview)

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
