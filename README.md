# Telegram Claude Bridge

A personal Telegram bridge for running Claude Code from your phone. It keeps a persistent Claude Agent SDK session, preprocesses URLs, returns Playwright screenshots to Telegram, and can save URL captures to an Obsidian-style markdown folder.

## What It Does

- Persistent Claude Code session through `claude-agent-sdk`
- Telegram text and photo handling
- URL preprocessing for X/Twitter, YouTube, GitHub, and general articles
- General article extraction with `trafilatura`, with HTTP fallback
- Optional Gemini Vision analysis for images
- X/Twitter reply capture through Playwright-first prompts
- Optional Obsidian markdown capture with screenshot attachments
- Shortcut commands for local status/log inspection

## Setup

1. Install Python 3.11+ and Claude Code.
2. Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

3. Copy `.env.example` to `.env` and fill in:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_USER_IDS=123456789
```

4. Optional features:

```env
GOOGLE_API_KEY=your_google_api_key_here
OBSIDIAN_MOBILE_DIR=.\obsidian_clippings
WORKING_DIR=%USERPROFILE%\claude-workspace
```

5. Start:

```powershell
python telegram_bridge_claude.py
```

or double-click `start_bridge.bat`.

## Commands

| Command | Purpose |
|---|---|
| `/start` | Show startup status and keyboard |
| `/help` | Show command help |
| `/status` | Show bridge and SDK status |
| `/session` | Show current Claude SDK session |
| `/clear` | Start a fresh Claude session |
| `/interrupt` | Interrupt the current Claude task |
| `/browser-close` | Close Playwright browsers |
| `/keyboard` | Re-send shortcut keyboard |
| `/history` | Show local reference history |
| `/stats` | Show usage metrics |
| `/ps` `/cclog` `/tasklog` `/bridge` `/uptime` | Local diagnostic shortcuts |

## URL Flow

- X/Twitter: fxtwitter API, then yt-dlp, then HTTP fallback
- YouTube and supported media pages: yt-dlp
- GitHub repos: Raw/API README and repository metadata
- General web/news/Perplexity-style links: trafilatura, then HTTP fallback

If extracted content is too thin, the bridge adds a prompt hint so Claude can decide whether to use Playwright for dynamic pages.

## Private Data

Do not commit `.env`, logs, fetch outputs, `session_state.json`, `stats.json`, or conversation history. The repository `.gitignore` excludes these by default.
