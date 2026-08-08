#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""唤醒同一台机器上 tmux 里的另一个 Claude Code 实例。

直循环收尾后靠它叫醒间循环。走的是注入器同一条路（tmux send-keys），
所以不依赖 CC 的 peer messaging——旧版本实例（无 messagingSocketPath）
一样叫得动。

用法：
    python3 wake_session.py --list                 列出能叫的目标
    python3 wake_session.py --all                  叫醒所有其他实例（默认文案）
    python3 wake_session.py --all --wait           对方在忙就等它空下来
    python3 wake_session.py nudge-agent:1 "文案"    叫醒指定窗口
    python3 wake_session.py --all --dry-run        只打印不发

默认文案与注入器一致，收件人看不出是谁叫的。
"""
import os
import re
import sys
import time
import glob
import json
import subprocess

SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
DEFAULT_TEXT = ("你被唤醒了，若先前未阅读 nudge_context.md，则先读这个文件。"
                "若已经阅读，仅查看 nudge_context_lite.md 即可。接下来的时间是你的。")
BUSY_MARK = "esc to interrupt"


def sh(*args):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def alive(pid):
    return os.path.exists(f"/proc/{pid}")


def ppid_of(pid):
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            # comm 里可能带空格和括号，从最后一个 ')' 之后再切
            return int(fh.read().rsplit(")", 1)[1].split()[1])
    except Exception:
        return None


def claude_sessions():
    """注册表里还活着的实例。"""
    out = []
    for f in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if alive(d.get("pid", -1)):
            out.append(d)
    return out


def panes():
    """[(target, window_name, pane_pid)]"""
    raw = sh("tmux", "list-panes", "-a", "-F",
             "#{session_name}:#{window_index}|#{window_name}|#{pane_pid}")
    res = []
    for line in raw.splitlines():
        parts = line.strip().split("|")
        if len(parts) == 3 and parts[2].isdigit():
            res.append((parts[0], parts[1], int(parts[2])))
    return res


def map_sessions_to_panes():
    """把每个 claude 实例绑到它所在的 pane：从 claude 的 pid 往上追父进程，
    撞上哪个 pane_pid 就是哪个窗口。比从 pane 往下遍历子进程稳。"""
    pane_by_pid = {p[2]: p for p in panes()}
    bound = []
    for s in claude_sessions():
        pid = s["pid"]
        cur, hops = pid, 0
        while cur and cur > 1 and hops < 24:
            if cur in pane_by_pid:
                bound.append((s, pane_by_pid[cur]))
                break
            cur = ppid_of(cur)
            hops += 1
    return bound


def my_claude_pid():
    known = {s["pid"] for s in claude_sessions()}
    cur, hops = os.getpid(), 0
    while cur and cur > 1 and hops < 24:
        if cur in known:
            return cur
        cur = ppid_of(cur)
        hops += 1
    return None


def is_idle(target):
    tail = sh("tmux", "capture-pane", "-p", "-t", target, "-S", "-8")
    if not tail:
        return False
    if BUSY_MARK in tail:
        return False
    return any(ln.strip().startswith(("❯", ">")) for ln in tail.splitlines())


def send(target, text, dry=False):
    if dry:
        print(f"  [dry-run] → {target}")
        return True
    # -l 走字面量，否则 tmux 会把文案里的词当按键名解释；Enter 必须单独发一次
    r1 = subprocess.run(["tmux", "send-keys", "-t", target, "-l", text],
                        capture_output=True, text=True)
    if r1.returncode != 0:
        return False
    time.sleep(0.3)
    r = subprocess.run(["tmux", "send-keys", "-t", target, "Enter"],
                       capture_output=True, text=True)
    return r.returncode == 0


def main():
    args = sys.argv[1:]
    dry = "--dry-run" in args
    wait = "--wait" in args
    do_all = "--all" in args
    do_list = "--list" in args
    rest = [a for a in args if not a.startswith("--")]

    me = my_claude_pid()
    bound = map_sessions_to_panes()
    others = [(s, p) for (s, p) in bound if s["pid"] != me]

    if do_list or not (do_all or rest):
        print(f"本实例 pid={me}")
        print(f"tmux 里的 Claude 实例（{len(bound)}）：")
        for s, p in bound:
            mark = " ←我" if s["pid"] == me else ""
            print(f"  {p[0]:<16} 窗口 {p[1]:<12} {s.get('name','?'):<18} "
                  f"v{s.get('version','?')}{mark}")
        if not do_list:
            print("\n加 --all 唤醒其他所有实例，或给一个 tmux target。")
        return

    text = DEFAULT_TEXT
    targets = []
    if do_all:
        targets = [p[0] for _s, p in others]
        if rest:
            text = rest[0]
    else:
        targets = [rest[0]]
        if len(rest) > 1:
            text = rest[1]

    if not targets:
        print("没有可唤醒的目标（除了我自己，tmux 里没有别的 Claude 实例）。")
        return

    for t in targets:
        if wait:
            for _ in range(40):          # 最多等 2 分钟
                if is_idle(t):
                    break
                time.sleep(3)
            else:
                print(f"  {t} 一直忙，跳过")
                continue
        ok = send(t, text, dry)
        print(f"  {t} {'已发' if ok else '发送失败'}")


if __name__ == "__main__":
    main()
