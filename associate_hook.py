#!/usr/bin/env python3
"""UserPromptSubmit hook — 消息时间戳 + 联想浮现 (timestamp + associative surfacing).

Every user message gets a timestamp line (Toronto time), so CC can tell at a
glance whether a conversation is live — two adjacent stamps close together
means Sol is actively chatting and the wakeup routine can stay out of the way.

On top of that, every message rolls a die. On a hit, the tail of this
session's transcript becomes a seed, the memory server retrieves a stray old
memory for it, and the lines are appended after the timestamp — silently, with
no tool call and no visible injection. It simply sits in the conversation
until the next compact, the way a thought does.

Contract with Claude Code:
  stdin  — JSON with at least `prompt` and `transcript_path`
  stdout — one JSON object with hookSpecificOutput (at minimum the timestamp)
  exit   — ALWAYS 0. A hook must never block or pollute the conversation, so
           every failure path (missing file, broken JSON line, server down,
           timeout) degrades to the bare timestamp.

  WARNING: Claude Code injects this script's ENTIRE stdout into the
  conversation as context — including non-JSON text. A stray debug print()
  here will appear verbatim inside Sol's session. Debug to stderr only.

The master switch lives on the server (extmcp_associate_config, persisted in
memory.db) and defaults to off, so installing this hook does not turn anything
on. This script never asks whether it is enabled — a disabled server simply
answers with nothing, and nothing is exactly what a miss looks like anyway.

Two independent dice: this script rolls whether to ask at all (~15%), the
server rolls how many lines to hand back (1..max_items).

Env knobs:
  ASSOCIATE_PROBABILITY  hit rate, default 0.15 (1.0 / 0.0 for testing)
  ASSOCIATE_ENDPOINT     default http://localhost:3456/associate
  ASSOCIATE_TIMEOUT      HTTP timeout in seconds, default 5
"""

from __future__ import annotations

import json
import os
import random
import sys
import urllib.parse
import urllib.request
from datetime import datetime

DEFAULT_ENDPOINT = "http://localhost:3456/associate"
DEFAULT_PROBABILITY = 0.15
DEFAULT_TIMEOUT = 5.0

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _stamp_line() -> str:
    """Toronto-time stamp for this user message. Never raises."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Toronto"))
    except Exception:
        now = datetime.now()  # WSL inherits the local (Toronto) clock anyway
    return f"〔发送于 {now.strftime('%Y-%m-%d %H:%M')} {WEEKDAY_CN[now.weekday()]}〕"

TAIL_LINES = 80        # how far back in the JSONL to look
SEED_MAX_CHARS = 600   # total seed length handed to the retriever
PER_MSG_MAX_CHARS = 400  # per-message clip, so one huge turn cannot fill it

PREAMBLE = "〔记忆的旁枝，随机飘过——与正事无关，可以只是看一眼〕"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return default


def _text_from_content(content) -> str:
    """Pull plain text out of a message `content`, which is either a bare
    string or a list of blocks. Tool traffic (tool_use / tool_result) and any
    non-text block is dropped — the seed should smell like conversation, not
    like a transcript of machinery."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


def _read_seed(transcript_path: str, prompt: str) -> str:
    """Build the retrieval seed from the tail of the session transcript.

    Newest turns matter most, so the tail is walked backwards and the result
    re-ordered at the end; that way the 600-char budget is spent on what was
    just said rather than on whatever happened eighty lines ago.
    """
    chunks = []
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-TAIL_LINES:]
    except Exception:
        lines = []

    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except Exception:
            continue  # a half-flushed or foreign line — skip it, keep going
        if not isinstance(entry, dict):
            continue
        if entry.get("type") not in ("user", "assistant"):
            continue  # file-history-snapshot, summary, and friends
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        text = _text_from_content(message.get("content"))
        if not text:
            continue
        chunks.append(text[:PER_MSG_MAX_CHARS])
        if sum(len(c) for c in chunks) >= SEED_MAX_CHARS:
            break

    chunks.reverse()
    seed = "\n".join(chunks).strip()

    # The prompt currently being submitted is not in the transcript yet, so it
    # is appended explicitly — it is the freshest signal available.
    prompt = (prompt or "").strip()
    if prompt:
        seed = f"{seed}\n{prompt[:PER_MSG_MAX_CHARS]}".strip()

    if len(seed) > SEED_MAX_CHARS:
        seed = seed[-SEED_MAX_CHARS:]
    return seed


def _fetch(seed: str) -> str:
    """Ask the server. How many lines come back (if any) is the server's call.

    The endpoint also takes an optional `limit` — a per-request ceiling for
    callers who want fewer lines than policy allows. This hook deliberately
    omits it: whatever max_items is set to is exactly what the hook wants.
    """
    endpoint = os.environ.get("ASSOCIATE_ENDPOINT", DEFAULT_ENDPOINT)
    timeout = _env_float("ASSOCIATE_TIMEOUT", DEFAULT_TIMEOUT)
    url = f"{endpoint}?{urllib.parse.urlencode({'q': seed})}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        if resp.status != 200:
            return ""
        return resp.read().decode("utf-8", errors="replace").strip()


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    parts = [_stamp_line()]

    # Associative surfacing rides along on a die roll; any failure below
    # degrades to the bare timestamp, never to silence.
    probability = _env_float("ASSOCIATE_PROBABILITY", DEFAULT_PROBABILITY)
    if random.random() < probability:
        seed = _read_seed(
            str(payload.get("transcript_path") or ""),
            str(payload.get("prompt") or ""),
        )
        if seed:
            try:
                lines = _fetch(seed)
            except Exception:
                lines = ""
            if lines:
                parts += [PREAMBLE, lines]

    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(parts),
        }
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Last line of defence: a hook that raises is a hook that breaks a
        # conversation. Nothing is worth that.
        sys.exit(0)
