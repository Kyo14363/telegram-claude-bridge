# Changelog

## v3.3.3 (2026-07-11) — Editorial triage

- **New `_EDITORIAL_TRIAGE` system-prompt fragment** — when a URL/clipping is
  dispatched for analysis, the reply must OPEN with the model's own 2-3
  sentence editorial judgment (novelty / credibility / confirms-extends-
  contradicts / keep-or-skip verdict) before any structured summary.
  Motivation: newer models follow instructions more literally — "jump straight
  to the answer" plus "stay close to the fetched text" made the model treat
  its own first-pass judgment as a violation and degrade into transcription.
  Making the judgment an explicitly *required* output turns that literalness
  into an asset.

## v3.3.2 (2026-07-09) — /cclog rewritten in Python

- `/cclog` used to shell out to PowerShell and parse Claude Code transcripts
  with `ConvertFrom-Json`. PowerShell 5.1 chokes on the huge single-line
  records real transcripts contain (one line can take 20-30 s), blowing the
  subprocess timeout. Parsing now happens in Python (`json.loads`, sub-10 ms
  on the same files), with project-keyword targeting: `/cclog` = most recent
  session, `/cclog <keyword>` = match a project, numeric argument = message
  count (max 3).
- All PowerShell shortcut invocations now pass `-NoProfile`.

## v3.3.1 (2026-07-02) — SDK model bump

- `SDK_MODEL` / `RESUME_MODEL` default moves `claude-sonnet-4-6` →
  `claude-sonnet-5` (still a full model id, never an alias — see v3.2.5).
  The bridge's two quality-sensitive paths (Playwright fallback browsing and
  `/handoff` briefings) are exactly where the newer model improves most.

## v3.3.0 (2026-07-01) — /handoff

- **New `/handoff <instruction>`** — reads the tail of the most recent
  *desktop* Claude Code session's transcript, condenses it into a briefing
  (skipping UI-state records, tool noise, and meta lines), and feeds it to
  the already-connected resident SDK session. Complements `/resume`:
  streaming progress, no CLI auth, and read-only over the desktop transcript,
  so it is safe even while the desktop app is still open (measured ~22 s vs
  ~3 min for a full `/resume` of a 1.5 MB session).
- Transcript parsing distinguishes human messages from tool output by content
  shape, filters `isMeta`/`isSidechain` records, and renders a compact
  oldest-to-newest digest capped by `HANDOFF_MAX_CHARS`.

## v3.2.6 – v3.2.8 (2026-07-01) — /resume

- **New `/resume`** — continues the most recent desktop Claude Code session
  headlessly via `claude -p --resume <sid>`, back in the session's original
  working directory, appending to the same transcript. A LIVE_GUARD refuses
  to resume a session that was written to in the last N minutes (the desktop
  app may still be attached) unless `--force` is given.
- **Windows event-loop fix** — `asyncio.create_subprocess_exec` needs a
  ProactorEventLoop, but importing twikit silently switches the global policy
  to SelectorEventLoop, which raises an *empty* `NotImplementedError`. All
  async subprocess work in handler context now goes through
  `run_in_executor(subprocess.run)` instead.
- **Progress heartbeat** — long resumes edit a progress message every ~25 s
  so a multi-minute resume doesn't look like a hang from the phone.

## v3.2.5 (2026-06-18) — Model pinning

- `bridge_core` now passes an explicit `model=CONFIG["SDK_MODEL"]` to the
  Agent SDK. Because `setting_sources` includes `user`, the bridge used to
  inherit whatever model the global `~/.claude/settings.json` happened to
  name — a silent drift once switched it to the most expensive model
  available. Rule adopted: unattended automation pins a concrete model id,
  never a bare alias, never an inherited default. Startup log and `/status`
  print the pinned model for auditability.

## v3.2.4 (2026-06-13) — PDF extraction + langextract API migration

- **PDF path** — `*.pdf` and `arxiv.org/pdf/` URLs are detected up front and
  extracted with PyMuPDF instead of being fed to the HTML extractor (which
  returned raw `%PDF` bytes). A magic-byte guard in the trafilatura path
  redirects misdetected PDFs too.
- **langextract API drift** — migrated to the current positional
  `text_or_documents` / `prompt_description` / `examples` / `model_id` call
  shape; `fetch_urls=False` avoids surprise network access on URLs found in
  article text. Automatic per-URL enhancement is now a config flag
  (`LANGEXTRACT_AUTO_ENHANCE`, default off — redundant for the SDK session
  and costs one Gemini call per URL); `/extract` is unaffected.

## v3.2.3 (2026-06-12) — Liveness heartbeat + watchdog wedge detection

- After the httpx log-noise reduction (v3.2), a silent log could mean either
  "healthy and idle" or "event loop wedged". The bridge now touches
  `logs/heartbeat.txt` every 5 minutes from its main asyncio loop and writes
  one `[hb] alive` line per hour.
- `tmb_watchdog.ps1` (Task Scheduler, every 5 min) upgraded from
  existence-checking to three-way: process gone → restart; process alive but
  heartbeat stale > 15 min → kill the wedged tree and restart; heartbeat file
  missing → only treated as wedged after a 20-minute startup grace window.

## v3.2.1 – v3.2.2 (2026-06-11) — Shortcut fixes + command slimming

- `/cclog` pointed at a log directory that never existed on any machine;
  rewritten against the real Claude Code transcript location
  (`~/.claude/projects/<slug>/<session>.jsonl`).
- `/uptime` used `Get-Process`'s `CommandLine` property, which does not exist
  in PowerShell 5.1 — switched to `Get-CimInstance`, and the process filter
  tightened to `telegram_bridge_claude` so unrelated bridges are not matched.
- Removed `/session` (80 % overlap with `/status`; the useful parts merged
  into `/status`) and `/history` (obsolete since the SDK manages context).

## v3.2 (2026-06-10) — Security + resilience batch

- **Token redaction actually works now.** An audit found all three layers of
  log-token protection had never functioned: (1) the redaction filter was
  attached to the root *logger*, which propagated child-logger records
  (httpx's polling URLs — the main leak) straight to handlers without
  filtering; (2) the rotating-file handler's `extMatch` never matched the
  custom suffix, so old logs were never deleted; (3) the cleanup fallback
  mis-parsed its own filenames and deleted nothing. Fixes: redaction moved to
  a handler-level `Formatter` (the last stop before output — also covers lazy
  %-args objects and tracebacks), `extMatch` synced to the suffix, cleanup
  parses dates by regex, and httpx polling INFO noise (~8,600 lines/day) is
  gated to WARNING. Lesson recorded in-repo: verify security fixes by
  observing final output, not by confirming the mechanism exists.
- **FIFO task queue** — while a task runs, new messages queue (the
  `asyncio.Lock` waiter order *is* the queue) with live "queued, N ahead"
  feedback, bouncing only past `QUEUE_MAX_WAITING`. Busy bounces are also
  detected on the write path so they can never be filed into notes as if
  they were analysis output (a TOCTOU guard covers the race).
- **URL cleaning for CJK input** — "URL，comment" with no space is normal in
  Chinese; full-width punctuation no longer gets swallowed into the URL
  (while native CJK paths like `wiki/台灣` stay intact), with bracket
  balancing for Wikipedia-style `_(...)` suffixes.
- **Direct browser cleanup** — `/browser-close` and the idle watchdog now
  terminate Playwright browser processes via psutil inside the SDK process
  tree (never touching the user's own browser), instead of spending a model
  round-trip asking Claude to close them.
- **Self-healing** — `tmb_watchdog.ps1` + `tmb_watchdog.vbs` (silent Task
  Scheduler wrapper) restart the bridge within 5 minutes if it dies. The
  bridge's whole value is being the remote entry point; its own death was
  the one failure it could not report.

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
