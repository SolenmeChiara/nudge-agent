#!/usr/bin/env python3
"""把视频抓进 mind/Claude_photos/video_cache/，音轨视频自动合好。

    python3 grab_video.py BV1Tq3f6GEfv          # 裸 BV 号
    python3 grab_video.py https://b23.tv/xxxx   # 短链
    python3 grab_video.py <yt-dlp 支持的任意链接>
    python3 grab_video.py BV... --name 百万英镑数学   # 顺手起个描述性名字

同一个视频抓过就不再抓，直接把已缓存的路径吐出来。
最后会同时打印 WSL 路径和 Windows 路径 —— 后者可以直接喂给 gemini-video MCP。
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

CACHE = Path(__file__).resolve().parent / "mind" / "Claude_photos" / "video_cache"
BV = re.compile(r"BV[0-9A-Za-z]{10}")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
MAX_MIN = 20  # 超过这个时长要 --force 才下，防手滑拉一部电影


def normalize(url: str) -> str:
    """B 站链接一律削成干净的 BV 页。

    手机分享出来的 b23.tv 短链展开后拖着一长串 story 参数
    （-Arouter=story、is_story_h5、share_session_id ……），
    yt-dlp 拿到会走进竖屏 story 的提取路径然后挂死
    —— 2026-08-04 实测卡过三分钟不动。把参数全削掉就好了。
    """
    if re.fullmatch(r"BV[0-9A-Za-z]{10}", url):
        return f"https://www.bilibili.com/video/{url}"
    if "bilibili.com" not in url and "b23.tv" not in url:
        return url  # 别的站交给 yt-dlp 自己认

    m = BV.search(url)
    if not m and "b23.tv" in url:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                url = r.url
            m = BV.search(url)
        except Exception as e:
            print(f"短链没解开（{e}），原样交给 yt-dlp 试试")
    if not m:
        return url
    base = f"https://www.bilibili.com/video/{m.group(0)}"
    # 分 P 参数是唯一要保住的查询参数，削掉就永远只能下到第一 P
    m_p = re.search(r"[?&]p=(\d+)", url)
    if m_p and int(m_p.group(1)) > 1:
        base += f"?p={m_p.group(1)}"
    return base


def win_path(p: Path) -> str:
    """/mnt/d/x → D:/x，给 gemini-video MCP 用。"""
    s = str(p)
    if s.startswith("/mnt/") and len(s) > 6 and s[6] == "/":
        return f"{s[5].upper()}:{s[6:]}"
    return s


def human(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n else "未知大小"


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", **kw)


def main() -> int:
    ap = argparse.ArgumentParser(description="抓视频进 video_cache，音视频自动合并")
    ap.add_argument("target", help="BV 号、b23.tv 短链，或任意 yt-dlp 支持的链接")
    ap.add_argument("--name", help="给文件起个描述性名字（不含扩展名）")
    ap.add_argument("--height", type=int, default=1080, help="最高分辨率，默认 1080")
    ap.add_argument("--force", action="store_true", help="超时长/已缓存也照抓")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # 否则自己的话会被 yt-dlp 的输出压到最后

    ytdlp = shutil.which("yt-dlp") or str(Path.home() / ".local/bin/yt-dlp")
    if not Path(ytdlp).exists():
        print("找不到 yt-dlp。装：pip install -U yt-dlp", file=sys.stderr)
        return 1
    if not shutil.which("ffmpeg"):
        print("找不到 ffmpeg，音视频合不了。装：sudo apt install ffmpeg", file=sys.stderr)
        return 1

    url = normalize(args.target.strip())
    CACHE.mkdir(parents=True, exist_ok=True)

    # 先问元信息：拿 id 判断缓存，拿时长防手滑
    print(f"查 {url} ...")
    # 带 ?p=N 时不能加 --no-playlist：B 站分 P 在 yt-dlp 眼里是 playlist 项，
    # --no-playlist 会无视 p 参数退回第一 P（2026-08-10 实测）
    no_playlist = [] if "?p=" in url else ["--no-playlist"]
    probe = run(
        [ytdlp, *no_playlist, "--dump-single-json", "--no-warnings",
         "--socket-timeout", "20", url],
        capture_output=True,
    )
    if probe.returncode != 0:
        print("拉不到视频信息：", (probe.stderr or "").strip()[-600:], file=sys.stderr)
        return 2
    info = json.loads(probe.stdout)

    vid = info.get("id") or "unknown"
    title = info.get("title") or "(无标题)"
    dur = int(info.get("duration") or 0)
    uploader = info.get("uploader") or ""
    site = (info.get("extractor_key") or "site").lower()
    stem = args.name or (f"bili_{vid}" if site.startswith("bili") else f"{site}_{vid}")

    print(f"《{title}》")
    if uploader:
        print(f"UP：{uploader}")
    print(f"时长：{dur // 60}:{dur % 60:02d}")

    hit = next((p for p in CACHE.glob(f"{stem}.*") if p.suffix != ".part"), None)
    if hit and not args.force:
        print(f"\n已经在缓存里了（{human(hit.stat().st_size)}），没重抓。")
        print(f"WSL     ：{hit}")
        print(f"Windows ：{win_path(hit)}")
        return 0

    if dur > MAX_MIN * 60 and not args.force:
        print(f"\n比 {MAX_MIN} 分钟长，先没抓。真要的话加 --force。", file=sys.stderr)
        return 3

    # 优先 H.264：下游要交给 gemini-video 抽帧，兼容性比编码效率值钱
    h = args.height
    fmt = (
        f"bv*[height<={h}][vcodec^=avc]+ba/bv*[height<={h}]+ba"
        f"/b[height<={h}]/bv*+ba/b"
    )
    cmd = [
        ytdlp, *no_playlist, "--no-warnings",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "--concurrent-fragments", "4",
        "--progress-delta", "3",
        "-o", str(CACHE / f"{stem}.%(ext)s"),
        url,
    ]
    print("\n开抓 ...")
    if run(cmd).returncode != 0:
        print("抓失败了。", file=sys.stderr)
        return 4

    out = next((p for p in CACHE.glob(f"{stem}.*") if p.suffix != ".part"), None)
    if not out:
        print("下完了却找不到文件，去 video_cache 里翻翻。", file=sys.stderr)
        return 5

    print(f"\n好了：{human(out.stat().st_size)}")
    print(f"WSL     ：{out}")
    print(f"Windows ：{win_path(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
