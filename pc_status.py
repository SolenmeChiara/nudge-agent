"""Windows PC presence signals for the nudge context.

Three cheap, read-only probes (all best-effort, each degrades
independently):
  1. Keyboard/mouse idle time  -- GetLastInputInfo, answers "is Sol
     at the computer right now?"
  2. Foreground window          -- title + process exe, answers "doing
     what?"
  3. Chrome tab snapshot        -- CDP /json (titles + hosts only, no
     page content), answers "browsing what?"

Caveat for the agent: long idle does NOT always mean away -- watching
a video keeps hands off the keyboard too. Treat it as one signal, not
a verdict.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from config import CFG

TITLE_MAX = 60
TABS_MAX = 8
_SKIP_URL_PREFIXES = ("devtools://", "chrome-extension://", "chrome://", "about:")
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.UINT),
        ("dwTime", ctypes.wintypes.DWORD),
    ]


def _idle_seconds() -> float | None:
    """Seconds since the last keyboard/mouse input, or None on failure."""
    try:
        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return None
        # Both values are 32-bit ticks; they wrap together, so the
        # difference stays correct across the 49.7-day rollover.
        ticks = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
        return max(ticks, 0) / 1000.0
    except (OSError, AttributeError):
        return None


def _fmt_idle(seconds: float) -> str:
    if seconds < 60:
        return "刚刚有操作（<1 分钟）"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} 分钟"
    return f"{minutes // 60} 小时 {minutes % 60} 分"


def _foreground_window() -> str | None:
    """'window title (process.exe)' for the foreground window, or None."""
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if len(title) > TITLE_MAX:
            title = title[:TITLE_MAX] + "…"

        exe = ""
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
            )
            if handle:
                try:
                    size = ctypes.wintypes.DWORD(260)
                    path_buf = ctypes.create_unicode_buffer(size.value)
                    if kernel32.QueryFullProcessImageNameW(
                        handle, 0, path_buf, ctypes.byref(size)
                    ):
                        exe = Path(path_buf.value).name
                finally:
                    kernel32.CloseHandle(handle)

        if title and exe:
            return f"{title} ({exe})"
        return title or exe or None
    except (OSError, AttributeError):
        return None


def _chrome_tabs() -> list[str] | None:
    """Open Chrome tabs as '- title — host' lines, or None if CDP is down.

    Only tab metadata (title + URL host) is read; no page content.
    """
    try:
        req = urllib.request.Request(
            f"{CFG.cdp_url}/json",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            targets = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, ConnectionError, OSError, json.JSONDecodeError):
        return None

    lines = []
    for t in targets:
        if t.get("type") != "page":
            continue
        url = t.get("url", "")
        if not url or url.startswith(_SKIP_URL_PREFIXES):
            continue
        title = (t.get("title") or "").strip() or "(无标题)"
        if len(title) > TITLE_MAX:
            title = title[:TITLE_MAX] + "…"
        host = urlparse(url).netloc or "?"
        lines.append(f"- {title} — {host}")
    return lines


def check() -> str | None:
    """Assemble the '## 电脑状态' context block. None only if every probe fails."""
    idle = _idle_seconds()
    fg = _foreground_window()
    tabs = _chrome_tabs()

    lines = ["## 电脑状态"]
    if idle is not None:
        lines.append(f"键鼠空闲: {_fmt_idle(idle)}")
    if fg:
        lines.append(f"前台窗口: {fg}")

    if tabs is None:
        lines.append("Chrome: 未检测到（9222 调试端口未开或 Chrome 未运行）")
    elif not tabs:
        lines.append("Chrome 标签页: （只有空白页/内部页面）")
    else:
        shown = tabs[:TABS_MAX]
        header = f"Chrome 标签页 ({len(tabs)}):"
        if len(tabs) > TABS_MAX:
            shown.append(f"- …等 {len(tabs) - TABS_MAX} 个")
        lines.append(header)
        lines.extend(shown)

    if len(lines) == 1:
        return None
    return "\n".join(lines)


if __name__ == "__main__":
    print(check() or "(所有探测都失败了)")
