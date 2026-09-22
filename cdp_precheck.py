#!/usr/bin/env python3
"""连 9222 之前先给每个标签页做个体检，把「哑页」挑出来。

    python3 cdp_precheck.py                # 逐页报告状态，什么都不动
    python3 cdp_precheck.py --close-dead   # 顺手关掉哑页（要显式传才会动手）

哑页指的是：Chrome 的 9222 HTTP 口回得好好的，`/json/list` 里这个 target 也在，
但它的渲染进程不再应答 DevTools 命令 —— 往那个 websocket 发一条
`Page.getFrameTree`，石沉大海。

这种页单独看不出毛病（浏览器里可能还显示着内容），坏就坏在
playwright 的 `chromium.connect_over_cdp()` 会 attach 所有 page target
并等它们各自初始化完：只要有一个哑页，整个 connect 就一直挂着，
直到超时为止。9/22 实测一个 `https://claude.ai/new` 页就足以拖死整条链路，
把它关掉之后 connect 0.8 秒就通。

所以调用方的用法是「连之前先问一句」：有哑页就别连，直接报出是哪一页，
让人自己决定关不关。这个模块不替用户关页 —— 那是他的浏览器。

依赖：标准库 + 可选的 websockets。没装 websockets 就只能做 HTTP 探活，
这时一律报告「未检查」，绝不假装页面是健康的。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "http://localhost:9222"

try:
    from websockets.sync.client import connect as _ws_connect
    WS_AVAILABLE = True
except ImportError:  # 没有 websockets 就退化成只做 HTTP 探活
    _ws_connect = None
    WS_AVAILABLE = False


def list_targets(base: str = DEFAULT_BASE, timeout: float = 5.0) -> list[dict]:
    """读 /json/list。连不上就抛出去，调用方自己判断。"""
    with urllib.request.urlopen(f"{base}/json/list", timeout=timeout) as resp:
        return json.load(resp)


def _probe_one(target: dict, timeout: float) -> str:
    """给一个 target 发 Page.getFrameTree，看它还答不答话。

    返回状态字符串：ok / dead / ws-fail。
    只发一条只读命令，不开任何 domain，不碰页面内容。
    """
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        return "ws-fail"
    try:
        with _ws_connect(ws_url, open_timeout=timeout, close_timeout=1) as ws:
            ws.send(json.dumps({"id": 1, "method": "Page.getFrameTree"}))
            # 握手之后可能先涌进来一堆事件，得一直读到 id=1 那条为止
            deadline = timeout
            while deadline > 0:
                t0 = time.monotonic()
                try:
                    raw = ws.recv(timeout=deadline)
                except TimeoutError:
                    return "dead"
                deadline -= time.monotonic() - t0
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if msg.get("id") == 1:
                    # 回了 error 也算活的 —— 渲染进程在说话就行
                    return "ok"
            return "dead"
    except TimeoutError:
        return "dead"
    except Exception:  # noqa: BLE001 — 握手失败/中途断线都只是「这页问不出来」
        return "ws-fail"


def probe_pages(base: str = DEFAULT_BASE, timeout: float = 2.0) -> list[dict]:
    """逐页体检，返回 [{"id","url","title","state"}]。

    state 取值：ok（应答了）、dead（没应答，就是它拖死 connect 的）、
    ws-fail（websocket 都连不上）、unchecked（没装 websockets）。
    HTTP 口本身不通时返回空列表 —— 那种情况 connect 自己就会快速失败，
    轮不到预检来兜。
    """
    try:
        targets = list_targets(base)
    except (urllib.error.URLError, OSError, ValueError):
        return []

    out: list[dict] = []
    for t in targets:
        if t.get("type") != "page":
            continue
        row = {
            "id": t.get("id", ""),
            "url": t.get("url", ""),
            "title": t.get("title", ""),
        }
        row["state"] = "unchecked" if not WS_AVAILABLE else _probe_one(t, timeout)
        out.append(row)
    return out


def find_dead_pages(base: str = DEFAULT_BASE, timeout: float = 2.0) -> list[dict]:
    """只把哑页挑出来：[{"id","url"}]。

    拿不准的一律不算哑页（没装 websockets、HTTP 口不通、websocket 连不上），
    宁可放行让原来的失败路径去报错，也不要拦住一次本来能成的连接。
    """
    return [
        {"id": p["id"], "url": p["url"]}
        for p in probe_pages(base, timeout)
        if p["state"] == "dead"
    ]


def close_target(target_id: str, base: str = DEFAULT_BASE,
                 timeout: float = 5.0) -> bool:
    """关掉一个 target（GET /json/close/<id>）。成功返回 True。"""
    try:
        with urllib.request.urlopen(
            f"{base}/json/close/{target_id}", timeout=timeout
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def warn_if_dead(base: str = DEFAULT_BASE, timeout: float = 2.0,
                 stream=sys.stderr) -> list[dict]:
    """连 CDP 之前调这个：有哑页就打印出来并返回，调用方据此别连。

    返回空列表表示可以连（或者没法判断，同样放行）。
    """
    dead = find_dead_pages(base, timeout)
    if dead:
        print(f"9222 里有 {len(dead)} 个标签页不应答 DevTools 命令，"
              f"连上去会一直挂着：", file=stream)
        for p in dead:
            print(f"  · {p['url'][:100]}", file=stream)
        print("用 python3 cdp_precheck.py --close-dead 关掉，或者自己去浏览器里关。",
              file=stream)
    return dead


def main() -> int:
    ap = argparse.ArgumentParser(description="9222 标签页体检")
    ap.add_argument("--base", default=DEFAULT_BASE, help="CDP HTTP 地址")
    ap.add_argument("--timeout", type=float, default=2.0,
                    help="每页等应答的秒数，默认 2")
    ap.add_argument("--close-dead", action="store_true",
                    help="关掉哑页（默认只报告，不动任何页）")
    args = ap.parse_args()

    try:
        list_targets(args.base)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"连不上 {args.base}（{type(e).__name__}: {e}）—— Chrome 没开调试口？",
              file=sys.stderr)
        return 2

    pages = probe_pages(args.base, args.timeout)
    if not WS_AVAILABLE:
        print("没装 websockets，只能确认 HTTP 口活着，页面状态未检查。"
              "装：pip install websockets", file=sys.stderr)

    if not pages:
        print("9222 里一个 page target 都没有。")
        return 0

    mark = {"ok": "正常", "dead": "哑页", "ws-fail": "连不上", "unchecked": "未检查"}
    for p in pages:
        print(f"[{mark.get(p['state'], p['state'])}] {p['url'][:90]}")

    dead = [p for p in pages if p["state"] == "dead"]
    if not dead:
        print(f"\n{len(pages)} 个页面，没有哑页，connect_over_cdp 可以放心连。")
        return 0

    print(f"\n{len(dead)} 个哑页 —— 它们会让 connect_over_cdp 挂死。")
    if not args.close_dead:
        print("要关的话加 --close-dead。")
        return 1

    for p in dead:
        ok = close_target(p["id"], args.base)
        print(f"{'关掉了' if ok else '没关成'}：{p['url'][:90]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
