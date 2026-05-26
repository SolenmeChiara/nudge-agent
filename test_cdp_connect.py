"""Smoke test: connect to existing Chrome via CDP, list tabs, screenshot Claude.ai.

Prereq: Chrome must be running with `--remote-debugging-port=9222`.
Start it via start_chrome_debug.bat (after closing all other Chrome windows).

Usage:
  py test_cdp_connect.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

CDP_URL = "http://localhost:9222"
SCREENSHOT_PATH = Path(__file__).resolve().parent / "claude_tab.png"


def _probe_cdp() -> tuple[bool, str]:
    try:
        with urlopen(f"{CDP_URL}/json/version", timeout=3) as r:
            return r.status == 200, r.read().decode("utf-8", "replace")[:200]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ok, info = _probe_cdp()
    if not ok:
        print(f"[FAIL] CDP probe failed — {info}")
        print("  Chrome is not running with --remote-debugging-port=9222.")
        print("  Close all Chrome windows then run start_chrome_debug.bat.")
        return 1
    print(f"[OK] CDP reachable — {info}")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        contexts = browser.contexts
        print(f"\n[INFO] {len(contexts)} browser context(s)")

        claude_pages = []
        total_pages = 0
        for ci, ctx in enumerate(contexts):
            for pi, page in enumerate(ctx.pages):
                total_pages += 1
                url = page.url
                try:
                    title = page.title()
                except Exception:
                    title = "(title unavailable)"
                tag = "  " if "claude.ai" not in url else "→ "
                print(f"{tag}[ctx {ci} / page {pi}] {url}")
                print(f"    title: {title}")
                if "claude.ai" in url:
                    claude_pages.append(page)

        print(f"\n[INFO] {total_pages} total page(s), "
              f"{len(claude_pages)} Claude.ai page(s)")

        if not claude_pages:
            print("[WARN] No Claude.ai tab found — open one and re-run.")
            return 2

        target = claude_pages[0]
        try:
            target.bring_to_front()
        except Exception as e:
            print(f"[WARN] bring_to_front failed: {e}")

        target.screenshot(path=str(SCREENSHOT_PATH), full_page=False)
        print(f"\n[OK] Screenshot saved → {SCREENSHOT_PATH}")
        print(f"     URL: {target.url}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
