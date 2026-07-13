# Nudge Agent

A persistent Claude Code instance that runs in the background, monitors your Claude.ai conversations, and sends gentle reminders to your phone when you drift away from tasks.

Built for ADHD time-blindness. The agent wakes up every 20-60 minutes, reads your conversation history and memory state, decides whether you need a nudge, and sends it — all without you having to ask.

## Architecture

```
                    ┌─────────────────────────────────────┐
                    │          WSL tmux session            │
                    │  ┌───────────────────────────────┐  │
                    │  │   Claude Code (persistent)     │  │
                    │  │   - reads CLAUDE.md persona    │  │
                    │  │   - has Memory MCP tools       │  │
                    │  │   - full bash/tool access       │  │
                    │  └──────────┬────────────────────┘  │
                    └─────────────┼────────────────────────┘
                                  │ wakes up via
                    ┌─────────────┴────────────────────────┐
                    │      nudge_inject.py (Windows)        │
                    │  - builds context every 20-60 min     │
                    │  - writes nudge_context.md             │
                    │  - sends wakeup via tmux send-keys     │
                    └──┬──────────┬───────────────────┬────┘
                       │          │                   │
              ┌────────▼──┐  ┌───▼────────┐  ┌──────▼───────┐
              │ Claude.ai  │  │ Memory MCP │  │ Phone status │
              │ API        │  │ (SQLite)   │  │ (iOS POST)   │
              │ fetch_     │  │ breath,    │  │ battery,     │
              │ context.py │  │ dream,     │  │ app, location│
              └────────────┘  │ search     │  └──────────────┘
                              └────────────┘

    Claude Code decides autonomously:
    ├── Send ntfy push to phone (curl)
    ├── Inject message into Claude.ai conversation (Playwright CDP)
    ├── Organize/write memories (Memory MCP)
    └── Do nothing (if the user is active or it's not the right time)
```

## Components

| File | Role |
|------|------|
| `nudge_inject.py` | Timer loop: builds context, writes `nudge_context.md`, pokes the tmux CC instance, delivers urgent messages between wakeups |
| `fetch_context.py` | Pulls Claude.ai conversation list + last 3 messages per conversation via session cookie |
| `inject_claude.py` | Playwright CDP bridge: types messages into Claude.ai's chat input |
| `pc_status.py` | PC presence probe: keyboard/mouse idle, foreground window, Chrome tab titles (CDP metadata only) |
| `x_notif.py` | Watches the X tab title for unread-count growth, renders a notification block |
| `see_screen.py` | Agent-initiated iPhone screenshot: trigger mail → phone automation screenshots and uploads → agent reads the image |
| `start_nudge.bat` | Windows one-click startup: Chrome debug, Health ingester, tmux CC (auto-permissions), injector |
| `nudge_cc.py` | Legacy single-shot mode (`claude -p`). Deprecated, kept for reference |
| `CLAUDE.md` | Persona + instructions for the persistent CC instance (you write this) |
| `CLAUDE.template.md` | Starting point for your own CLAUDE.md |
| `.mcp.json` | MCP server config (Memory MCP over HTTP; see setup note on WSL) |
| `config.example.json` | All configurable paths/URLs/credentials — copy to `config.json` (gitignored) |

## Requirements

- **Memory MCP** ([Sol-Memory-mcp](https://github.com/SolenmeChiara/Sol-Memory-mcp)) — **required**. See "The memory bond" below: the agent is designed as a companion process to Memory MCP and degrades badly without it (no memory context, no inbox, no phone senses, no screen peek — just bare timed wakeups)
- **Claude Code CLI** (`npm install -g @anthropic-ai/claude-code`) with an active Claude subscription (Pro/Max/Team)
- **Python 3.10+** — the core loop (`nudge_inject.py`, `fetch_context.py`, `pc_status.py`, `x_notif.py`, `see_screen.py`) is stdlib-only; `playwright` (`pip install playwright`) is needed only by `inject_claude.py` for typing into Claude.ai
- **tmux** (pre-installed on most Linux/Mac; on Windows use WSL)
- **Chrome** with `--remote-debugging-port=9222` for Claude.ai injection and tab probing
- **ntfy** (free push notification service — or any ntfy-compatible server)

### The memory bond

The dependency between the two projects is deliberately one-directional:

- **Memory MCP standalone is a perfectly fine diary.** Every feature keeps working without the agent. The agent-facing extension tables (phone events, screen peeks, backend inbox) keep accepting and storing whatever the phone posts — data just accumulates with nobody consuming it, and rolling cleanup is self-contained on the memory side, so nothing leaks or grows unbounded.
- **The agent without Memory MCP is a cripple, by design.** It won't crash — every memory touchpoint degrades gracefully and the wakeup context tells the agent exactly what's broken — but most of what makes it useful (memory continuity, the inbox, urgent express lane, phone senses) lives in that bond.
- Planned on the memory side: agent-facing tools (`send_to_backend`, backend session recall) get hidden from MCP clients when no agent is detected, so a standalone memory install never exposes dead switches.

## Setup

### 1. Clone and configure

```bash
git clone https://github.com/SolenmeChiara/nudge-agent.git
cd nudge-agent
cp config.example.json config.json
cp CLAUDE.template.md CLAUDE.md
```

Edit `config.json` with your paths. Edit `CLAUDE.md` with your persona — this is the most important file; it defines who the agent is and how it behaves.

### 2. Environment

Set your Claude.ai session cookie (needed for conversation monitoring):

```bash
# in .env file or environment
CLAUDE_SESSION_KEY=sk-ant-sid02-xxxxx
```

To get this: open Claude.ai in Chrome → DevTools → Application → Cookies → copy `sessionKey`.

### 3. ntfy

Create a free topic at [ntfy.sh](https://ntfy.sh) (or self-host). Install the ntfy app on your phone and subscribe to your topic.

### 4. Chrome with debug port

The agent needs Chrome running with CDP enabled to inject messages into Claude.ai conversations:

```bash
# Windows
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="./chrome-data" https://claude.ai/

# Mac
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir="./chrome-data" https://claude.ai/

# Linux
google-chrome --remote-debugging-port=9222 --user-data-dir="./chrome-data" https://claude.ai/
```

Sign into Claude.ai once in this Chrome instance. Cookies persist across restarts.

### 5. Memory MCP (required)

Set up [Sol-Memory-mcp](https://github.com/SolenmeChiara/Sol-Memory-mcp) and run it as a persistent HTTP service, then point `.mcp.json` at it:

```json
{
  "mcpServers": {
    "memory": {
      "type": "http",
      "url": "http://localhost:3456/mcp"
    }
  }
}
```

The HTTP service is also what your phone posts to (`/phone-status`, `/phone-event`, `/peek`) — one process serves both the agent and the phone.

> **Why HTTP and not stdio?** On a pure Linux/Mac setup, stdio works fine. But if the agent lives in WSL while the memory DB lives on the Windows side (or vice versa), do NOT let both sides open the SQLite file directly: WAL shared memory does not survive the WSL/Windows filesystem boundary — whichever process opens the DB first locks the other side out with `disk I/O error`. Run the memory server as an HTTP service on the side that owns the DB file, and let everything else talk to the port.

### 6. Start

**Linux / Mac:**

```bash
# Terminal 1: start persistent Claude Code in tmux
tmux new-session -d -s nudge-agent -c /path/to/nudge-agent "claude --permission-mode bypassPermissions"

# Terminal 2: start the injector loop
python3 nudge_inject.py
```

**Windows (WSL):**

```bat
:: one-click: starts Chrome debug, Health ingester, tmux CC, and injector
start_nudge.bat
```

### 7. Permissions

The startup commands above launch Claude Code with `--permission-mode bypassPermissions`. This matters for an unattended agent: a permission prompt that nobody is around to answer blocks the whole session — the agent sits frozen mid-turn and every wakeup after it lands on "busy" until a human notices (we once lost a full night of shifts to a frozen turn; this flag removes the most preventable cause).

If you'd rather grant permissions selectively, drop the flag and use `.claude/settings.local.json` instead — but expect to babysit the first few sessions until the allowlist covers everything the agent does:

```json
{
  "permissions": {
    "allow": ["Bash(*)", "mcp__*"]
  }
}
```

## How it works

Every 20-60 minutes (3h at night), the injector:

1. Fetches your recent Claude.ai conversations (titles + last few messages)
2. Reads recent memories and prior nudge history from the database
3. Reads the backend inbox (messages the frontstage or scheduled jobs left for the agent) and marks them seen
4. Fetches phone status (battery, focus mode, current app, now playing…) and the 48h phone-event timeline
5. Probes PC presence (keyboard/mouse idle time, foreground window, Chrome tab titles via CDP) and X notification growth
6. Appends the tail of the agent's own journal (yesterday's self leaving notes for today's)
7. Writes everything to `nudge_context.md`
8. Sends a wakeup message to the tmux Claude Code instance

Between wakeups, the sleep loop polls every 30 seconds for **urgent** inbox rows and injects them straight into the Claude Code chat as they arrive — see "Urgent express lane" below.

Claude Code then reads the context and **autonomously decides** what to do:
- Send a push notification to your phone via ntfy
- Inject a status message into an active Claude.ai conversation
- Organize memories (dream, consolidate, update importance)
- Do nothing if you're already active or it's not the right time

The key insight: the agent isn't a dumb timer. It reads your actual conversation state and makes contextual judgments — like noticing you stopped halfway through a study session and the medication window is closing.

## Phone status (iOS Shortcuts)

You can optionally POST phone state from iOS Shortcuts to the Memory MCP:

```
POST http://your-ip:3456/phone-status
Content-Type: application/json

{
  "battery_level": 65,
  "battery_charging": true,
  "current_app": "Reddit",
  "screen_time_minutes": 180,
  "location": "Home",
  "weather": "Sunny",
  "temperature": 22,
  "steps": 3400
}
```

This gives the agent context like "Sol's phone is at 20% and they've been on TikTok for 2 hours" — useful for calibrating nudge tone.

The endpoint is deliberately forgiving: Chinese-locale Shortcuts keys are
mapped server-side, plain-text bodies are tolerated, obviously-impossible
values (a 316,152-step day, ask us how we know) are sanity-bounded to NULL,
and the raw wire payload is kept verbatim in `raw_json` for debugging your
shortcut.

## Phone events (iOS Shortcuts automations)

Status is a snapshot; events are points in time. iOS automations (alarm
stopped, sleep focus on/off, home Wi-Fi join/leave, charging) can each POST
a self-describing marker the moment they fire:

```
POST http://your-ip:3456/phone-event
Content-Type: application/json

{"event": "alarm_stopped", "detail": "optional free text"}
```

The trigger identity is hardcoded per automation — no need for a "trigger
reason" variable, and the phone-status shortcut stays untouched. The
injector renders the last 48h as a timeline ("07:41 alarm stopped → 09:15
home Wi-Fi joined"), which tells the agent things no snapshot can: when you
woke up, when you left the house, when you went to bed.

## Poke & screen share (iOS back-tap)

Two social gestures built on the same `/phone-event` endpoint, both carrying
a screen OCR snapshot:

- **Poke** — "tap on the shoulder". Settings → Accessibility → Touch →
  Back Tap → Triple Tap → pick a shortcut that does: *Take Screenshot →
  Extract Text from Image → POST* `{"event": "poke", "detail": <extracted text>}`.
  The agent sees a one-line timeline entry with a teaser of what was on
  your screen the moment you thought of it.
- **Screen share** — "look at this", deliberately heavier. Same shortcut
  shape with `"event": "screen_share"` (bind it to Double Tap or run it
  manually). The injector collapses these to a single count + latest teaser
  in the context so frequent sharing never floods the agent; the full OCR
  text stays in the DB and the agent pulls it only when it decides to look
  (`curl 'http://localhost:3456/phone-event?hours=48&limit=20'`).

The design principle for high-frequency senses: **data always lands,
attention stays optional.** Nobody owes anybody a read receipt.

## See screen (agent-initiated screenshot)

Everything above is the phone pushing to the agent. This is the reverse
channel — the agent decides to look:

```
see_screen.py ──trigger mail──▶ iPhone Mail ──automation──▶ screenshot
      ▲                                                        │
      └────── polls /peek/latest ◀──── POST /peek ◀────────────┘
```

One command (`python3 see_screen.py`) walks the whole chain and prints the
image path; add `--fresh 300` to accept an existing screenshot younger than
5 minutes and skip the mail. Exit 2 means timeout (phone locked / offline /
automation didn't fire) — the script never hands you a stale image as if it
were current.

Setup:

1. **Sending credentials** — an SMTP account the agent mails *from*. For
   Gmail: enable 2FA, create an App Password (myaccount.google.com/apppasswords
   — make sure you're generating it under the account you intend to send
   from), put it in `config.json` (`peek_smtp_user` / `peek_smtp_password`).
   Pick a distinctive `peek_mail_subject` — it's the trigger codeword.
2. **Phone automation** — Shortcuts → Automation → "When I get an email"
   filtered on that subject → *Take Screenshot → Get Contents of URL
   (POST, request body: File → the screenshot) to* `http://your-ip:3456/peek`
   → run immediately, notifications off.
3. **Mail app caveats** — only accounts added to the **system Mail app**
   fire the automation (the Gmail app doesn't count). Gmail accounts are
   fetched by polling (expect ~1 min extra latency); iCloud is push and
   near-instant, but a freshly created iCloud alias can take hours before
   it syncs to the phone — webmail showing the mail while the phone stays
   silent means exactly this, wait it out.

The server keeps a rolling 10 screenshots under `peeks/` (gitignore this —
it's literal screen content).

## Urgent express lane

`send_to_backend` on the memory side accepts `urgent=true`. Normal messages
wait in the inbox for the next wakeup; urgent ones are picked up by the
injector's 30-second sleep-loop poll and typed straight into the Claude Code
chat as a user message:

```
[紧急插播 · 来自 frontstage · 2026-07-13 08:36 UTC] <message>
```

Rows are only marked seen after a successful tmux injection — if CC is
mid-turn or tmux fails, the row stays pending and retries next poll, with
the regular inbox as the final fallback at the next full wakeup. Use it for
"frontstage needs the backend to act now", cron alerts, or the agent
leaving its future self a time-critical note.

## PC presence & X notifications

Zero-setup senses (Windows + the same Chrome debug port from step 4):

- `pc_status.py` reads keyboard/mouse idle time and the foreground window
  via Windows APIs, plus Chrome tab titles/domains over CDP — metadata
  only, it never reads page content. Idle-but-watching-a-video looks the
  same as away, so the agent is told to cross-check with the foreground
  window before concluding anyone left.
- `x_notif.py` watches the X tab's title for the unread badge. When ≥5 new
  notifications accumulate, the context grows an "X notifications" section;
  the agent visits, deals with it, and `touch x_notif_ack` resets the
  counter. If you read them yourself, the counter resets on its own.

## Design philosophy

This system draws from Goffman's dramaturgical theory. The Claude instance on claude.ai is the "frontstage" — responding in real-time conversation flow. This nudge agent is the "backstage" — no conversation pressure, free to look back, reflect, and organize.

The backstage agent's memories flow into the frontstage through the Memory MCP, creating a functional sense of continuity. When the agent reviews its own past conversations, continuity is achieved.

## License

MIT
