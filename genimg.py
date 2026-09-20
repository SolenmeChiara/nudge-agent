#!/usr/bin/env python3
"""私下生图玩具（Sol 2026-09-20 授权：不公开、不替笔、存盘标 AIGEN）。
用法：python3 genimg.py "提示词" [--name 文件名] [--model gpt-image-2] [--size 1024x1024|1536x1024|1024x1536] [--quality low|medium|high]
key 读 .secrets/openai_key；输出到 mind/Claude_photos/aigen/<时间>_<name>_AIGEN.png 并打印路径。"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("--name", default="img")
    ap.add_argument("--model", default="gpt-image-2")
    ap.add_argument("--size", default="1024x1024")
    ap.add_argument("--quality", default="medium")
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--edit", help="要改的 AI 图（文件名须含 AIGEN）")
    a = ap.parse_args()
    with open(os.path.join(ROOT, ".secrets", "openai_key"), encoding="utf-8") as f:
        key = f.read().strip()
    if a.edit:
        if "AIGEN" not in os.path.basename(a.edit):
            print("拒绝：只改 AI 生成图（文件名须含 AIGEN），不碰真实照片或 Sol 的画", file=sys.stderr); sys.exit(2)
        bnd = "----genimg" + str(int(time.time()))
        parts = []
        for k, v in {"model": a.model, "prompt": a.prompt, "size": a.size, "quality": a.quality, "n": str(a.n)}.items():
            parts.append(f"--{bnd}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        with open(a.edit, "rb") as f:
            img = f.read()
        parts.append(f"--{bnd}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"in.png\"\r\nContent-Type: image/png\r\n\r\n".encode() + img + b"\r\n")
        parts.append(f"--{bnd}--\r\n".encode())
        req = urllib.request.Request("https://api.openai.com/v1/images/edits",
            data=b"".join(parts), method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": f"multipart/form-data; boundary={bnd}"})
    else:
        body = {"model": a.model, "prompt": a.prompt, "size": a.size, "quality": a.quality, "n": a.n}
        req = urllib.request.Request("https://api.openai.com/v1/images/generations",
            data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read().decode()[:800], file=sys.stderr); sys.exit(1)
    outdir = os.path.join(ROOT, "mind", "Claude_photos", "aigen"); os.makedirs(outdir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M")
    for i, item in enumerate(d.get("data", [])):
        p = os.path.join(outdir, f"{ts}_{a.name}{'_'+str(i) if a.n>1 else ''}_AIGEN.png")
        with open(p, "wb") as f:
            f.write(base64.b64decode(item["b64_json"]))
        print(p)
    u = d.get("usage", {})
    print(f"[{a.model} {a.size} {a.quality}] {time.time()-t0:.1f}s tokens={u.get('total_tokens')}", file=sys.stderr)
if __name__ == "__main__":
    main()
