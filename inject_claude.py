"""Inject a message into the Claude.ai chat input via CDP.

Usage:
  py inject_claude.py "消息内容"                    # type in current tab
  py inject_claude.py "消息内容" --conv-id UUID     # navigate to conversation first
  py inject_claude.py "消息内容" --new              # start a new conversation
  py inject_claude.py "消息内容" --dry-run          # type but don't send
  py inject_claude.py "消息内容" --screenshot       # save screenshot after action

Connects to an existing Chrome instance via --remote-debugging-port=9222.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

SCRIPT_DIR = Path(__file__).resolve().parent

# Lazy config import — inject_claude.py can also run standalone from WSL
# where config.json may not be on the Python path.
try:
    from config import CFG as _CFG
    CDP_URL = _CFG.cdp_url
except ImportError:
    CDP_URL = "http://localhost:9222"
INJECT_TIMEOUT = 60_000  # hard cap: 60s for the entire operation

CLAUDE_INPUT_SELECTORS = [
    "div.ProseMirror[contenteditable='true']",
    "fieldset div[contenteditable='true']",
    "div[contenteditable='true']",
]

SEND_BUTTON_SELECTORS = [
    "button[aria-label='Send message']",
    "button[aria-label='Send Message']",
    "fieldset button[type='button']:last-child",
]

_SKIP_TITLE_KEYWORDS = {"test", "injection", "automated", "nudge-agent"}


def _find_claude_page(contexts) -> Page | None:
    for ctx in contexts:
        for page in ctx.pages:
            if "claude.ai" in page.url:
                return page
    return None


def _wait_for_input(page: Page, timeout: int = 10_000) -> str | None:
    """Try each known input selector; return the first one that resolves."""
    for sel in CLAUDE_INPUT_SELECTORS:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout)
            return sel
        except Exception:
            continue
    return None


def _pick_best_page(pages: list[Page]) -> Page | None:
    """Among Claude.ai pages, pick one showing a real conversation (not a test)."""
    claude_pages = [p for p in pages if "claude.ai" in p.url]
    if not claude_pages:
        return None
    # Prefer a /chat/ page whose title doesn't look like a test
    for pg in claude_pages:
        url = pg.url
        if "/chat/" not in url:
            continue
        try:
            title = pg.title().lower()
        except Exception:
            title = ""
        if not any(kw in title for kw in _SKIP_TITLE_KEYWORDS):
            return pg
    return claude_pages[0]


def inject(
    message: str,
    *,
    conv_id: str | None = None,
    new_chat: bool = False,
    dry_run: bool = False,
    screenshot: bool = False,
) -> dict:
    """Main entry point. Returns a result dict for the caller.

    When neither conv_id nor new_chat is set, navigates to the first
    non-test Claude.ai page found in the browser.
    """
    result: dict = {"ok": False, "error": None, "url": None, "sent": False}

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            result["error"] = f"CDP connection failed: {e}"
            return result

        all_pages = [pg for ctx in browser.contexts for pg in ctx.pages]
        if not any("claude.ai" in pg.url for pg in all_pages):
            result["error"] = "No Claude.ai tab found in Chrome"
            return result

        # Pick or navigate to the target page
        if new_chat:
            target_url = "https://claude.ai/new"
        elif conv_id:
            target_url = f"https://claude.ai/chat/{conv_id}"
        else:
            target_url = None

        if target_url:
            page = _pick_best_page(all_pages) or all_pages[0]
            if page.url != target_url:
                page.goto(target_url, wait_until="domcontentloaded",
                          timeout=30_000)
                page.wait_for_load_state("networkidle", timeout=15_000)
        else:
            page = _pick_best_page(all_pages)
            if not page:
                result["error"] = "No suitable (non-test) Claude.ai page found"
                return result

        result["url"] = page.url

        # Wait for input box (with global timeout)
        sel = _wait_for_input(page, timeout=10_000)
        if not sel:
            result["error"] = "Input box not found (tried all known selectors)"
            return result

        input_el = page.locator(sel).first
        input_el.click()
        page.wait_for_timeout(200)

        # Type via clipboard paste (fast, handles CJK correctly)
        input_el.fill("")
        page.wait_for_timeout(100)
        input_el.click()
        page.evaluate(
            "text => navigator.clipboard.writeText(text)", message)
        modifier = "Meta" if sys.platform == "darwin" else "Control"
        page.keyboard.press(f"{modifier}+v")
        page.wait_for_timeout(500)

        if screenshot or dry_run:
            page.screenshot(path=str(SCRIPT_DIR / "inject_preview.png"))

        if dry_run:
            result["ok"] = True
            result["sent"] = False
            return result

        # Send and exit immediately — don't wait for Claude's response
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)

        result["ok"] = True
        result["sent"] = True

    return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Inject message into Claude.ai")
    parser.add_argument("message", help="Message text to inject")
    parser.add_argument("--conv-id", help="Conversation UUID to navigate to")
    parser.add_argument("--new", action="store_true", dest="new_chat",
                        help="Start a new conversation")
    parser.add_argument("--dry-run", action="store_true",
                        help="Type but don't send (screenshot saved)")
    parser.add_argument("--screenshot", action="store_true",
                        help="Save screenshot after action")
    args = parser.parse_args()

    r = inject(
        args.message,
        conv_id=args.conv_id,
        new_chat=args.new_chat,
        dry_run=args.dry_run,
        screenshot=args.screenshot,
    )

    if r["ok"]:
        status = "typed (dry-run)" if not r["sent"] else "sent"
        print(f"[OK] {status} — URL: {r['url']}")
        return 0
    else:
        print(f"[FAIL] {r['error']}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
