# Changelog

## v3.1 Public Sync

- Upgraded the public bridge to the persistent Claude Agent SDK architecture.
- Added environment-driven configuration for bot token, allowed users, paths, SDK settings, and optional Gemini features.
- Added token redaction for Telegram API URLs in logs.
- Added `trafilatura` extraction for general articles/news pages before HTTP fallback.
- Added Playwright-first X/Twitter reply capture prompts.
- Trimmed the public command set to status, session, browser, history, and diagnostics commands.
- Added `metrics.py` and `sdk_smoke_test.py`.
- Updated runtime dependency list and startup script.

## v2.x

- Original stateless `claude --print` bridge.
- URL preprocessing, image analysis, and basic Telegram command handling.
