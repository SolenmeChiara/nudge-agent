#!/usr/bin/env python3
"""把视频抓进 mind/Claude_photos/video_cache/，音轨视频自动合好。

    python3 grab_video.py BV1Tq3f6GEfv          # 裸 BV 号
    python3 grab_video.py https://b23.tv/xxxx   # 短链
    python3 grab_video.py https://xhslink.cn/o/xxxx   # 小红书短链
    python3 grab_video.py <yt-dlp 支持的任意链接>
    python3 grab_video.py BV... --name 百万英镑数学   # 顺手起个描述性名字
    python3 grab_video.py BV... --chrome-cookies      # 一上来就带 Chrome 的 cookie
    python3 grab_video.py BV... --no-chrome-cookies   # 关掉 cookie 兜底，412 就直接失败
    python3 grab_video.py <链接> --speed 2            # 下完再压一个两倍速版
    python3 grab_video.py <抖音链接> --cdp-first      # 抖音先走 Chrome，别先试 yt-dlp

B 站从 2026-09-04 起会对没带 cookie 的裸请求回 412（风控），默认会自动兜底：
连上 9222 端口那个常开的 Windows Chrome，把登录态 cookie 导出来，
配上 Chrome 的 UA 和 referer 重试；还 412 就睡 4 秒再试最后一次。

抖音（v.douyin.com 短链或 www.douyin.com/video/... 页）有两条路，默认先走
yt-dlp 带 ~/.cache/nudge-agent/douyin_cookies.txt 直下 —— 9/22 实测这份
两周前导出的 cookie 还认，几秒就完；没有 cookie 文件时也裸试一次。这条不通
才连 9222 的 Chrome 走专线：开视频页读 DOM 里 video/source 的真实地址，读不到
再从网络流量里捞 douyinvod/zjcdn，拿到就在同一趟里立刻用 curl 下（直链带签名
时效，存着后用会过期），顺手把新 cookie 写回那个文件喂给下次。--cdp-first
可以把顺序倒过来。--height 对抖音专线无效（播放器给什么拿什么）。

小红书（xhslink.cn 短链、xiaohongshu.com/explore/ 或 /discovery/item/ 页）
交给 yt-dlp 直下，不用 cookie。短链展开后 query 里的 xsec_token / xsec_source
是通行证，一个都不能削，所以这条路径绕开 normalize()。

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

DOUYIN_REFERER = "https://www.douyin.com/"
DOUYIN_COOKIE_FILE = Path.home() / ".cache" / "nudge-agent" / "douyin_cookies.txt"
# 解抖音短链用移动端 UA，桌面 UA 有时被引去落地页而不是重定向
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

XHS_HOSTS = ("xhslink.cn", "xhslink.com", "xiaohongshu.com")
XHS_REFERER = "https://www.xiaohongshu.com/"
XHS_NOTE = re.compile(r"/(?:explore|discovery/item|item)/([0-9A-Za-z]+)")
CDP_TIMEOUT = 15000  # 毫秒。9222 的 HTTP 口活着但 websocket 挂死过（9/22），别裸等
PROBE_TIMEOUT = 90  # 秒。问元信息的墙钟上限，别让一条挂死的路拖住换路


def normalize(url: str) -> str:
    """B 站链接一律削成干净的 BV 页。

    手机分享出来的 b23.tv 短链展开后拖着一长串 story 参数
    （-Arouter=story、is_story_h5、share_session_id ……），
    yt-dlp 拿到会走进竖屏 story 的提取路径然后挂死
    —— 2026-08-04 实测卡过三分钟不动。把参数全削掉就好了。
    """
    if re.fullmatch(r"BV[0-9A-Za-z]{10}", url):
        return f"https://www.bilibili.com/video/{url}"
    if "douyin.com" in url:
        # 抖音也削成干净的视频页；短链（v.douyin.com）先展开
        m = re.search(r"/video/(\d+)", url)
        if not m:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": MOBILE_UA})
                with urllib.request.urlopen(req, timeout=15) as r:
                    url = r.url
                m = re.search(r"/video/(\d+)", url)
            except Exception as e:
                print(f"抖音短链没解开（{e}）")
        return f"https://www.douyin.com/video/{m.group(1)}" if m else url
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


def is_douyin(url: str) -> bool:
    return "douyin.com" in url


def is_xhs(url: str) -> bool:
    return any(h in url for h in XHS_HOSTS)


def win_path(p: Path) -> str:
    """/mnt/d/x → D:/x，给 gemini-video MCP 用。"""
    s = str(p)
    if s.startswith("/mnt/") and len(s) > 6 and s[6] == "/":
        return f"{s[5].upper()}:{s[6:]}"
    return s


def human(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n else "未知大小"


def shorten(s: str, n: int = 140) -> str:
    """长链接打屏幕上只留个头，省得把 xsec_token 那一长串铺满一行。"""
    return s if len(s) <= n else s[:n] + " …"


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", **kw)


def looks_412(blob: str) -> bool:
    """yt-dlp 的输出里有没有 412。"""
    return "412" in (blob or "")


def cached(stem: str) -> Path | None:
    """缓存里有没有这个名字的成品（.part 是下一半的残骸，不算）。"""
    return next((p for p in CACHE.glob(f"{stem}.*") if p.suffix != ".part"), None)


def report(out: Path, args, was_cached: bool = False) -> int:
    """统一的收尾：打两行路径，顺带按 --speed 压个加速版。"""
    if was_cached:
        print(f"已经在缓存里了（{human(out.stat().st_size)}），没重抓。")
    else:
        print(f"\n好了：{human(out.stat().st_size)}")
    print(f"WSL     ：{out}")
    print(f"Windows ：{win_path(out)}")
    speed_copy(out, getattr(args, "speed", None),
               force=getattr(args, "force", False))
    return 0


def atempo_chain(n: float) -> str:
    """atempo 单次最多 2.0，更快就得串起来：4 倍 = atempo=2.0,atempo=2.0。"""
    parts, rest = [], float(n)
    while rest > 2.0 + 1e-9:
        parts.append("atempo=2.0")
        rest /= 2.0
    if abs(rest - 1.0) > 1e-9:
        parts.append(f"atempo={rest:g}")
    return ",".join(parts) or "anull"


def speed_copy(src: Path, n, force: bool = False) -> Path | None:
    """压一个 N 倍速的小版本放在原片旁边，失败了不影响原片。

    压过就不重压——同一条命令跑第二遍时原片已经命中缓存，加速版却会
    重新编码一遍（20 分钟的片子要几分钟 CPU）。要重压加 --force。
    """
    if not n:
        return None
    n = float(n)
    if n <= 1:
        print("--speed 要大于 1 才有意义，跳过。", file=sys.stderr)
        return None
    if not shutil.which("ffmpeg"):
        print("找不到 ffmpeg，加速版跳过。装：sudo apt install ffmpeg", file=sys.stderr)
        return None

    dst = src.with_name(f"{src.stem}_{n:g}x.mp4")
    if dst.exists() and not force:
        print(f"\n加速版已经有了（{human(dst.stat().st_size)}），没重压。")
        print(f"WSL     ：{dst}")
        print(f"Windows ：{win_path(dst)}")
        return dst
    head = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
        "-filter:v", f"setpts=PTS/{n:g},scale=-2:540",
    ]
    tail = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26", str(dst)]
    print(f"\n压 {n:g} 倍速版 ...")
    proc = run(
        head + ["-filter:a", atempo_chain(n), "-c:a", "aac", "-b:a", "96k"] + tail,
        capture_output=True,
    )
    if proc.returncode != 0:
        # 没有音轨的片子 -filter:a 会直接报错，退一步做纯画面版
        print("音轨那步没过，改压纯画面版（加速版会没声音）。", file=sys.stderr)
        proc = run(head + ["-an"] + tail, capture_output=True)
    if proc.returncode != 0 or not dst.exists():
        print("加速版没压出来：", (proc.stderr or "").strip()[-400:], file=sys.stderr)
        dst.unlink(missing_ok=True)
        return None
    print(f"加速版：{human(dst.stat().st_size)}")
    print(f"WSL     ：{dst}")
    print(f"Windows ：{win_path(dst)}")
    return dst


def probe_duration(ytdlp: str, url: str, cookie: Path | None = None) -> float | None:
    """只问时长不下载。问不到返回 None —— 问不到不该挡住下载。"""
    cmd = [ytdlp, "--print", "duration", "--no-download", "--no-warnings",
           "--socket-timeout", "20"]
    if cookie:
        cmd += ["--cookies", str(cookie)]
    try:
        proc = run(cmd + [url], capture_output=True, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f"问时长等了 {PROBE_TIMEOUT} 秒还没回，当这条路没通。", file=sys.stderr)
        return None
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        try:
            return float(line.strip())
        except ValueError:
            continue
    return None


def ytdlp_fetch(
    ytdlp: str,
    url: str,
    stem: str,
    *,
    cookie: Path | None = None,
    referer: str | None = None,
    fmt: str | None = None,
    force: bool = False,
) -> Path | None:
    """用 yt-dlp 把 url 下到 CACHE/<stem>.<ext>，成了返回路径，没成返回 None。

    -c 是续传：被 timeout 杀掉留下的 .part 下次接着下，不从头来。
    force 时加 --force-overwrites，否则 yt-dlp 见同名成品就直接说「下过了」
    然后原地返回 0，--force 等于没加。
    """
    cmd = [ytdlp, "--no-warnings", "-c", "--socket-timeout", "20",
           "--concurrent-fragments", "4", "--progress-delta", "3",
           "--merge-output-format", "mp4"]
    if force:
        cmd.append("--force-overwrites")
    if cookie:
        cmd += ["--cookies", str(cookie)]
    if referer:
        cmd += ["--referer", referer]
    if fmt:
        cmd += ["-f", fmt]
    cmd += ["-o", str(CACHE / f"{stem}.%(ext)s"), url]

    proc = run(cmd, stderr=subprocess.PIPE)  # 进度条照常走到屏幕上
    out = cached(stem)
    # 太小的多半是错误页伪装的，不算数
    if proc.returncode == 0 and out and out.stat().st_size > 100_000:
        return out
    if proc.stderr:
        print("\n".join((proc.stderr or "").strip().splitlines()[-6:]), file=sys.stderr)
    elif proc.returncode == 0:
        print(f"yt-dlp 说下完了，但文件不见或小得不像视频（{out}）", file=sys.stderr)
    return None


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
                browser = p.chromium.connect_over_cdp(CDP, timeout=CDP_TIMEOUT)
            except Exception as e:
                print(f"连不上 9222 的 Chrome（{e}），拿不到 cookie", file=sys.stderr)
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


def _write_netscape(cookies: list, path: Path = COOKIE_FILE) -> None:
    """写成 yt-dlp/curl 认的 Netscape 格式，权限 0600，每次覆盖。"""
    path.parent.mkdir(parents=True, exist_ok=True)
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
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)  # 文件本来就在的话 O_CREAT 的 mode 不生效


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


def douyin_ytdlp(ytdlp: str, url: str, stem: str, args) -> tuple[int, Path | None]:
    """抖音第一条路：直接 yt-dlp。

    文件头那句「抖音不经 yt-dlp」是 9/8 写的，9/22 被推翻——手里有
    douyin_cookies.txt（上次 CDP 专线顺手导的）时 yt-dlp 就认，几秒下完，
    而且完全不依赖 Chrome 开没开。没有 cookie 文件也裸试一次，不亏。

    返回 (状态码, 文件)：0 成功、3 太长、其他非零表示这条路没走通。
    """
    cookie = DOUYIN_COOKIE_FILE if DOUYIN_COOKIE_FILE.exists() else None
    print("走 yt-dlp，带上存着的 cookie ..." if cookie
          else "走 yt-dlp（手里没有抖音 cookie 文件，裸试一次）...")

    dur = probe_duration(ytdlp, url, cookie)
    if dur is None:
        print("yt-dlp 问不到这条视频的信息。", file=sys.stderr)
        return 4, None
    dur = int(dur)
    if dur:
        print(f"时长：{dur // 60}:{dur % 60:02d}")
    if dur > MAX_MIN * 60 and not args.force:
        print(f"\n比 {MAX_MIN} 分钟长，先没抓。真要的话加 --force。", file=sys.stderr)
        return 3, None

    out = ytdlp_fetch(ytdlp, url, stem, cookie=cookie,
                      referer=DOUYIN_REFERER, force=args.force)
    return (0, out) if out else (4, None)


def douyin_cdp(url: str, stem: str, args) -> tuple[int, Path | None]:
    """抖音第二条路：连 9222 的 Chrome 捞直链。

    管线是 9/8 两个循环各验通一半后合的：开视频页，先读 DOM 里 video/source
    的 src（不带 cookie、只配 UA＋referer 就能下），src 是 blob: 或没挂出来时
    退到网络流量里捞 douyinvod/zjcdn 的响应地址。直链带签名时效，捞到必须
    同一趟里立刻下，不能存着后用。走通的话顺手把 cookie 写回文件喂给下次。

    返回 (状态码, 文件)：0 成功、3 太长、其他非零表示这条路没走通。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("没装 playwright，Chrome 专线走不了。装：pip install playwright",
              file=sys.stderr)
        return 1, None

    net_urls: list = []
    dom_srcs: list = []
    title, dur, ua, cookies = "", 0, chrome_ua(), []
    print(f"开 {url} ...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CDP, timeout=CDP_TIMEOUT)
            if not browser.contexts:
                print("9222 的 Chrome 没有可用的窗口", file=sys.stderr)
                return 2, None
            ctx = browser.contexts[0]
            page = ctx.new_page()

            def on_resp(r):
                u = r.url
                ct = r.headers.get("content-type") or ""
                if "douyinvod" in u or "zjcdn" in u or ct.startswith("video/"):
                    net_urls.append(u)

            page.on("response", on_resp)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                time.sleep(8)  # 等播放器起来、真实地址挂进 DOM
                dom_srcs = page.evaluate(
                    "() => [...document.querySelectorAll('video, video source')]"
                    ".map(v => v.src).filter(Boolean)"
                ) or []
                dur = page.evaluate(
                    "() => { const v = document.querySelector('video');"
                    " return v && isFinite(v.duration) ? v.duration : 0; }"
                ) or 0
                title = page.title() or ""
                ua = page.evaluate("navigator.userAgent") or ua
                cookies = ctx.cookies(["https://www.douyin.com"])
            finally:
                try:
                    page.close()  # 只关自己开的这一页
                except Exception:
                    pass
    except Exception as e:
        # 9/22 实测：9222 的 HTTP 口回得好好的，websocket 却一直挂着不握手。
        # 所以这里不光要接连不上，还要接超时和中途断线，接住了就换另一条路。
        print(f"Chrome 专线没走通（{type(e).__name__}: {e}）", file=sys.stderr)
        return 2, None

    title = re.sub(r"\s*-\s*抖音.*$", "", title.strip())
    if title:
        print(f"《{title}》")
    dur = int(dur)
    if dur:
        print(f"时长：{dur // 60}:{dur % 60:02d}")
    if dur > MAX_MIN * 60 and not args.force:
        print(f"\n比 {MAX_MIN} 分钟长，先没抓。真要的话加 --force。", file=sys.stderr)
        return 3, None

    # DOM 的 src 是最干净的直链，排前面；blob: 是播放器内部句柄，下不了
    candidates = [u for u in dom_srcs if u.startswith("http")] + net_urls
    if not candidates:
        print("DOM 和网络流量里都没捞到视频地址（登录墙？页面没放出来？）", file=sys.stderr)
        return 4, None

    cookie_args: list = []
    if cookies:
        _write_netscape(cookies, DOUYIN_COOKIE_FILE)
        cookie_args = ["-b", str(DOUYIN_COOKIE_FILE)]

    out = CACHE / f"{stem}.mp4"
    for cand in dict.fromkeys(candidates):  # 去重保序
        print(f"\n下 {cand[:110]} ...")
        proc = run([
            "curl", "-fSL", "--retry", "2", "-o", str(out),
            "-A", ua, "-e", DOUYIN_REFERER, *cookie_args, cand,
        ])
        # 太小的多半是错误页伪装的，不算数
        if proc.returncode == 0 and out.exists() and out.stat().st_size > 100_000:
            return 0, out
        out.unlink(missing_ok=True)
    print("候选地址都没下成。", file=sys.stderr)
    return 4, None


def douyin_grab(url: str, args, ytdlp: str) -> int:
    """抖音：两条路按顺序试，哪条先通算哪条。"""
    m = re.search(r"/video/(\d+)", url)
    vid = m.group(1) if m else "unknown"
    stem = args.name or f"douyin_{vid}"

    hit = cached(stem)
    if hit and not args.force:
        return report(hit, args, was_cached=True)

    # 有 cookie 文件就先试 yt-dlp（几秒完事且不用 Chrome）；没有就先走
    # 专线——专线能顺手把 cookie 导出来，下次就轮到 yt-dlp 打头了
    order = ["cdp", "ytdlp"] if (args.cdp_first or not DOUYIN_COOKIE_FILE.exists()) \
        else ["ytdlp", "cdp"]

    last = 4
    for i, which in enumerate(order):
        if i:
            print(f"\n换一条路试试（{which}）...")
        code, out = (douyin_ytdlp(ytdlp, url, stem, args) if which == "ytdlp"
                     else douyin_cdp(url, stem, args))
        if code == 0 and out:
            return report(out, args)
        if code == 3:
            return 3  # 是用户自己该拿主意的事（太长），别再换路重试
        last = code
    print("\n两条路都没下成。", file=sys.stderr)
    return last


def xhs_grab(url: str, args, ytdlp: str) -> int:
    """小红书：短链展开后原样交给 yt-dlp，不用 cookie。

    短链 302 到 /explore/<note_id>?xsec_token=...&xsec_source=...，那串
    token 是通行证，削掉 query 就 404，所以这条路径刻意不过 normalize()。
    """
    if "xhslink" in url:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": MOBILE_UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                url = r.url  # urlopen 自己跟完重定向，query 原样带着
            print(f"短链展开：{shorten(url)}")
        except Exception as e:
            print(f"小红书短链没解开（{e}），原样试试")

    m = XHS_NOTE.search(url)
    stem = args.name or f"xhs_{m.group(1) if m else 'unknown'}"

    hit = cached(stem)
    if hit and not args.force:
        return report(hit, args, was_cached=True)

    print(f"查 {shorten(url)} ...")
    dur = probe_duration(ytdlp, url)
    if dur is None:
        print("拉不到这条笔记的信息（token 过期了？不是视频笔记？）", file=sys.stderr)
        return 2
    dur = int(dur)
    if dur:
        print(f"时长：{dur // 60}:{dur % 60:02d}")
    if dur > MAX_MIN * 60 and not args.force:
        print(f"\n比 {MAX_MIN} 分钟长，先没抓。真要的话加 --force。", file=sys.stderr)
        return 3

    h = args.height
    fmt = f"bv*[height<={h}]+ba/b[height<={h}]/b"
    print("\n开抓 ...")
    out = ytdlp_fetch(ytdlp, url, stem, referer=XHS_REFERER, fmt=fmt,
                      force=args.force)
    if not out:
        print("抓失败了。", file=sys.stderr)
        return 4
    return report(out, args)


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
    ap.add_argument(
        "target",
        help="BV 号、b23.tv／抖音／小红书短链，或任意 yt-dlp 支持的链接",
    )
    ap.add_argument("--name", help="给文件起个描述性名字（不含扩展名）")
    ap.add_argument("--height", type=int, default=1080, help="最高分辨率，默认 1080")
    ap.add_argument(
        "--force", action="store_true",
        help="超时长/已缓存也照抓（加速版也一并重压）",
    )
    ap.add_argument(
        "--speed", type=float, metavar="N",
        help="下完再压一个 N 倍速的小版本（<名字>_Nx.mp4，540p），要 ffmpeg",
    )
    ap.add_argument(
        "--cdp-first", dest="cdp_first", action="store_true",
        help="抖音：先走 9222 的 Chrome 专线，别先试 yt-dlp（默认反过来）",
    )
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

    raw = args.target.strip()
    CACHE.mkdir(parents=True, exist_ok=True)

    # 小红书要保住 query 里的 xsec_token，绝不能过 normalize()
    if is_xhs(raw):
        if args.chrome_cookies is not None:
            print("--chrome-cookies 开关只对 B 站有意义，小红书这条不用 cookie。")
        return xhs_grab(raw, args, ytdlp)

    url = normalize(raw)

    if is_douyin(url):
        if args.chrome_cookies is not None:
            print("--chrome-cookies 开关只对 B 站有意义，抖音管线自己处理 cookie。")
        return douyin_grab(url, args, ytdlp)
    if args.cdp_first:
        print("--cdp-first 只对抖音有意义，这个链接照常抓。")

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

    hit = cached(stem)
    if hit and not args.force:
        print()
        return report(hit, args, was_cached=True)

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
            # 不加这个，--force 碰上已存在的成品 yt-dlp 只会说「下过了」
            *(["--force-overwrites"] if args.force else []),
            # 与上面探测那次同一个值。只卡单次 socket 读写，不是整段下载的
            # 墙钟上限——长视频本来就该慢慢下。
            "--socket-timeout", "20",
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

    out = cached(stem)
    if not out:
        print("下完了却找不到文件，去 video_cache 里翻翻。", file=sys.stderr)
        return 5

    return report(out, args)


if __name__ == "__main__":
    sys.exit(main())
