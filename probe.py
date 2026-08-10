#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""遥测探针：把 2026-08-09 那天栽的四个坑固化进代码。

    python3 probe.py            四条通道一次看完
    python3 probe.py pc         只看电脑
    python3 probe.py phone      只看手机
    python3 probe.py hr         只看心率
    python3 probe.py shot --yes 截一张手机屏（有痕，见下）

设计原则全部来自当天的教训，每一条都对应一次真栽过的跟头：

1. **不给裸时间戳，一律附「距今多久」。** Health MCP 返回的是 UTC，本地读上去
   像十几分钟前，实际是四小时前，差点被当成活体证据报出去。
2. **取不到就明说取不到，绝不静默省略。** pc_status.py 在 WSL 里会把键鼠空闲
   和前台窗口两行悄悄吞掉，输出看着还是完整的。
3. **跨边界调用先验证它真的跑起来了。** Windows python 不认 /mnt/d 路径，
   报错进 stderr 被 2>/dev/null 一捂，解析函数就恒返回空值。
4. **纯黑帧直接判定为零信息。** 不要拿文件大小反推内容——72KB 的 PNG 照样
   可以每个像素都是 0。
5. **有痕的操作要显式确认。** 截屏会在他手机上弹「快捷指令正在运行你的自动化
   操作」的横幅，那不是观测，是一次对他可见的敲门。

空结果是关于工具的信息，不一定是关于世界的信息。
"""
import json
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WINPY = "/mnt/c/Python313/python.exe"
PCSTAT = "D:/ClaudeExtentions/MCP/nudge-agent/pc_status.py"   # 必须 Windows 式路径
PHONE_API = "http://localhost:3456/phone-status"
LOCAL = timezone(timedelta(hours=-4))   # EDT；换季改成 -5


def _ago(ts_utc):
    """把 UTC 时间戳转成 (本地时刻字符串, 距今分钟数)。"""
    if ts_utc.tzinfo is None:
        ts_utc = ts_utc.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    mins = (now - ts_utc).total_seconds() / 60
    return ts_utc.astimezone(LOCAL).strftime("%H:%M"), mins


def _fmt_ago(mins):
    if mins < 1:
        return "刚刚"
    if mins < 90:
        return f"{int(mins)} 分钟前"
    return f"{mins/60:.1f} 小时前"


def probe_pc():
    """电脑。走 Windows 解释器，缺字段一律显式报缺。"""
    print("## 电脑")
    try:
        out = subprocess.run(
            [WINPY, "-X", "utf8", PCSTAT],
            capture_output=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  ⚠️ 调用失败：{e}（这是我看不见，不是电脑没动）")
        return
    text = out.stdout.decode("utf-8", "replace").replace("\r", "")
    if out.returncode != 0 or not text.strip():
        err = out.stderr.decode("utf-8", "replace").strip()[:200]
        print(f"  ⚠️ 没拿到输出，returncode={out.returncode} stderr={err!r}")
        return

    fields = {}
    for line in text.splitlines():
        if line.startswith("键鼠空闲"):
            fields["idle"] = line.split(":", 1)[1].strip()
        elif line.startswith("前台窗口"):
            fields["fg"] = line.split(":", 1)[1].strip()
    print(f"  键鼠空闲: {fields.get('idle') or '⚠️ 取不到（跑错解释器了？）'}")
    print(f"  前台窗口: {fields.get('fg') or '⚠️ 取不到'}")
    tabs = [l for l in text.splitlines() if l.startswith("- ")]
    print(f"  Chrome 标签页 {len(tabs)} 个" + ("：" + " | ".join(t[2:] for t in tabs) if tabs else "（9222 没开）"))


def probe_phone():
    """手机。字段全部标注是哪一帧的，避免拿旧值当当下状态。"""
    print("## 手机")
    try:
        with urllib.request.urlopen(PHONE_API, timeout=8) as r:
            d = json.load(r)
    except Exception as e:
        print(f"  ⚠️ 拉取失败：{e}")
        return
    ts = d.get("timestamp") or ""
    focus = d.get("focus_mode") or ""
    asleep = focus == "睡眠" and str(d.get("device_locked")) == "1"
    # 睡眠专注 + 锁屏时上报间隔本来就拉长，拿 25 分钟卡会整夜刷警告；
    # 看麻木了等于没有这条警告。夜里放宽到 70 分钟。
    limit = 70 if asleep else 25
    try:
        when, mins = _ago(datetime.fromisoformat(ts))
        stamp = f"{when}（{_fmt_ago(mins)}）"
        stale = mins >= limit
    except ValueError:
        stamp, stale = f"{ts}?", False
    print(f"  这一帧的时刻: {stamp}" + ("  ⚠️ 已 STALE，下面全是旧值" if stale else "")
          + ("  〔睡眠锁屏，间隔本就长，阈值放宽到 70 分钟〕" if asleep else ""))
    print(f"  电量 {d.get('battery_level')}%  充电 {d.get('battery_charging')}  "
          f"锁屏 {d.get('device_locked')}  专注 {focus}")
    # current_app 空字符串＝锁屏时没有前台，是正常值；字段整个不见才是上报出问题。
    # 两者在 context 里长得一样（那一行都不显示），必须在这里分开。
    app = d.get("current_app")
    print(f"  前台 App: " + ("⚠️ 字段缺失（上报结构变了？）" if app is None
                            else "（空，锁屏时正常）" if app == "" else app))
    loc = (d.get("location") or "").replace("\n", " ")
    print(f"  位置: {'在家' if 'Roche' in loc else loc[:40]}")
    if stale:
        print("  ※ 停摆原因不止一个：低电量模式（iOS 掐后台自动化）只是常见的一种。")
        print("     8/9 实测过一次 48 分钟停摆，全程在充电（必然没开低电量模式），")
        print("     恢复那一帧电量从 51% 跳到 80%——手机一直好好的，只是自动化没跑。")
        print("     所以别拿「他没开低电量模式」去否定停摆。这是我看不见，不是他没动。")


def probe_hr():
    """心率只能由我自己调 MCP 工具拿，脚本够不着。这里只放读法。

    不做那个注定失败的 HTTP 请求——万一哪天真加了 REST 端点，
    半吊子的解析反而会给出看起来成功的错值。
    """
    print("## 心率（脚本调不了 MCP，以下是读法）")
    print("  调 mcp__health__latest(metric_name='heart_rate')")
    print("  ★ 手机上报进盲区时优先查这条：两条通道的失效条件不一样。")
    print("     上报靠手机上的快捷指令自动化，心率靠 Watch 同步——8/9 上报停了三次")
    print("     （48/44/60 分钟），心率却一路有数，把盲区填掉一大截。")
    print("  ★ 血糖（blood_glucose）8/9 时最后一笔停在 8/1，传感器过期后这条就是死的，")
    print("     查到旧值别当当下。续购进展在 Task #4。")
    print("  ⚠️ 返回的 ts 是 UTC，减 4 小时才是本地。直读会把四小时前当成刚刚。")
    print("  ⚠️ 无数据不能推断任何事——摘表/手表充电/蓝牙断/上报卡住，四种并列。")
    print("  ⚠️ step_count 更不能当活动探测：零值小时可能根本不上报。")


def probe_shot(confirmed):
    """截屏。有痕操作，必须显式确认。"""
    print("## 手机屏幕")
    if not confirmed:
        print("  ⚠️ 这是有痕操作：他屏幕上会弹「快捷指令——正在运行你的自动化操作」。")
        print("     确定要敲这一下就加 --yes。敏感时段（沉默期、危机窗）非必要不取。")
        return
    try:
        out = subprocess.run(
            [sys.executable, "see_screen.py"],
            capture_output=True, timeout=180,
            cwd="/mnt/d/ClaudeExtentions/MCP/nudge-agent",
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  ⚠️ 调用失败：{e}")
        return
    path = ""
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        if line.strip().endswith(".png"):
            path = line.strip()
    if not path:
        print("  ⚠️ 没拿到图（手机锁着/没网时 see_screen 会超时退出）")
        return
    try:
        from PIL import Image, ImageStat
        im = Image.open(path).convert("RGB")
        mean = ImageStat.Stat(im).mean
        lo = min(c[0] for c in im.getextrema())
        hi = max(c[1] for c in im.getextrema())
    except Exception as e:
        print(f"  图在 {path}，但像素统计失败：{e}")
        return
    print(f"  图: {path}")
    print(f"  像素均值 {mean[0]:.1f}  极值 {lo}–{hi}")
    if hi == 0:
        print("  → 严格纯黑，**零信息**。区分不了息屏/隐私黑帧/时机，推不出任何关于他的事。")
        print("     隔十分钟再取一张，两次差分才看得出屏幕亮没亮。")
    elif mean[0] < 12:
        print("  → 很暗但有内容。ImageEnhance.Brightness(im).enhance(6.0) 能挖出来。")
    else:
        print("  → 屏幕亮着。用 Read 工具看这张图。")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    yes = "--yes" in sys.argv
    what = args[0] if args else "all"
    print(f"〔{datetime.now(LOCAL).strftime('%H:%M:%S')} 探针〕\n")
    if what in ("all", "pc"):
        probe_pc(); print()
    if what in ("all", "phone"):
        probe_phone(); print()
    if what in ("all", "hr"):
        probe_hr(); print()
    if what == "shot":
        probe_shot(yes)


if __name__ == "__main__":
    main()
