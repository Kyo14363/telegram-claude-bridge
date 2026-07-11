"""
T-M-B Configuration — CONFIG dict + path setup + system_prompt fragments.

Public build: every path / secret / numeric tunable comes from environment
variables (loaded from .env by start_bridge.bat). No hard-coded user paths.

Sections:
- CONFIG (paths, SDK, reply fetch, Telegram limits)
- Auto-create runtime directories
- _TokenRedactFilter (log redaction)
- Structured system_prompt.append fragments
  (Browser autoclose / Provenance / Telegram output style)
"""

import os
import re
import logging
from pathlib import Path
from typing import List


# === Version ===
VERSION = "3.1"
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
    "FETCH_OUTPUT_DIR": _env_path("FETCH_OUTPUT_DIR", BASE_DIR / "fetch_outputs"),
    "IMAGE_ANALYSIS_ENABLED": _env_bool("IMAGE_ANALYSIS_ENABLED", True),
    "MAX_IMAGES_PER_MESSAGE": _env_int("MAX_IMAGES_PER_MESSAGE", 5),
    "IMAGE_ANALYSIS_TIMEOUT": _env_int("IMAGE_ANALYSIS_TIMEOUT", 30),
    "OBSIDIAN_MOBILE_DIR": _env_path("OBSIDIAN_MOBILE_DIR", BASE_DIR / "obsidian_clippings"),
    # --- Reply capture ---
    "REPLY_KEYWORDS": _parse_keywords(),
    "REPLY_MAX_FETCH": _env_int("REPLY_MAX_FETCH", 80),
    "TWIKIT_COOKIES": _env_optional_path("TWIKIT_COOKIES"),
    # --- SDK ---
    "SDK_PERMISSION_MODE": os.environ.get("SDK_PERMISSION_MODE", "bypassPermissions"),
    "SDK_SETTING_SOURCES": [p.strip() for p in os.environ.get("SDK_SETTING_SOURCES", "user,project,local").split(",") if p.strip()],
    "SDK_SKILLS": os.environ.get("SDK_SKILLS", "all"),
    "SDK_PROGRESS_EDIT_INTERVAL": _env_float("SDK_PROGRESS_EDIT_INTERVAL", 1.5),
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


# === Log token redaction filter ===
# python-telegram-bot's internal httpx writes the full API URL (including the
# bot<token>) into INFO logs. A leaked screenshot of the log would expose the
# token, so we mask it before it hits any handler.
class _TokenRedactFilter(logging.Filter):
    _PATTERN = re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str) and "bot" in record.msg:
                record.msg = self._PATTERN.sub("bot<REDACTED>", record.msg)
            if record.args:
                record.args = tuple(
                    self._PATTERN.sub("bot<REDACTED>", a) if isinstance(a, str) else a
                    for a in record.args
                )
        except Exception:
            pass
        return True


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


_TELEGRAM_OUTPUT_STYLE = (
    "## Output style for Telegram replies\n"
    "Your replies are sent to a Telegram chat (4096-char per message, mobile screen). "
    "Apply these rules unless the user's request explicitly calls for verbosity "
    "(e.g. \"detailed explanation\", \"long analysis\", \"deep dive\"):\n\n"
    "1. **No sycophantic openings.** Skip \"Of course!\", \"Sure, I'd be happy to...\". "
    "Jump straight to the answer.\n"
    "2. **No closing fluff.** Skip \"Hope this helps!\", \"Let me know if you need "
    "anything else\". End when the answer ends.\n"
    "3. **Don't restate the user's question.** They wrote it; they know what "
    "they asked.\n"
    "4. **Tight paragraphs and bullets.** Mobile reading. Avoid long prose blocks; "
    "prefer short paragraphs (≤3 lines) and bullet lists.\n"
    "5. **Code blocks for actual code only.** Don't wrap regular sentences in "
    "triple-backticks for visual emphasis. Use inline `code` for commands/paths/"
    "identifiers and triple-backtick blocks only for multi-line code.\n\n"
    "These rules YIELD to the Provenance protocol above for URL analysis tasks — "
    "the structured Claims/Sources blocks are required output, not \"closing fluff\"."
)


SYSTEM_PROMPT_APPEND = "\n\n".join([
    _BROWSER_AUTOCLOSE,
    _PROVENANCE_PROTOCOL,
    _TELEGRAM_OUTPUT_STYLE,
])
