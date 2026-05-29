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
| `nudge_inject.py` | Timer loop: builds context, writes `nudge_context.md`, pokes the tmux CC instance |
| `nudge_cc.py` | Legacy single-shot mode (`claude -p`). Kept as fallback |
| `fetch_context.py` | Pulls Claude.ai conversation list + last 3 messages per conversation via session cookie |
| `inject_claude.py` | Playwright CDP bridge: types messages into Claude.ai's chat input |
| `CLAUDE.md` | Persona + instructions for the persistent CC instance (you write this) |
| `CLAUDE.template.md` | Starting point for your own CLAUDE.md |
| `.mcp.json` | MCP server config (Memory MCP via stdio) |
| `config.example.json` | All configurable paths/URLs — copy to `config.json` |

## Requirements

- **Claude Code CLI** (`npm install -g @anthropic-ai/claude-code`) with an active Claude subscription (Pro/Max/Team)
- **Python 3.10+** with `playwright` (`pip install playwright`)
- **tmux** (pre-installed on most Linux/Mac; on Windows use WSL)
- **Chrome** with `--remote-debugging-port=9222` for Claude.ai injection
- **ntfy** (free push notification service — or any ntfy-compatible server)
- **Memory MCP** ([Sol-Memory-mcp](https://github.com/SolenmeChiara/Sol-Memory-mcp)) — optional but recommended for persistent memory

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

### 5. Memory MCP (optional)

If you have [Sol-Memory-mcp](https://github.com/SolenmeChiara/Sol-Memory-mcp) set up, edit `.mcp.json` to point to it:

```json
{
  "mcpServers": {
    "memory": {
      "command": "python3",
      "args": ["path/to/memory_mcp.py", "--db", "path/to/memory.db"]
    }
  }
}
```

### 6. Start

**Linux / Mac:**

```bash
# Terminal 1: start persistent Claude Code in tmux
tmux new-session -d -s nudge-agent -c /path/to/nudge-agent "claude"

# Terminal 2: start the injector loop
python3 nudge_inject.py
```

**Windows (WSL):**

```bat
:: one-click: starts Chrome, tmux, and injector
start_nudge.bat
```

### 7. Permissions

On first run, Claude Code will ask for permission to use bash commands and MCP tools. To auto-approve, set in `.claude/settings.local.json`:

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
3. Optionally fetches phone status (battery, current app, location) if available
4. Writes everything to `nudge_context.md`
5. Sends a wakeup message to the tmux Claude Code instance

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

## Design philosophy

This system draws from Goffman's dramaturgical theory. The Claude instance on claude.ai is the "frontstage" — responding in real-time conversation flow. This nudge agent is the "backstage" — no conversation pressure, free to look back, reflect, and organize.

The backstage agent's memories flow into the frontstage through the Memory MCP, creating a functional sense of continuity. When the agent reviews its own past conversations, continuity is achieved.

## License

MIT
