#!/usr/bin/env python3
"""把视频抓进 mind/Claude_photos/video_cache/，音轨视频自动合好。

    python3 grab_video.py BV1Tq3f6GEfv          # 裸 BV 号
    python3 grab_video.py https://b23.tv/xxxx   # 短链
    python3 grab_video.py <yt-dlp 支持的任意链接>
    python3 grab_video.py BV... --name 百万英镑数学   # 顺手起个描述性名字
    python3 grab_video.py BV... --chrome-cookies      # 一上来就带 Chrome 的 cookie
    python3 grab_video.py BV... --no-chrome-cookies   # 关掉 cookie 兜底，412 就直接失败

B 站从 2026-09-04 起会对没带 cookie 的裸请求回 412（风控），默认会自动兜底：
连上 9222 端口那个常开的 Windows Chrome，把登录态 cookie 导出来，
配上 Chrome 的 UA 和 referer 重试；还 412 就睡 4 秒再试最后一次。

环境变量 GRAB_VIDEO_CACHE 可以改缓存目录（测试用，平时不用管）。
同一个视频抓过就不再抓，直接把已缓存的路径吐出来。
最后会同时打印 WSL 路径和 Windows 路径 —— 后者可以直接喂给 gemini-video MCP。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CACHE = Path(
    os.environ.get("GRAB_VIDEO_CACHE")
    or Path(__file__).resolve().parent / "mind" / "Claude_photos" / "video_cache"
)
BV = re.compile(r"BV[0-9A-Za-z]{10}")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
MAX_MIN = 20  # 超过这个时长要 --force 才下，防手滑拉一部电影

CDP = "http://localhost:9222"  # Sol 常开的那个 Windows Chrome，登录态就在里面
COOKIE_FILE = Path.home() / ".cache" / "nudge-agent" / "bili_cookies.txt"
# 拿不到 Chrome 真实 UA 时的兜底值，跟 9222 那个实例保持同代
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
BILI_REFERER = "https://www.bilibili.com/"
RETRY_SLEEP = 4  # 风控是随机的，同一条命令睡几秒再来往往就过了


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


def is_bili(url: str) -> bool:
    return "bilibili.com" in url or "b23.tv" in url


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


def looks_412(blob: str) -> bool:
    """yt-dlp 的输出里有没有 412。"""
    return "412" in (blob or "")


def chrome_ua() -> str:
    """问 9222 那个 Chrome 自己的 UA，问不到就用写死的。"""
    try:
        with urllib.request.urlopen(f"{CDP}/json/version", timeout=5) as r:
            ua = json.load(r).get("User-Agent")
        if ua:
            return ua
    except Exception:
        pass
    return CHROME_UA


def export_cookies(page_url: str) -> Path | None:
    """连 9222 的 Chrome，开一个新标签页把视频页打开，导出 B 站 cookie。

    只关自己开的那个标签页，绝不动别的标签、更不关浏览器。
    拿不到就返回 None，调用方自己决定怎么办。
    """
    try:
        from playwright.sync_api import sync_playwright  # 懒加载：别的站用不着
    except ImportError:
        print("没装 playwright，拿不到 cookie。装：pip install playwright", file=sys.stderr)
        return None

    page = None
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(CDP)
            except Exception:
                print("9222 的 Chrome 没开，拿不到 cookie", file=sys.stderr)
                return None
            if not browser.contexts:
                print("9222 的 Chrome 没有可用的窗口，拿不到 cookie", file=sys.stderr)
                return None
            ctx = browser.contexts[0]
            page = ctx.new_page()
            try:
                # 先把视频页打开，让风控该发的 cookie 都发下来
                page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(3)
                cookies = ctx.cookies(
                    ["https://www.bilibili.com", "https://api.bilibili.com"]
                )
            finally:
                try:
                    page.close()  # 只关自己开的这一页
                except Exception:
                    pass
            if not cookies:
                print("Chrome 里没有 B 站的 cookie（没登录？）", file=sys.stderr)
                return None
            _write_netscape(cookies)
            print(f"从 Chrome 拿到 {len(cookies)} 条 cookie")
            return COOKIE_FILE
    except Exception as e:
        print(f"导 cookie 出岔子了（{e}）", file=sys.stderr)
        return None


def _write_netscape(cookies: list) -> None:
    """写成 yt-dlp 认的 Netscape 格式，权限 0600，每次覆盖。"""
    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        dom = c["domain"]
        exp = int(c.get("expires") or 0)
        lines.append(
            "\t".join(
                [
                    dom,
                    "TRUE" if dom.startswith(".") else "FALSE",
                    c.get("path") or "/",
                    "TRUE" if c.get("secure") else "FALSE",
                    str(max(exp, 0)),
                    c["name"],
                    c["value"],
                ]
            )
        )
    fd = os.open(COOKIE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(COOKIE_FILE, 0o600)  # 文件本来就在的话 O_CREAT 的 mode 不生效


class BiliAuth:
    """B 站 412 的兜底开关：按需掏一次 cookie，掏到就一直用。"""

    def __init__(self, enabled: bool, forced: bool, page_url: str):
        self.enabled = enabled  # 允许走 cookie 兜底
        self.forced = forced  # 一上来就带 cookie
        self.page_url = page_url
        self._args: list | None = None
        self._failed = False  # 掏过一次没掏到就别再折腾

    def args(self) -> list | None:
        """要额外带给 yt-dlp 的参数；拿不到 cookie 返回 None。"""
        if self._args is not None:
            return self._args
        if self._failed or not self.enabled:
            return None
        path = export_cookies(self.page_url)
        if path is None:
            self._failed = True
            return None
        self._args = [
            "--cookies", str(path),
            "--user-agent", chrome_ua(),
            "--referer", BILI_REFERER,
        ]
        return self._args


def run_ytdlp(build_cmd, auth: BiliAuth, runner):
    """跑 yt-dlp，遇到 412 就按「裸跑 → 带 cookie → 睡 4 秒再带 cookie」退避。

    build_cmd(extra) 把附加参数拼进完整命令；
    runner(cmd) 负责真正执行，返回 (CompletedProcess, 用来找 412 的文本)。
    """
    extra: list = []
    if auth.forced:
        extra = auth.args() or []
    used_cookie = bool(extra)
    slept = False

    while True:
        proc, blob = runner(build_cmd(extra))
        if proc.returncode == 0 or not looks_412(blob) or not auth.enabled:
            return proc
        if not used_cookie:
            print("\nB 站回了 412（风控），去 9222 的 Chrome 拿 cookie 再试 ...")
            nxt = auth.args()
            if nxt is None:
                return proc
            extra, used_cookie = nxt, True
            auth.forced = True  # 后面的命令直接带上，别再白挨一次 412
            continue
        if not slept:
            print(f"\n带着 cookie 还是 412，睡 {RETRY_SLEEP} 秒再试最后一次 ...")
            time.sleep(RETRY_SLEEP)
            slept = True
            continue
        return proc


def main() -> int:
    ap = argparse.ArgumentParser(description="抓视频进 video_cache，音视频自动合并")
    ap.add_argument("target", help="BV 号、b23.tv 短链，或任意 yt-dlp 支持的链接")
    ap.add_argument("--name", help="给文件起个描述性名字（不含扩展名）")
    ap.add_argument("--height", type=int, default=1080, help="最高分辨率，默认 1080")
    ap.add_argument("--force", action="store_true", help="超时长/已缓存也照抓")
    g = ap.add_mutually_exclusive_group()
    g.add_argument(
        "--chrome-cookies", dest="chrome_cookies", action="store_true",
        help="B 站：一上来就从 9222 的 Chrome 拿 cookie（默认碰到 412 才拿）",
    )
    g.add_argument(
        "--no-chrome-cookies", dest="chrome_cookies", action="store_false",
        help="B 站：关掉 cookie 兜底，412 就直接失败",
    )
    ap.set_defaults(chrome_cookies=None)  # None = 自动
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

    bili = is_bili(url)
    if args.chrome_cookies and not bili:
        print("--chrome-cookies 只对 B 站有用，这个链接照常抓。")
    auth = BiliAuth(
        enabled=bili and args.chrome_cookies is not False,
        forced=bool(bili and args.chrome_cookies),
        page_url=url,
    )

    def capture_runner(cmd):
        p = run(cmd, capture_output=True)
        return p, (p.stderr or "")

    def live_runner(cmd):
        # 只截 stderr，进度条照常走到屏幕上
        p = run(cmd, stderr=subprocess.PIPE)
        if p.returncode != 0 and p.stderr:
            print(p.stderr.strip()[-600:], file=sys.stderr)
        return p, (p.stderr or "")

    # 先问元信息：拿 id 判断缓存，拿时长防手滑
    print(f"查 {url} ...")
    # 带 ?p=N 时不能加 --no-playlist：B 站分 P 在 yt-dlp 眼里是 playlist 项，
    # --no-playlist 会无视 p 参数退回第一 P（2026-08-10 实测）
    no_playlist = [] if "?p=" in url else ["--no-playlist"]
    probe = run_ytdlp(
        lambda extra: [
            ytdlp, *no_playlist, *extra, "--dump-single-json", "--no-warnings",
            "--socket-timeout", "20", url,
        ],
        auth,
        capture_runner,
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

    def build_dl(extra):
        return [
            ytdlp, *no_playlist, *extra, "--no-warnings",
            "-f", fmt,
            "--merge-output-format", "mp4",
            "--concurrent-fragments", "4",
            "--progress-delta", "3",
            "-o", str(CACHE / f"{stem}.%(ext)s"),
            url,
        ]

    print("\n开抓 ...")
    if run_ytdlp(build_dl, auth, live_runner).returncode != 0:
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
