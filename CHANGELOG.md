# Changelog

## v2.6 (2026-02-21)

### Architecture — Modular Refactor
Single-file monolith → 3-file modular architecture:
- `vision.py` — Platform-agnostic image understanding module
- `url_fetchers.py` — URL detection, platform fetchers, preprocessing orchestrator
- `telegram_bridge_claude.py` — Main file: config, history, bridge, Telegram handlers

### New Features
- **Twitter Article parsing** — Full support for Twitter long-form Notes/Articles (`tweet.article.content.blocks[]`)
- **GIF thumbnail extraction** — Twitter GIFs (classified as `videos` with `type="gif"`) now have their thumbnails extracted for Gemini Vision analysis
- **Image analysis (Gemini Vision)** — Auto-download tweet images → base64 encode in memory → Gemini 2.0 Flash analysis → text descriptions merged into content
- **`/clear` command fix** — Previously blocked by `~filters.COMMAND` filter, now properly routed through message handler
- **Command logging** — All special commands (`/clear`, `/help`, etc.) now logged for traceability

### Bug Fixes
- Fixed `/clear`, `/help`, `/history`, `/status`, `/extract`, `/fetch` being silently dropped by Telegram's `~filters.COMMAND` filter
- Fixed Twitter Articles (Notes) returning empty content (only metadata)
- Fixed Twitter GIFs not entering image analysis pipeline

### Design Decisions
- CONFIG sharing via parameter passing (`config: dict`) to reduce module coupling
- Each sub-module uses `logging.getLogger(__name__)` for its own logger
- `vision.py` has zero knowledge of any platform — receives only image URL list + context string
- Special commands route through `message_handler` → `bridge.handle_message()` internal dispatch

---

## v2.5 (2026-02-17)

### New — Image Analysis
Two-layer architecture for tweet image understanding:
- **Layer 1 (Platform parsing)**: `fetch_via_fxtwitter()` return type changed from `str` to `Tuple[str, List[str]]`, extracts image URLs from `media.photos[]`
- **Layer 2 (Generic image understanding)**: Three platform-agnostic functions:
  - `download_image_to_base64()` — Download image to memory, base64 encode (no disk I/O)
  - `describe_image_via_gemini()` — Gemini 2.0 Flash Vision API for single image
  - `analyze_images()` — Orchestrator: iterate URL list → download → Gemini → combine text descriptions

### New Config
- `IMAGE_ANALYSIS_ENABLED`, `MAX_IMAGES_PER_MESSAGE`, `IMAGE_ANALYSIS_TIMEOUT`
- `GOOGLE_API_KEY` env var (also used by LangExtract)

### New Dependency
- `google-generativeai` (Gemini Vision API)

---

## v2.4 (2026-02-12)

### New Features
- **`/fetch` command** — Deep fetch URL content → Claude analysis → save as AI-friendly Markdown
- **Auto-save** — Messages with URLs auto-save fetched content + Claude response to `fetch_outputs/`
- **`/extract` command** — On-demand structured data extraction using LangExtract (Gemini)
- **LangExtract integration** — Auto-enhance general URL content with structured extraction

---

## v2.2 (2026-02-09)

### URL Processing
- **fxtwitter API** — X/Twitter tweet parsing (author, text, media, engagement, quotes)
- **yt-dlp** — YouTube / social media metadata extraction
- **HTTP fallback** — Page title + OG/meta description extraction
- **Cascade strategy** — X/Twitter: fxtwitter → yt-dlp → HTTP | YouTube: yt-dlp → HTTP | Others: HTTP

---

## v2.1 (2026-01-31)

### Core
- `ConversationHistory` class with rolling persistence (JSON)
- Daily log rotation (`TimedRotatingFileHandler`) + auto-cleanup
- `/clear` / `/history` / `/status` / `/help` commands
- `ClaudeBridge` class encapsulating Claude CLI calls + conversation management

---

## v1.0 (2026-01-29)

### Initial Release
- Basic Telegram ↔ Claude Code CLI bridge
- `/exec` command for direct shell execution
- `ALLOWED_USER_IDS` whitelist authorization
