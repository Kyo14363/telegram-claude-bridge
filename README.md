# Telegram Claude Code Bridge v2.6

A lightweight bridge that lets you control [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) from your phone via Telegram, with conversation history, automatic URL content fetching, image analysis via Gemini Vision, and Twitter Article parsing.

**[繁體中文說明](#繁體中文)**

## Features

- **Conversation Context Memory** — Rolling history across messages, so Claude understands "this", "that one", "the items above"
- **Automatic URL Processing** — Share any link and the bot auto-fetches content for Claude
  - X/Twitter → fxtwitter API (fast) → yt-dlp (fallback)
  - YouTube → yt-dlp metadata extraction
  - Other URLs → HTTP title/description extraction
- **📷 Image Analysis (Gemini Vision)** — Tweet images auto-downloaded → base64 in memory → Gemini 2.0 Flash analysis
  - GIF thumbnails extracted for visual analysis
  - Twitter Articles (long-form Notes) fully parsed
- **`/fetch` Deep Analysis** — Fetch URL → Claude analysis → save as AI-friendly Markdown
- **`/extract` Structured Extraction** — Extract structured data using LangExtract + Gemini
- **Daily Log Rotation** — Auto-rotation with configurable retention
- **Modular Architecture** — 3-file design for maintainability
- **Windows Native** — Built and tested on Windows, works out of the box

## Architecture (v2.6)

```
telegram_bridge_claude.py   — Main: config, history, bridge, Telegram handlers
├── url_fetchers.py         — URL detection, platform fetchers, preprocessing
└── vision.py               — Platform-agnostic image understanding (Gemini Vision)
```

## ⚠️ Security Notice

This bridge uses `claude --print --dangerously-skip-permissions` to run Claude Code CLI. This means Claude can execute commands on your machine without confirmation prompts.

**Only run this on a machine you trust, and only allow your own Telegram User ID.**

## Quick Start

### Prerequisites

- Python 3.10+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`npm install -g @anthropic-ai/claude-code`)
- A Telegram Bot token (from [@BotFather](https://t.me/BotFather))

### Installation

```bash
git clone https://github.com/yourusername/telegram-MCP-bridge.git
cd telegram-MCP-bridge

# Install required dependencies
pip install -r requirements.txt

# Optional: Image analysis
pip install google-generativeai

# Optional: YouTube/social media
pip install yt-dlp

# Copy and configure environment
cp .env.example .env
# Edit .env with your TELEGRAM_BOT_TOKEN and ALLOWED_USER_ID
```

### Configuration

Edit `.env`:

```ini
# Required
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_USER_ID=your_user_id_here

# Optional: Image analysis (Gemini Vision)
GOOGLE_API_KEY=your_google_api_key_here
```

Get your Telegram User ID from [@userinfobot](https://t.me/userinfobot).
Get a free Google API key from [AI Studio](https://aistudio.google.com/apikey).

### Run

```bash
python telegram_bridge_claude.py
```

Or on Windows, double-click `start_bridge.bat`.

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Show welcome message and feature status |
| `/help` | Show all commands and system status |
| `/clear` | Clear conversation history |
| `/history` | Show conversation history summary |
| `/status` | Show system status (URL processors, image analysis, etc.) |
| `/exec <cmd>` | Execute a shell command directly |
| `/fetch` | Re-fetch last URL with deep Claude analysis |
| `/extract` | Extract structured data from last Claude response |

## URL Processing Flow

```
User sends URL
    ↓
detect_urls() — classify platform (x_twitter / youtube / general)
    ↓
Platform-specific fetcher:
  X/Twitter: fxtwitter API → yt-dlp → HTTP fallback
  YouTube:   yt-dlp → HTTP fallback
  Other:     HTTP fallback → LangExtract enhancement
    ↓
Image analysis (if tweet has photos/GIFs):
  download_image_to_base64() → describe_image_via_gemini()
    ↓
Article parsing (if tweet is long-form Note):
  article.content.blocks[] → structured markdown
    ↓
Enhanced content + Claude analysis → Telegram response
```

## Optional Dependencies

| Package | Purpose | Install |
|---------|---------|---------|
| `google-generativeai` | Image analysis (Gemini Vision) + LangExtract | `pip install google-generativeai` |
| `yt-dlp` | YouTube / social media metadata | `pip install yt-dlp` |
| `langextract` | Structured data extraction | `pip install langextract` |
| `requests` | URL fetching (included in requirements.txt) | `pip install requests` |

## File Structure

```
telegram-MCP-bridge/
├── telegram_bridge_claude.py  # Main bridge (config + history + handlers)
├── url_fetchers.py            # URL detection & platform fetchers
├── vision.py                  # Image analysis module (Gemini Vision)
├── start_bridge.bat           # Windows launcher
├── .env.example               # Configuration template
├── .env                       # Your configuration (git-ignored)
├── requirements.txt           # Python dependencies
├── logs/                      # Daily rotating logs (git-ignored)
├── fetch_outputs/             # Saved fetch results (git-ignored)
└── conversation_history.json  # Rolling history (git-ignored)
```

---

## 繁體中文

### Telegram Claude Code 橋接器 v2.6

透過 Telegram 從手機控制 Claude Code CLI 的輕量級橋接器。

#### 功能特色

- **對話記憶** — 自動保留最近 N 輪對話作為上下文
- **URL 自動處理** — 分享連結自動抓取內容（fxtwitter / yt-dlp / HTTP）
- **📷 圖片分析** — 推文圖片自動下載 → Gemini Vision 分析（支援 GIF 縮圖）
- **Twitter 長文解析** — 完整支援 Twitter Article / Notes 格式
- **模組化架構** — 三檔設計，易於維護

#### 快速開始

```bash
git clone https://github.com/yourusername/telegram-MCP-bridge.git
cd telegram-MCP-bridge
pip install -r requirements.txt
cp .env.example .env
# 編輯 .env 填入你的 TELEGRAM_BOT_TOKEN 和 ALLOWED_USER_ID
python telegram_bridge_claude.py
```

## License

MIT License — See [LICENSE](LICENSE) for details.
