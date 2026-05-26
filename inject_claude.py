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

CDP_URL = "http://localhost:9222"
SCRIPT_DIR = Path(__file__).resolve().parent

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


def inject(
    message: str,
    *,
    conv_id: str | None = None,
    new_chat: bool = False,
    dry_run: bool = False,
    screenshot: bool = False,
) -> dict:
    """Main entry point. Returns a result dict for the caller."""
    result: dict = {"ok": False, "error": None, "url": None, "sent": False}

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            result["error"] = f"CDP connection failed: {e}"
            return result

        page = _find_claude_page(browser.contexts)
        if not page:
            result["error"] = "No Claude.ai tab found in Chrome"
            return result

        # Navigate if needed
        if new_chat:
            target_url = "https://claude.ai/new"
        elif conv_id:
            target_url = f"https://claude.ai/chat/{conv_id}"
        else:
            target_url = None

        if target_url and page.url != target_url:
            page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

        result["url"] = page.url

        # Wait for input box
        sel = _wait_for_input(page)
        if not sel:
            result["error"] = "Input box not found (tried all known selectors)"
            if screenshot:
                page.screenshot(path=str(SCRIPT_DIR / "inject_fail.png"))
            return result

        input_el = page.locator(sel).first

        # Focus and clear
        input_el.click()
        page.wait_for_timeout(200)

        # Type the message character by character (handles CJK + contenteditable)
        input_el.fill("")
        page.wait_for_timeout(100)
        input_el.click()
        page.keyboard.type(message, delay=10)
        page.wait_for_timeout(300)

        if screenshot or dry_run:
            page.screenshot(path=str(SCRIPT_DIR / "inject_preview.png"))

        if dry_run:
            result["ok"] = True
            result["sent"] = False
            return result

        # Send: press Enter
        page.keyboard.press("Enter")
        page.wait_for_timeout(500)

        result["ok"] = True
        result["sent"] = True

        if screenshot:
            page.wait_for_timeout(2000)
            page.screenshot(path=str(SCRIPT_DIR / "inject_sent.png"))

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
