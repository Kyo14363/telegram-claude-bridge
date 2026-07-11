"""
T-M-B Configuration — CONFIG dict + path setup + system_prompt fragments.

Public build: every path / secret / numeric tunable comes from environment
variables (loaded from .env by start_bridge.bat). No hard-coded user paths.

Sections:
- CONFIG (paths, SDK, reply fetch, queue, heartbeat, Telegram limits)
- Auto-create runtime directories
- _TokenRedactingFormatter (log redaction; formatter-level since v3.2)
- Structured system_prompt.append fragments
  (Browser autoclose / Provenance / Editorial triage / Telegram output style)
"""

import os
import re
import logging
from pathlib import Path
from typing import List


# === Version ===
VERSION = "3.3.3"
VERSION_LABEL = f"Telegram Claude Code Bridge v{VERSION}"


# === Helpers for env-driven config ===

BASE_DIR = Path(__file__).resolve().parent


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(os.path.expandvars(os.path.expanduser(raw))) if raw else default


def _env_optional_path(name: str):
    raw = os.environ.get(name, "").strip()
    return Path(os.path.expandvars(os.path.expanduser(raw))) if raw else None


def _parse_allowed_user_ids() -> List[int]:
    raw = os.environ.get("ALLOWED_USER_IDS") or os.environ.get("ALLOWED_USER_ID", "")
    ids: List[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            logging.warning(f"Ignoring invalid Telegram user id: {part!r}")
    return ids


def _parse_keywords() -> List[str]:
    raw = os.environ.get("REPLY_KEYWORDS", "").strip()
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    return [
        "capture replies",
        "fetch replies",
        "收錄留言",
        "抓回覆",
        "看留言",
        "抓取下方留言",
        "收錄下方留言",
        "抓取留言",
    ]


# === Configuration ===

_TOKEN_FROM_ENV = (
    os.environ.get("TELEGRAM_BOT_TOKEN")
    or os.environ.get("TG_BOT_TOKEN")
    or ""
).strip()
_CLAUDE_CLI_ENV = os.environ.get("CLAUDE_CLI_PATH", "").strip()
_DEFAULT_CLAUDE_CLI = Path(os.path.expandvars(
    r"%APPDATA%\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe"
))

CONFIG = {
    "TELEGRAM_BOT_TOKEN": _TOKEN_FROM_ENV or "MISSING_TELEGRAM_BOT_TOKEN_ENV",
    "ALLOWED_USER_IDS": _parse_allowed_user_ids(),
    "WORKING_DIR": _env_path("WORKING_DIR", Path.home() / "claude-workspace"),
    "BASE_DIR": BASE_DIR,
    "HISTORY_FILE": _env_path("HISTORY_FILE", BASE_DIR / "conversation_history.json"),
    "SESSION_STATE_FILE": _env_path("SESSION_STATE_FILE", BASE_DIR / "session_state.json"),
    "CLAUDE_CLI_PATH": Path(os.path.expandvars(os.path.expanduser(_CLAUDE_CLI_ENV))) if _CLAUDE_CLI_ENV else _DEFAULT_CLAUDE_CLI,
    "LOG_DIR": _env_path("LOG_DIR", BASE_DIR / "logs"),
    "TIMEOUT": _env_int("TIMEOUT", 600),
    "MAX_HISTORY_ROUNDS": _env_int("MAX_HISTORY_ROUNDS", 10),
    "ALLOW_DANGEROUS": _env_bool("ALLOW_DANGEROUS", False),
    "LOG_RETENTION_DAYS": _env_int("LOG_RETENTION_DAYS", 14),
    "URL_FETCH_TIMEOUT": _env_int("URL_FETCH_TIMEOUT", 15),
    # --- langextract auto-enhance (v3.2.4) ---
    # Whether to run an extra langextract structured-extraction pass (entity
    # bullets) on every general-URL fetch and append it to the content given
    # to Claude. Mostly redundant for Claude's own analysis and costs one
    # ~2-4s Gemini call per URL, so off by default; the /extract command is
    # unaffected and remains available on demand.
    "LANGEXTRACT_AUTO_ENHANCE": _env_bool("LANGEXTRACT_AUTO_ENHANCE", False),
    "FETCH_OUTPUT_DIR": _env_path("FETCH_OUTPUT_DIR", BASE_DIR / "fetch_outputs"),
    "IMAGE_ANALYSIS_ENABLED": _env_bool("IMAGE_ANALYSIS_ENABLED", True),
    "MAX_IMAGES_PER_MESSAGE": _env_int("MAX_IMAGES_PER_MESSAGE", 5),
    "IMAGE_ANALYSIS_TIMEOUT": _env_int("IMAGE_ANALYSIS_TIMEOUT", 30),
    "OBSIDIAN_MOBILE_DIR": _env_path("OBSIDIAN_MOBILE_DIR", BASE_DIR / "obsidian_clippings"),
    # --- Reply capture ---
    "REPLY_KEYWORDS": _parse_keywords(),
    "REPLY_MAX_FETCH": _env_int("REPLY_MAX_FETCH", 80),
    "TWIKIT_COOKIES": _env_optional_path("TWIKIT_COOKIES"),
    # --- Task queue (v3.2) ---
    # When busy, new messages queue up instead of bouncing (mobile usage is
    # naturally bursty). The limit counts waiting messages (not the running
    # one); only past the limit does the bridge bounce.
    "QUEUE_MAX_WAITING": _env_int("QUEUE_MAX_WAITING", 2),
    # --- Liveness heartbeat (v3.2.3) ---
    # After httpx log-noise reduction, "healthy" means "silent" — the log can
    # no longer distinguish healthy idle from a wedged event loop. Heartbeat:
    # touch logs/heartbeat.txt every HEARTBEAT_FILE_SEC (a liveness signal an
    # external watchdog can check) and write one [hb] INFO line every
    # HEARTBEAT_LOG_SEC (24 lines/day).
    "HEARTBEAT_FILE_SEC": _env_int("HEARTBEAT_FILE_SEC", 300),
    "HEARTBEAT_LOG_SEC": _env_int("HEARTBEAT_LOG_SEC", 3600),
    # --- SDK (v3.0) ---
    "SDK_PERMISSION_MODE": os.environ.get("SDK_PERMISSION_MODE", "bypassPermissions"),
    "SDK_SETTING_SOURCES": [p.strip() for p in os.environ.get("SDK_SETTING_SOURCES", "user,project,local").split(",") if p.strip()],
    "SDK_SKILLS": os.environ.get("SDK_SKILLS", "all"),
    # --- Model pinning (v3.2.5) ---
    # Unattended automation must pin a CONCRETE model id — never a bare alias,
    # and never inherit the global ~/.claude/settings.json model (a global
    # settings drift once silently switched this bridge to the most expensive
    # model available). setting_sources includes 'user', so an explicit
    # model= here is what keeps the bridge immune to that drift.
    "SDK_MODEL": os.environ.get("SDK_MODEL", "claude-sonnet-5"),
    "SDK_PROGRESS_EDIT_INTERVAL": _env_float("SDK_PROGRESS_EDIT_INTERVAL", 1.5),
    # --- /resume: continue the desktop Claude Code session headlessly (v3.2.6) ---
    # /resume spawns a one-shot headless `claude -p --resume <sid>` that
    # continues the most recent desktop Claude Code session (same transcript
    # and todos, back in the original project folder). Unlike the resident
    # SDK session it is slow (loads the whole transcript), needs CLI auth,
    # and has no streaming — see /handoff for the lightweight alternative.
    # RESUME_MODEL is pinned to a concrete id for the same reason as SDK_MODEL.
    "RESUME_MODEL": os.environ.get("RESUME_MODEL", "claude-sonnet-5"),
    "RESUME_TIMEOUT": _env_int("RESUME_TIMEOUT", 300),
    "RESUME_LIVE_GUARD_MIN": _env_int("RESUME_LIVE_GUARD_MIN", 5),
    # --- /handoff: brief the resident SDK session on the desktop session (v3.3.0) ---
    # Complements /resume: instead of waking the desktop session, /handoff
    # READS the tail of its transcript, condenses it into a briefing, and
    # feeds that to the already-connected resident SDK session. Streaming,
    # no CLI auth, and read-only — safe even while the desktop app is open.
    #   HANDOFF_TAIL_RECORDS: how many meaningful records to excerpt
    #   HANDOFF_MAX_CHARS:    cap for the assembled context block
    #   HANDOFF_SNIPPET_CHARS: per-record truncation length
    "HANDOFF_TAIL_RECORDS": _env_int("HANDOFF_TAIL_RECORDS", 24),
    "HANDOFF_MAX_CHARS": _env_int("HANDOFF_MAX_CHARS", 6000),
    "HANDOFF_SNIPPET_CHARS": _env_int("HANDOFF_SNIPPET_CHARS", 500),
    # --- Playwright idle auto-close ---
    "BROWSER_IDLE_CLOSE_MIN": _env_int("BROWSER_IDLE_CLOSE_MIN", 5),
    "BROWSER_IDLE_CHECK_SEC": _env_int("BROWSER_IDLE_CHECK_SEC", 60),
    # --- Telegram limits ---
    "TG_MAX_PHOTO_BYTES": _env_int("TG_MAX_PHOTO_BYTES", 10 * 1024 * 1024),
    # --- URL extraction ---
    "THIN_CONTENT_THRESHOLD": _env_int("THIN_CONTENT_THRESHOLD", 200),
}

CONFIG["WORKING_DIR"].mkdir(parents=True, exist_ok=True)
CONFIG["LOG_DIR"].mkdir(parents=True, exist_ok=True)
CONFIG["FETCH_OUTPUT_DIR"].mkdir(parents=True, exist_ok=True)
CONFIG["OBSIDIAN_MOBILE_DIR"].mkdir(parents=True, exist_ok=True)


# === Log token redaction formatter ===
# python-telegram-bot's internal httpx writes the full API URL (including
# bot<token>) into INFO logs, so a leaked screenshot or pasted log tail
# exposes the token.
#
# v3.2: moved from a logger-level Filter to a handler-level Formatter. A
# filter attached to the root logger only sees records emitted DIRECTLY
# through the root logger — records from child loggers (httpx, telegram.ext)
# propagate to the root *handlers* without passing root-logger filters, so
# the old filter never caught the main leak source. Three leak shapes are
# covered here in one place, because Formatter.format() is the last stop
# before the string is written out:
#   1. propagated child-logger records
#   2. lazy %-args containing non-str objects (httpx passes httpx.URL objects;
#      the token only appears when the handler formats the record)
#   3. exception tracebacks (appended by the formatter, after any filter)
class _TokenRedactingFormatter(logging.Formatter):
    _PATTERN = re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}")

    def format(self, record: logging.LogRecord) -> str:
        s = super().format(record)
        if "bot" in s:
            s = self._PATTERN.sub("bot<REDACTED>", s)
        return s


# === System prompt fragments (structured) ===
# Each fragment is self-contained. To toggle a fragment via CONFIG flag or add
# a new one, edit the constant list below and SYSTEM_PROMPT_APPEND assembly.

_BROWSER_AUTOCLOSE = (
    "When you finish a self-contained browser task (single screenshot, "
    "one-shot Q&A on a website, single page extraction), call "
    "mcp__playwright__browser_close at the end to free resources. "
    "Skip this if the user is in an obvious multi-step browsing session "
    "(e.g. they explicitly say 'keep the browser open' or are mid-flow)."
)


_PROVENANCE_PROTOCOL = (
    "## Provenance protocol (URL/clipping analysis only)\n"
    "When the user gives you a URL and you analyze its fetched content for "
    "Obsidian filing, structure your response so every factual claim is "
    "traceable. Apply ONLY to URL/article analysis — skip for general chat, "
    "code tasks, or interactive browsing.\n\n"
    "Rules:\n"
    "1. For every claim about names, numbers, dates, quotes, or specific "
    "events, attach a footnote marker [^N] right after the claim, pointing "
    "to the source paragraph in the fetched content.\n"
    "2. End your analysis with two sections in this exact order:\n\n"
    "## Claims\n"
    "- <claim, one sentence> — confidence: high|med|low — [^N]\n"
    "- (one bullet per atomic claim; group related ones if natural)\n\n"
    "## Sources\n"
    "[^1]: <URL or section anchor> — \"<≤80-char quote from source>\" — fetched <YYYY-MM-DD>\n"
    "[^2]: ...\n\n"
    "Confidence calibration:\n"
    "- high  = directly stated in fetched text, verbatim or near-verbatim\n"
    "- med   = reasonably inferred from fetched text, not literal\n"
    "- low   = single weak source, opinion-as-fact, or cross-reference needed\n"
    "- If a claim has NO basis in the fetched content (your prior knowledge "
    "or speculation), mark it [^speculation] and do NOT fabricate a source. "
    "Prefer omitting unsupported claims over inventing citations.\n\n"
    "Keep the analysis prose itself natural — citations should aid the "
    "reader, not bury the writing."
)


# Editorial triage (v3.3.3)
# Pain point: newer models follow instructions more LITERALLY — the Telegram
# output style's "jump straight to the answer" plus the Provenance protocol's
# "stay close to the fetched text" made the model treat its own spontaneous
# editorial judgment as a violation, degrading replies into faithful
# transcription. Fix: make the first-pass review an explicitly REQUIRED
# output, turning the literal instruction-following into an asset.
_EDITORIAL_TRIAGE = (
    "## Editorial triage (URL/clipping analysis only)\n"
    "When the user sends a URL or clipped content for analysis, OPEN your "
    "reply with your own 2-3 sentence editorial judgment BEFORE any summary "
    "or restatement of the fetched content. Cover, briefly:\n"
    "- Novelty: genuinely new, or recycled/already-known?\n"
    "- Credibility: source quality; marketing language vs verifiable claims.\n"
    "- Relation to prior context: confirms / extends / contradicts what is "
    "already established.\n"
    "- Verdict: worth keeping or acting on, or safe to skip.\n"
    "Only after this judgment give the structured summary. Restating fetched "
    "content without a leading judgment is a protocol violation — the user "
    "relies on you as a first-pass reviewer, not a transcriber.\n"
    "This opening judgment is REQUIRED output, not sycophantic filler — it "
    "does not conflict with the Telegram output style rules below."
)


_TELEGRAM_OUTPUT_STYLE = (
    "## Output style for Telegram replies\n"
    "Your replies are sent to a Telegram chat (4096-char per message, mobile screen). "
    "Apply these rules unless the user's request explicitly calls for verbosity "
    "(e.g. \"詳細解釋\", \"long analysis\", \"deep dive\"):\n\n"
    "1. **No sycophantic openings.** Skip \"Of course!\", \"好的！\", "
    "\"Sure, I'd be happy to...\", \"當然可以\". Jump straight to the answer.\n"
    "2. **No closing fluff.** Skip \"Hope this helps!\", \"希望對你有幫助\", "
    "\"Let me know if you need anything else\". End when the answer ends.\n"
    "3. **Don't restate the user's question.** They wrote it; they know what "
    "they asked. No \"你問的是關於 X...\" or \"Regarding your question about X...\".\n"
    "4. **Tight paragraphs and bullets.** Mobile reading. Avoid long prose blocks; "
    "prefer short paragraphs (≤3 lines) and bullet lists.\n"
    "5. **Code blocks for actual code only.** Don't wrap regular sentences in "
    "triple-backticks for visual emphasis. Use inline `code` for commands/paths/"
    "identifiers and triple-backtick blocks only for multi-line code.\n"
    "6. **Reply in the user's language.** Match the language the user writes "
    "in unless they explicitly ask otherwise. Technical terms/identifiers may "
    "stay in English.\n\n"
    "These rules YIELD to the Provenance protocol and Editorial triage above for "
    "URL analysis tasks — the leading judgment and the structured Claims/Sources "
    "blocks are required output, not \"fluff\"."
)


SYSTEM_PROMPT_APPEND = "\n\n".join([
    _BROWSER_AUTOCLOSE,
    _PROVENANCE_PROTOCOL,
    _EDITORIAL_TRIAGE,
    _TELEGRAM_OUTPUT_STYLE,
])
