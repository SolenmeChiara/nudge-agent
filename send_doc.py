#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 markdown 文件渲染成手机可读的 HTML 邮件发给 Sol。

    python3 send_doc.py CLAUDE.md
    python3 send_doc.py mind/journal.md --subject "八月日记"
    python3 send_doc.py foo.md --no-attach      只发正文不带附件
    python3 send_doc.py foo.md --dry            只生成 HTML 存到 /tmp 看效果

正文是渲染好的 HTML（手机上直接读），附件是原始 md（要编辑时用）。
收件人默认取 config.json 里的 doc_mail_to，可用 --to 覆盖；主题默认取文件名。
"""
import argparse
import html
import os
import re
import smtplib
import sys
from email.message import EmailMessage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 收件人不写死在代码里：真实地址住在 config.json（gitignored），
# 与 peek_smtp_* 同一套约定。本仓库是公开的。

CSS = """
body{margin:0;padding:16px;background:#f7f6f3;color:#1f1f1f;
 font-family:-apple-system,"PingFang SC","Helvetica Neue",sans-serif;
 font-size:17px;line-height:1.75;-webkit-text-size-adjust:100%;}
.wrap{max-width:760px;margin:0 auto;background:#fff;padding:22px 20px 40px;
 border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,.08);}
h1,h2,h3,h4{line-height:1.35;margin:1.6em 0 .6em;font-weight:600;}
h1{font-size:1.55em;border-bottom:2px solid #e6e3dc;padding-bottom:.3em;}
h2{font-size:1.3em;border-bottom:1px solid #ece9e2;padding-bottom:.25em;}
h3{font-size:1.12em;} h4{font-size:1em;color:#555;}
p{margin:.7em 0;}
ul,ol{margin:.6em 0;padding-left:1.5em;}
li{margin:.3em 0;}
code{background:#f0eeea;padding:.12em .35em;border-radius:4px;
 font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.88em;
 word-break:break-all;}
pre{background:#f4f2ee;padding:12px 14px;border-radius:8px;overflow-x:auto;
 border-left:3px solid #d8d3c8;}
pre code{background:none;padding:0;font-size:.85em;word-break:normal;}
blockquote{margin:.8em 0;padding:.4em 0 .4em 14px;border-left:3px solid #cfc9bb;
 color:#5a5a5a;background:#faf9f6;}
hr{border:0;border-top:1px solid #e2ded5;margin:1.8em 0;}
a{color:#2f6f9f;}
.tw{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:1em 0;}
table{border-collapse:collapse;width:100%;font-size:.92em;}
th,td{border:1px solid #e0dcd3;padding:7px 10px;text-align:left;
 vertical-align:top;}
th{background:#f4f2ee;font-weight:600;}
strong{font-weight:600;}
.meta{color:#8a8479;font-size:.85em;margin-bottom:1.4em;}
"""

_TOKEN = "\x00CODE%d\x00"


def _inline(text):
    """行内标记。先把 `code` 抠出来占位，免得里头的星号被当粗体。"""
    stash = []

    def keep(m):
        stash.append(m.group(1))
        return _TOKEN % (len(stash) - 1)

    text = re.sub(r"`([^`]+)`", keep, text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
                  r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"~~(.+?)~~", r"<del>\1</del>", text)
    for i, raw in enumerate(stash):
        text = text.replace(_TOKEN % i,
                            "<code>%s</code>" % html.escape(raw, quote=False))
    return text


def _table(rows):
    """rows 是原始的 | ... | 行。第二行若是分隔行就丢掉。"""
    def cells(line):
        line = line.strip()
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|"):
            line = line[:-1]
        return [c.strip() for c in line.split("|")]

    body = rows[:]
    head = cells(body.pop(0))
    if body and re.match(r"^[\s|:-]+$", body[0]):
        body.pop(0)
    out = ['<div class="tw"><table><thead><tr>']
    out += ["<th>%s</th>" % _inline(c) for c in head]
    out.append("</tr></thead><tbody>")
    for line in body:
        out.append("<tr>")
        out += ["<td>%s</td>" % _inline(c) for c in cells(line)]
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def md_to_html(md):
    lines = md.split("\n")
    out, i = [], 0
    stack = []          # 开着的列表标签

    def close_lists(to_depth=0):
        while len(stack) > to_depth:
            out.append("</%s>" % stack.pop())

    while i < len(lines):
        line = lines[i]

        # 围栏代码块
        m = re.match(r"^\s*```(\w*)\s*$", line)
        if m:
            close_lists()
            buf, i = [], i + 1
            while i < len(lines) and not re.match(r"^\s*```\s*$", lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append("<pre><code>%s</code></pre>"
                       % html.escape("\n".join(buf), quote=False))
            continue

        # 表格
        if line.lstrip().startswith("|") and line.rstrip().endswith("|"):
            close_lists()
            buf = []
            while (i < len(lines) and lines[i].lstrip().startswith("|")
                   and lines[i].rstrip().endswith("|")):
                buf.append(lines[i])
                i += 1
            out.append(_table(buf))
            continue

        # 空行
        if not line.strip():
            close_lists()
            i += 1
            continue

        # 分隔线
        if re.match(r"^\s*([-*_])\s*(\1\s*){2,}$", line):
            close_lists()
            out.append("<hr>")
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            close_lists()
            lv = min(len(m.group(1)), 4)
            out.append("<h%d>%s</h%d>" % (lv, _inline(m.group(2).strip()), lv))
            i += 1
            continue

        # 引用
        if line.lstrip().startswith(">"):
            close_lists()
            buf = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>%s</blockquote>"
                       % "<br>".join(_inline(b) for b in buf if b.strip()))
            continue

        # 列表（按缩进算层级，两个空格一层）
        m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", line)
        if m:
            depth = len(m.group(1)) // 2 + 1
            tag = "ul" if m.group(2) in "-*+" else "ol"
            while len(stack) > depth:
                out.append("</%s>" % stack.pop())
            if len(stack) < depth:
                while len(stack) < depth:
                    out.append("<%s>" % tag)
                    stack.append(tag)
            elif stack and stack[-1] != tag:
                out.append("</%s>" % stack.pop())
                out.append("<%s>" % tag)
                stack.append(tag)
            out.append("<li>%s</li>" % _inline(m.group(3)))
            i += 1
            continue

        # 普通段落
        close_lists()
        buf = []
        while i < len(lines) and lines[i].strip():
            nxt = lines[i]
            if (re.match(r"^(#{1,6})\s", nxt) or nxt.lstrip().startswith(("|", ">"))
                    or re.match(r"^\s*([-*+]|\d+[.)])\s", nxt)
                    or re.match(r"^\s*```", nxt)):
                break
            buf.append(nxt.strip())
            i += 1
        if buf:
            out.append("<p>%s</p>" % _inline("<br>".join(buf))
                       .replace("&lt;br&gt;", "<br>"))
        else:
            i += 1

    close_lists()
    return "\n".join(out)


def build_page(title, body_html, meta):
    return ("<!doctype html><html><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,"
            "initial-scale=1\"><style>%s</style></head><body>"
            "<div class=\"wrap\"><div class=\"meta\">%s</div>%s</div>"
            "</body></html>" % (CSS, html.escape(meta), body_html))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--subject", default=None)
    ap.add_argument("--to", default=None)
    ap.add_argument("--no-attach", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    src = os.path.abspath(a.path)
    if not os.path.exists(src):
        sys.exit("找不到文件：%s" % src)
    raw = open(src, encoding="utf-8").read()
    name = os.path.basename(src)
    subject = a.subject or ("%s 全文" % name)

    import datetime
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    meta = "%s · %d 行 · %.1f KB · %s" % (
        name, raw.count("\n") + 1, len(raw.encode()) / 1024, stamp)
    page = build_page(subject, md_to_html(raw), meta)

    if a.dry:
        dest = "/tmp/%s.html" % name.replace(".", "_")
        open(dest, "w", encoding="utf-8").write(page)
        print("已生成 %s（%.1f KB）" % (dest, len(page.encode()) / 1024))
        return

    from config import CFG
    user, pw = CFG.peek_smtp_user, CFG.peek_smtp_password
    if not user or not pw:
        sys.exit("config.json 里 peek_smtp_user / peek_smtp_password 是空的")
    to = a.to or CFG.doc_mail_to
    if not to:
        sys.exit("没有收件人：config.json 里补 doc_mail_to，或者用 --to 指定")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content("这封信是 HTML 的，用支持 HTML 的客户端看。原文见附件。\n\n" + raw)
    msg.add_alternative(page, subtype="html")
    if not a.no_attach:
        msg.add_attachment(raw.encode("utf-8"), maintype="text",
                           subtype="markdown", filename=name)

    with smtplib.SMTP_SSL(CFG.peek_smtp_host, CFG.peek_smtp_port,
                          local_hostname="localhost") as s:
        s.login(user, pw)
        s.send_message(msg)
    print("已发送「%s」→ %s（正文 %.1f KB%s）"
          % (subject, to, len(page.encode()) / 1024,
             "" if a.no_attach else "，附件 " + name))


if __name__ == "__main__":
    main()
