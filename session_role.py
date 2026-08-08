#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判定本 session 在多 session 链里是直循环还是间循环，并列出同期活着的实例。

用法：
    python3 session_role.py          # 判定 + 列表
    python3 session_role.py --list   # 只列表
    python3 session_role.py --quiet  # 只吐一个词：direct / relay / unknown

判定规则（CLAUDE.md「多 session 管理」节）：
门牌名里写了「直循环」/「间循环」就直接采信 —— 名字是 Sol 起的意图声明，比猜可靠。
名字没写才退回启发式：注入器唤醒的那一个是直循环，所以看注入器最近一次注入之后、
本 session 活跃之前有没有别的 session 动过 —— 有，说明注入器叫的是它，本 session 是被
它转发唤醒的（间循环）；没有，本 session 就是直循环。

启发式的失效场景（2026-08-08 实测翻车）：间循环被 Sol 从手机 remote control 直连时，
它有了独立于注入器的活跃源，时间戳会跑到直循环前面，于是直循环被判成间循环。
「注入器叫谁谁先动」这个前提一旦破了，启发式就没救，只能靠名字。

注册表 ~/.claude/sessions/<pid>.json 里死进程的残留很常见（进程崩了不会自己清），
所以每条都拿 pid 存活性过一遍再算。
"""
import json
import os
import re
import sys
import glob
from datetime import datetime

SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
INJECT_LOG = "/mnt/d/ClaudeExtentions/MCP/nudge-agent/logs/logs/nudge_inject.log"
INJECT_MARK = "wakeup injected into tmux"


def alive(pid):
    return os.path.exists(f"/proc/{pid}")


def proc_start_ms(pid):
    """进程启动时刻（epoch ms）。注入时还没出生的进程不可能是注入对象。"""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            jiffies = int(fh.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/stat", encoding="utf-8") as fh:
            btime = next(int(ln.split()[1]) for ln in fh if ln.startswith("btime"))
        return (btime + jiffies / os.sysconf("SC_CLK_TCK")) * 1000
    except Exception:
        return None


def load_sessions():
    out = []
    for f in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if not alive(d.get("pid", -1)):
            continue
        out.append(d)
    out.sort(key=lambda d: d.get("updatedAt", 0), reverse=True)
    return out


def my_pid():
    """从本进程往上找，第一个在注册表里有档案的祖先就是承载我的 claude。"""
    known = {}
    for f in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            known[json.load(open(f, encoding="utf-8"))["pid"]] = True
        except Exception:
            pass
    pid = os.getpid()
    for _ in range(24):
        if pid in known:
            return pid
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                # comm 里可能有空格，取最后一个 ')' 之后的字段
                pid = int(fh.read().rsplit(")", 1)[1].split()[1])
        except Exception:
            return None
        if pid <= 1:
            return None
    return None


def last_inject_ms():
    try:
        lines = open(INJECT_LOG, encoding="utf-8", errors="replace").readlines()
    except Exception:
        return None
    for line in reversed(lines):
        if INJECT_MARK in line:
            m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", line)
            if m:
                return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp() * 1000
    return None


def fmt(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S") if ms else "?"


def main():
    args = sys.argv[1:]
    quiet = "--quiet" in args
    sessions = load_sessions()
    me = my_pid()
    inject = last_inject_ms()

    role, why = "unknown", "找不到本 session 的注册档案"
    if me is not None:
        mine = next((d for d in sessions if d["pid"] == me), None)
        name = (mine or {}).get("name", "") or ""
        if mine and ("直循环" in name or "间循环" in name):
            role = "direct" if "直循环" in name else "relay"
            why = f"门牌名「{name}」写明了角色，不走启发式"
        elif mine and inject:
            t_me = mine.get("updatedAt", 0)
            between = [d for d in sessions
                       if d["pid"] != me and inject < d.get("updatedAt", 0) < t_me
                       and (proc_start_ms(d["pid"]) or 0) <= inject]
            if between:
                role = "relay"
                why = "注入器注入后、我活跃前，" + "、".join(
                    d.get("name", "?") for d in between) + " 动过 —— 注入器叫的是它"
            else:
                role = "direct"
                why = "注入器注入后到我活跃之间没有别的 session 动过"
        elif mine:
            role, why = "unknown", "注入器日志里读不到注入时间"

    if quiet:
        print(role)
        return

    if "--list" not in args:
        label = {"direct": "直循环（注入器直接叫的，本轮收尾后负责唤醒其他实例）",
                 "relay": "间循环（被转发唤醒，无特殊义务）",
                 "unknown": "判不出来"}[role]
        print(f"本 session：{label}")
        print(f"  依据：{why}")
        print(f"  注入器最近一次注入 {fmt(inject)}")
        print()

    print(f"活着的实例（{len(sessions)}）：")
    for d in sessions:
        star = " ←我" if d["pid"] == me else ""
        sock = "可收发" if d.get("messagingSocketPath") else "旧版·收发不了"
        print(f"  {d.get('name','?'):<16} pid={d['pid']:<7} v{d.get('version','?'):<8} "
              f"{sock:<14} {d.get('status','?'):<7} 活跃 {fmt(d.get('updatedAt'))}{star}")


if __name__ == "__main__":
    main()
