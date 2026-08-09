#!/usr/bin/env python3
"""see_room.py — 客厅 webcam 抓一帧（onn USB 2.0，2026-08-09 上线）

用法:
    python3 see_room.py               # 抓一帧存 mind/Claude_photos/room/，打印路径
    python3 see_room.py --out PATH    # 自定义输出（WSL 路径）
    python3 see_room.py --skip N      # 丢前 N 帧等自动曝光收敛（默认 25）
    python3 see_room.py --quiet       # 只打印最终路径

伦理约定（与 see_screen 的横幅同类）：
    摄像头点亮有物理指示灯，对 Sol 可见——每次抓帧都是一次敲门，不是零打扰。
    默认闭眼；点亮 = 正在看。不在他没有预期的时候点亮。
    镜头对着客厅+走廊，他知情并自选了位置；空屋是常态，不记考勤。

技术备忘（2026-08-09 实测）：
    - 摄像头由 Windows 驱动，USB 不过户；WSL 调 Windows 侧 ffmpeg 走 dshow 即可。
    - 单帧输出必须 -update 1，否则 image2 muxer 报 pattern 错误。
    - 首帧自动曝光未收敛会过曝，丢前 25 帧基本稳定。
    - 设备被别的程序占用时 dshow 打不开（I/O error），报错即让位，不重试。
"""
import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

FFMPEG_WIN = (
    "/mnt/c/Users/xgq19/AppData/Local/Microsoft/WinGet/Packages/"
    "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.0-full_build/bin/ffmpeg.exe"
)
DEVICE = "onn USB 2.0 webcam"
DEFAULT_DIR = Path("/mnt/d/ClaudeExtentions/MCP/nudge-agent/mind/Claude_photos/room")


def wsl_to_win(p: Path) -> str:
    s = str(p)
    if s.startswith("/mnt/") and len(s) > 6:
        return f"{s[5].upper()}:{s[6:]}"
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description="抓一帧客厅 webcam")
    ap.add_argument("--out", help="输出文件（WSL 路径），默认 room/ 下带时间戳")
    ap.add_argument("--skip", type=int, default=25, help="丢弃的起始帧数，默认 25")
    ap.add_argument("--quiet", action="store_true", help="只打印最终路径")
    args = ap.parse_args()

    if not Path(FFMPEG_WIN).exists():
        print(f"ffmpeg 不在预期位置：{FFMPEG_WIN}", file=sys.stderr)
        return 1

    if args.out:
        out = Path(args.out)
    else:
        DEFAULT_DIR.mkdir(parents=True, exist_ok=True)
        out = DEFAULT_DIR / f"room_{datetime.now():%Y%m%d_%H%M%S}.jpg"

    cmd = [
        FFMPEG_WIN, "-hide_banner", "-loglevel", "error",
        "-f", "dshow", "-i", f"video={DEVICE}",
        "-vf", f"select=gte(n\\,{args.skip})",
        "-frames:v", "1", "-update", "1", "-y", wsl_to_win(out),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        print("抓帧超时（30s）——设备可能被占用或已拔出", file=sys.stderr)
        return 2

    if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        msg = (r.stderr or "").strip()
        if "I/O error" in msg or "busy" in msg.lower():
            print("摄像头打不开——多半被别的程序占用着，让位不重试", file=sys.stderr)
        else:
            print(f"抓帧失败：{msg or '未知错误'}", file=sys.stderr)
        return 2

    if args.quiet:
        print(out)
    else:
        kb = out.stat().st_size // 1024
        print(f"已抓帧（丢前 {args.skip} 帧，{kb} KB）→ {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
