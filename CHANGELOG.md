# Changelog

## v3.1.5 Public Sync (2026-05-08)

- **Modular layout** — split the monolithic `telegram_bridge_claude.py` into
  focused modules. The entry script keeps the same name but is now ~150 lines
  of wiring; the heavy lifting lives in `config.py`, `bridge_core.py`,
  `handlers.py`, `shortcuts.py`, `metrics.py`, `url_fetchers.py`, `vision.py`,
  and `reply_fetcher.py`.
- **`google-genai` SDK** — Vision migrates off the deprecated
  `google.generativeai` package onto the new `google-genai` client.
- **Structured system prompt** — three named fragments (browser autoclose,
  provenance protocol, Telegram output style) replace the inline string,
  making it easier to toggle individual rules.
- **Reply capture refinements** — `reply_fetcher.py` now uses loose verb +
  noun matching ("收錄...留言" with words in between) on top of the strict
  keyword list, classifies twikit errors for clearer diagnostics, and falls
  back to a Playwright prompt when twikit is unavailable.
- **Idle browser auto-close** — Playwright Chromium closes after 5 minutes
  of inactivity (`BROWSER_IDLE_CLOSE_MIN`).
- **Provenance protocol** — when the bridge feeds Claude a fetched URL for
  Obsidian filing, Claude is asked to attach Claims/Sources blocks with
  per-claim confidence calibration so summaries stay traceable.
- **Optional `psutil` orphan cleanup** — `/clear` now best-effort terminates
  Playwright/Chromium descendants that survive `claude.exe` exit on Windows.
- **`stats.json` schema** — reply success/failure counts and a `recent_errors`
  ring buffer for `/stats` diagnosis.
- **Drop unused `LICENSE`-adjacent fields** — config moves to `config.py`
  with env helpers (`_env_bool`, `_env_int`, `_env_path`, `_env_optional_path`).

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
