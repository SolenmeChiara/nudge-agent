"""See Sol's iPhone screen: trigger mail -> phone screenshots -> path out.

The whole chain hides behind one command (the point: the agent just
"calls a function and gets a picture"):

  1. If the newest screenshot is already fresh (< --fresh seconds),
     print its path and exit — no mail sent.
  2. Otherwise send the PEEK trigger mail. Sol's iPhone "on receiving
     email" automation takes a screenshot and POSTs it to /peek.
  3. Poll /peek/latest until a screenshot newer than the trigger
     arrives, then print its WSL path (Read it to actually look).

Usage (backstage agent, WSL):
    python3 see_screen.py              # default: fresh window 60s, wait 75s
    python3 see_screen.py --fresh 300  # accept a 5-min-old shot without mail

Exit codes: 0 = path printed; 1 = config missing; 2 = timed out
(mail sent but no screenshot came back — Sol's phone may be off,
offline, or the automation didn't fire. Do NOT read an old file and
pretend it's current; say so or retry later instead.)

peek_mail_to must be an iCloud address: Apple Mail gets real push
for iCloud only. Gmail is lazy-fetched, so a Gmail trigger fires
only while Mail happens to be foreground (field-tested 2026-07-14,
4 shots). DND does not block the automation. The trigger matches on
the subject alone, so the body is free text — write something human.
"""

from __future__ import annotations

import argparse
import json
import smtplib
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.mime.text import MIMEText

from config import CFG

POLL_INTERVAL = 2.0
DEFAULT_FRESH_S = 60
DEFAULT_WAIT_S = 75


def _latest() -> dict | None:
    try:
        with urllib.request.urlopen(
            f"{CFG.peek_base_url}/peek/latest", timeout=5
        ) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, ConnectionError, OSError, json.JSONDecodeError):
        return None


def _send_trigger() -> None:
    msg = MIMEText(f"peek {datetime.now().isoformat()}")
    msg["Subject"] = CFG.peek_mail_subject
    msg["From"] = CFG.peek_smtp_user
    msg["To"] = CFG.peek_mail_to
    # local_hostname must be ASCII: the Windows machine name contains
    # Chinese characters, which crashes smtplib's EHLO otherwise.
    with smtplib.SMTP_SSL(CFG.peek_smtp_host, CFG.peek_smtp_port,
                          local_hostname="localhost", timeout=20) as s:
        s.login(CFG.peek_smtp_user, CFG.peek_smtp_password)
        s.send_message(msg)


def main() -> int:
    ap = argparse.ArgumentParser(description="Request an iPhone screenshot")
    ap.add_argument("--fresh", type=int, default=DEFAULT_FRESH_S,
                    help="accept an existing shot younger than N seconds")
    ap.add_argument("--wait", type=int, default=DEFAULT_WAIT_S,
                    help="seconds to wait for the phone after triggering")
    args = ap.parse_args()

    cur = _latest()
    if cur and cur.get("age_seconds", 1e9) < args.fresh:
        print(cur["wsl_path"])
        print(f"(现成的，{cur['age_seconds']:.0f} 秒前)", file=sys.stderr)
        return 0

    if not (CFG.peek_smtp_user and CFG.peek_smtp_password and CFG.peek_mail_to):
        print("peek 未配置：config.json 需要 peek_smtp_user / "
              "peek_smtp_password / peek_mail_to", file=sys.stderr)
        return 1

    baseline_ts = cur["ts_ms"] if cur else 0
    try:
        _send_trigger()
    except Exception as e:
        print(f"暗号邮件发送失败: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print("暗号已发，等手机截屏……", file=sys.stderr)

    t_end = time.time() + args.wait
    while time.time() < t_end:
        time.sleep(POLL_INTERVAL)
        cur = _latest()
        if cur and cur.get("ts_ms", 0) > baseline_ts:
            print(cur["wsl_path"])
            print(f"(新鲜出炉，用 Read 工具看它)", file=sys.stderr)
            return 0

    print("超时：邮件发出去了但截图没回来。Sol 的手机可能锁着/没网/"
          "自动化没触发。别拿旧图充数——过会儿再试或直接问 Sol。",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
