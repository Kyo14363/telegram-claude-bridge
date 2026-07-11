# Telegram Claude Bridge

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows-lightgrey.svg)](#)
[![Built on Claude Agent SDK](https://img.shields.io/badge/built%20on-Claude%20Agent%20SDK-d97757.svg)](https://docs.claude.com/en/api/agent-sdk/overview)

An always-on Telegram bridge that turns Claude Code into a phone-accessible
assistant, built on the official
[`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/).

**Why this exists:** most Agent SDK examples are short-lived CLI demos. If you
want a *resident* agent — one process that holds a persistent SDK session for
weeks, survives crashes and machine sleep, and is safe to expose to a phone —
there is very little working reference code, especially on Windows, where
asyncio event-loop policy and console encoding break things in non-obvious
ways. This repo is that reference: a small production deployment with the
scars documented in its [CHANGELOG](CHANGELOG.md).

## What It Does

- Persistent Claude Code session through `claude-agent-sdk` (one session
  across messages, session id persisted and resumed across restarts)
- Telegram text and photo handling, with FIFO queueing while a task runs
- URL preprocessing for X/Twitter, YouTube, GitHub, PDFs (PyMuPDF), and
  general articles (`trafilatura`, with HTTP fallback)
- `/resume` — wake your *desktop* Claude Code session headlessly from the
  phone (`claude -p --resume`, same transcript, original project folder)
- `/handoff` — brief the resident SDK session on your desktop session's tail
  and hand it an instruction (streaming, read-only, safe while desktop is open)
- Provenance protocol + editorial triage: URL analyses must open with the
  model's own judgment and end with per-claim `## Claims` / `## Sources`
  blocks, so filed clippings stay traceable instead of becoming opaque summaries
- Optional Gemini Vision analysis for images
- X/Twitter reply capture (twikit primary, Playwright fallback)
- Optional Obsidian-style markdown capture with screenshot attachments
- Idle Playwright browser auto-close, plus direct psutil cleanup scoped to
  the SDK process tree

## Built for unattended operation

The bridge's whole value is being the remote entry point — so its own death
is the one failure it cannot report. The deployment story is designed around
that:

- **Liveness heartbeat** — the main asyncio loop touches
  `logs/heartbeat.txt` every 5 minutes; a silent log no longer means "maybe
  dead".
- **Watchdog** (`tmb_watchdog.ps1` + silent `.vbs` wrapper, Task Scheduler
  every 5 min) — restarts the bridge if the process dies, and kills + restarts
  the tree if the process is alive but the heartbeat is stale (wedged event
  loop).
- **Model pinning** — the SDK model is passed explicitly as a concrete id;
  unattended automation must never inherit global settings drift or bare
  aliases.
- **Token redaction at the formatter level** — the Telegram bot token never
  reaches the log file, including via propagated child-logger records, lazy
  %-args objects, and tracebacks (see CHANGELOG v3.2 for why filter-level
  redaction silently fails).
- **Full SDK teardown on `/clear`** — old loop/thread/subprocess tree are
  torn down completely before a fresh session spawns, avoiding a busy-looped
  zombie SDK thread.

## Layout

| File | Role |
|---|---|
| `telegram_bridge_claude.py` | Entry point — wires python-telegram-bot up |
| `config.py` | env-driven `CONFIG` dict, log redaction, system_prompt fragments |
| `bridge_core.py` | `ClaudeBridge`: SDK lifecycle, session persistence, queue, command dispatch |
| `handlers.py` | Telegram message / photo / command / error handlers, `/resume`, `/handoff` |
| `shortcuts.py` | Local diagnostic commands (`/ps`, `/cclog`, `/bridge`, …) |
| `metrics.py` | Lightweight stats backed by `stats.json`, summarized via `/stats` |
| `url_fetchers.py` | X/Twitter, YouTube, GitHub, PDF, general-article fetchers |
| `vision.py` | Optional Gemini Vision image description |
| `reply_fetcher.py` | Optional twikit-based X/Twitter reply capture + AI filtering |
| `tmb_watchdog.ps1` / `.vbs` | Task Scheduler watchdog (existence + liveness) |

## Setup

1. Install Python 3.11+ and [Claude Code](https://claude.com/claude-code).
2. Install dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and fill in:

   ```env
   TELEGRAM_BOT_TOKEN=your_bot_token_here
   ALLOWED_USER_IDS=123456789
   ```

4. Optional features (see `.env.example` for the full list):

   ```env
   GOOGLE_API_KEY=your_google_api_key_here
   OBSIDIAN_MOBILE_DIR=.\obsidian_clippings
   WORKING_DIR=%USERPROFILE%\claude-workspace
   TWIKIT_COOKIES=C:\path\to\cookies.json
   SDK_MODEL=claude-sonnet-5
   ```

5. Start:

   ```powershell
   python telegram_bridge_claude.py
   ```

   or double-click `start_bridge.bat`.

6. Optional — register the watchdog for self-healing:

   ```powershell
   schtasks /Create /TN TMB_Watchdog /SC MINUTE /MO 5 /TR "wscript.exe \"C:\path\to\tmb_watchdog.vbs\""
   ```

## Commands

See [COMMANDS.md](COMMANDS.md) for the full table. Highlights: `/status`,
`/clear`, `/interrupt`, `/stats`, `/resume`, `/handoff <instruction>`,
`/cclog [keyword] [n]`, `/exec <cmd>`.

## URL Flow

- X/Twitter: fxtwitter API, then yt-dlp, then HTTP fallback
- YouTube and supported media pages: yt-dlp
- GitHub repos: Raw/API README and repository metadata
- PDFs (including `arxiv.org/pdf/...`): PyMuPDF text extraction
- General web/news/Perplexity-style links: trafilatura, then HTTP fallback

If extracted content is too thin, the bridge adds a prompt hint so Claude can
decide whether to use Playwright for dynamic pages.

## Windows notes (learned the hard way)

- Importing **twikit** silently switches the global asyncio event-loop policy
  to `SelectorEventLoop`, which breaks `asyncio.create_subprocess_exec` with
  an *empty* `NotImplementedError`. Any async subprocess work in handler
  context goes through `run_in_executor(subprocess.run)` instead.
- PowerShell 5.1's `ConvertFrom-Json` is unusably slow on the huge
  single-line records in Claude Code transcripts; parse them in Python.
- `Get-Process` has no `CommandLine` property until PowerShell 7 — use
  `Get-CimInstance Win32_Process` for process matching.

## Private Data

Do not commit `.env`, logs, fetch outputs, `session_state.json`, `stats.json`,
or `conversation_history.json`. The repository `.gitignore` excludes these by
default, and the log formatter redacts the bot token as a second line of
defense.
