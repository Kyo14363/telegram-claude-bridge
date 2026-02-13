# Changelog

## v2.4 (2026-02-13)

### New Features
- **`/fetch` command** - Deep fetch URL content → Claude analysis → save as AI-friendly Markdown to `fetch_outputs/`
- **`/extract` command** - On-demand structured data extraction using LangExtract (Gemini)
- **LangExtract integration** - Auto-enhance general URL content with structured extraction (topic, key data, entities, conclusion)
- **`fetch_outputs/` directory** - Timestamped Markdown files for each deep fetch result

### URL Processing (v2.2+)
- **fxtwitter API** - Rich X/Twitter tweet parsing (author, text, media, engagement, quotes)
- **yt-dlp (Python API)** - YouTube / social media metadata extraction (no subprocess)
- **HTTP fallback** - Page title + OG/meta description extraction
- **Cascade strategy** - X/Twitter: fxtwitter → yt-dlp → HTTP | YouTube: yt-dlp → HTTP | Others: HTTP

### Core
- **Conversation context memory** - Maintains rolling history (configurable rounds)
- **Daily log rotation** - Auto-rotate with configurable retention period
- **`.env` configuration** - All secrets and settings via environment variables
- **Multi-user support** - Comma-separated `ALLOWED_USER_ID`

### Dependencies
- `python-telegram-bot` >= 20.0 (required)
- `python-dotenv` >= 1.0.0 (required)
- `requests` >= 2.28.0 (recommended, for URL fetching)
- `yt-dlp` (optional, for YouTube/social media)
- `langextract` (optional, for /extract and LangExtract enhancement)
- `GOOGLE_API_KEY` env var (required for LangExtract features)

---

## v1.0 (Initial)

- Basic Telegram ↔ Claude Code CLI bridge
- Conversation history
- `/clear`, `/history`, `/help`, `/status`, `/exec` commands
