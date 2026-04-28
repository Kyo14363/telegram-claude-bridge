#!/usr/bin/env python3
"""

Telegram <-> Claude Code Bridge v3.1
=====================================
v3.0 核心變化：執行器從 stateless `claude --print` subprocess 升級為
**常駐 Claude Agent SDK client**。

帶來的能力擴展：
- 跨訊息保留 Claude Code session（plan mode、todos、檔案 cache、background tasks）
- MCP server 啟動一次常駐運行（Chrome/Preview/Obsidian/Windows-MCP 等的 session 不再每訊息重建）
- 全域 settings/hooks/skills 自動載入（setting_sources=['user','project','local']）
- Tool 使用即時推送進度到 Telegram（streaming progress）
- session_id 持久化，bridge 重啟後可 resume 同一條工作線
- 保留所有 v2.7 既有能力：URL 預處理、圖片分析、回覆抓取、Obsidian 落地、shortcuts

備份：v2.7 保留為 telegram_bridge_claude_v2.7.py 作為回溯範本。
=======
Telegram <-> Claude Code Bridge with Context Memory v2.6
========================================================
- Conversation history with context memory
- Daily rotating log files with auto-cleanup
- URL preprocessing: auto-fetch link content (fxtwitter + yt-dlp + HTTP fallback)
- Image analysis: auto-download tweet images and analyze via Gemini Vision
- Modular architecture: vision.py + url_fetchers.py
- Twitter Article (long-form Notes) parsing support
- GIF thumbnail extraction for visual analysis

"""

import os
import sys
import json
import asyncio
import subprocess

import threading
=======

import re
import glob
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Tuple
from dataclasses import dataclass, field, asdict
import logging
from logging.handlers import TimedRotatingFileHandler


VERSION = "3.1"
VERSION_LABEL = f"Telegram Claude Code Bridge v{VERSION}"


# === Configuration ===
# Public build: secrets and machine-specific paths come from environment variables.
# TELEGRAM_BOT_TOKEN is preferred; TG_BOT_TOKEN is kept as a private-build alias.
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


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(os.path.expandvars(os.path.expanduser(raw))) if raw else default


def _parse_allowed_user_ids() -> List[int]:
    raw = os.environ.get("ALLOWED_USER_IDS") or os.environ.get("ALLOWED_USER_ID", "")
    ids = []
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
        "\u6536\u9304\u7559\u8a00",
        "\u6293\u56de\u8986",
        "\u770b\u7559\u8a00",
    ]


_TOKEN_FROM_ENV = (os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN") or "").strip()
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
    "REPLY_KEYWORDS": _parse_keywords(),
    "REPLY_MAX_FETCH": _env_int("REPLY_MAX_FETCH", 80),
    "SDK_PERMISSION_MODE": os.environ.get("SDK_PERMISSION_MODE", "bypassPermissions"),
    "SDK_SETTING_SOURCES": [p.strip() for p in os.environ.get("SDK_SETTING_SOURCES", "user,project,local").split(",") if p.strip()],
    "SDK_SKILLS": os.environ.get("SDK_SKILLS", "all"),
    "SDK_PROGRESS_EDIT_INTERVAL": float(os.environ.get("SDK_PROGRESS_EDIT_INTERVAL", "1.5")),
    "BROWSER_IDLE_CLOSE_MIN": _env_int("BROWSER_IDLE_CLOSE_MIN", 5),
    "BROWSER_IDLE_CHECK_SEC": _env_int("BROWSER_IDLE_CHECK_SEC", 60),
    "TG_MAX_PHOTO_BYTES": _env_int("TG_MAX_PHOTO_BYTES", 10 * 1024 * 1024),
    "THIN_CONTENT_THRESHOLD": _env_int("THIN_CONTENT_THRESHOLD", 200),
=======
# === Load .env ===
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv is optional if env vars are set externally

# === Configuration ===
BASE_DIR = Path(__file__).parent.resolve()

def _parse_user_ids(raw: str) -> list:
    """Parse comma-separated user IDs from env var."""
    if not raw:
        return []
    return [int(uid.strip()) for uid in raw.split(",") if uid.strip().isdigit()]

CONFIG = {
    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "ALLOWED_USER_IDS": _parse_user_ids(os.getenv("ALLOWED_USER_ID", "")),
    "CLAUDE_CLI": os.getenv("CLAUDE_CLI_PATH", "claude"),
    "WORKING_DIR": Path(os.getenv("WORKING_DIR", str(Path.home() / "claude-workspace"))),
    "BASE_DIR": BASE_DIR,
    "HISTORY_FILE": BASE_DIR / "conversation_history.json",
    "LOG_DIR": BASE_DIR / "logs",
    "TIMEOUT": int(os.getenv("TIMEOUT", "300")),
    "MAX_HISTORY_ROUNDS": int(os.getenv("MAX_HISTORY_ROUNDS", "10")),
    "ALLOW_DANGEROUS": os.getenv("ALLOW_DANGEROUS", "false").lower() == "true",
    "LOG_RETENTION_DAYS": int(os.getenv("LOG_RETENTION_DAYS", "14")),
    "URL_FETCH_TIMEOUT": int(os.getenv("URL_FETCH_TIMEOUT", "15")),
    "FETCH_OUTPUT_DIR": BASE_DIR / "fetch_outputs",
    "IMAGE_ANALYSIS_ENABLED": os.getenv("IMAGE_ANALYSIS_ENABLED", "true").lower() == "true",
    "MAX_IMAGES_PER_MESSAGE": int(os.getenv("MAX_IMAGES_PER_MESSAGE", "5")),
    "IMAGE_ANALYSIS_TIMEOUT": int(os.getenv("IMAGE_ANALYSIS_TIMEOUT", "30")),

}

CONFIG["WORKING_DIR"].mkdir(parents=True, exist_ok=True)
CONFIG["LOG_DIR"].mkdir(parents=True, exist_ok=True)
CONFIG["FETCH_OUTPUT_DIR"].mkdir(parents=True, exist_ok=True)

CONFIG["OBSIDIAN_MOBILE_DIR"].mkdir(parents=True, exist_ok=True)

# === 日誌設定（每日輪換）===
=======

# === Logging (daily rotation) ===

def setup_logging():
    log_file = CONFIG["LOG_DIR"] / "bridge.log"
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    file_handler = TimedRotatingFileHandler(
        log_file, when='midnight', interval=1,
        backupCount=CONFIG["LOG_RETENTION_DAYS"], encoding='utf-8'
    )
    file_handler.suffix = "%Y-%m-%d.log"
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    return logging.getLogger(__name__)

def cleanup_old_logs():
    cutoff_date = datetime.now() - timedelta(days=CONFIG["LOG_RETENTION_DAYS"])
    log_pattern = CONFIG["LOG_DIR"] / "bridge.log.*"
    deleted_count = 0
    for log_file in glob.glob(str(log_pattern)):
        try:
            date_str = log_file.split('.')[-1].replace('.log', '')
            file_date = datetime.strptime(date_str, "%Y-%m-%d")
            if file_date < cutoff_date:
                os.remove(log_file)
                deleted_count += 1
        except (ValueError, OSError):
            continue
    if deleted_count > 0:

        logging.info(f"已清理 {deleted_count} 個超過 {CONFIG['LOG_RETENTION_DAYS']} 天的舊 log 檔案")

logger = setup_logging()

# v3.1 P1：log token 遮罩 filter
# python-telegram-bot 內部的 httpx 會把完整 API URL（含 bot<token>）寫進 INFO log，
# 任何不小心截圖/外傳 log 都會洩漏 token。掛一個 filter 攔下這串並遮罩。
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

# 掛到 root logger，所有子 logger（含 httpx / telegram.ext）都會過這層
logging.getLogger().addFilter(_TokenRedactFilter())

# === 外部模組 ===
try:
    from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
=======
        logging.info(f"Cleaned up {deleted_count} log files older than {CONFIG['LOG_RETENTION_DAYS']} days")

logger = setup_logging()

# === External modules ===
try:
    from telegram import Update

    from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
    TELEGRAM_LIB_AVAILABLE = True
except ImportError:
    TELEGRAM_LIB_AVAILABLE = False


try:
    from claude_agent_sdk import (
        ClaudeSDKClient, ClaudeAgentOptions,
        AssistantMessage, UserMessage, SystemMessage, ResultMessage,
        TextBlock, ToolUseBlock, ToolResultBlock, ThinkingBlock,
    )
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False

from url_fetchers import (
    detect_urls, preprocess_urls, save_fetch_output, save_to_obsidian,
    extract_structured_data,
    REQUESTS_AVAILABLE, YTDLP_AVAILABLE, LANGEXTRACT_AVAILABLE,
)
from vision import GENAI_AVAILABLE, describe_image_from_bytes


def detect_reply_keywords(text: str, keywords: list) -> Tuple[bool, str]:
    """Detect reply-capture trigger keywords and return user filtering criteria."""
    matched_keyword = ""
    for keyword in keywords:
        if keyword in text:
            matched_keyword = keyword
            break
    if not matched_keyword:
        return False, ""
    remainder = re.sub(r"https?://\S+", "", text).replace(matched_keyword, "")
    remainder = re.sub(r"[\s,.;:!?]+", " ", remainder).strip()
    return True, remainder


def extract_tweet_id(url: str) -> Optional[str]:
    match = re.search(r"(?:twitter\.com|x\.com)/\w+/status/(\d+)", url)
    return match.group(1) if match else None
from metrics import Metrics, Timer


# === 對話歷史 ===
# v3.0 後此類僅作 /fetch /extract 等指令的「最近一條」參考用，
# 不再是 Claude 上下文的來源（context 由 SDK session 自管）。
=======
from url_fetchers import (
    detect_urls, preprocess_urls, save_fetch_output, extract_structured_data,
    REQUESTS_AVAILABLE, YTDLP_AVAILABLE, LANGEXTRACT_AVAILABLE,
)
from vision import GENAI_AVAILABLE


# === Conversation History ===


@dataclass
class Message:
    role: str
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

class ConversationHistory:
    def __init__(self, max_rounds: int = 10):
        self.max_messages = max_rounds * 2
        self.messages: List[Message] = []

    def add_user_message(self, content: str) -> None:
        self.messages.append(Message(role="user", content=content))
        self._trim()

    def add_assistant_message(self, content: str) -> None:
        self.messages.append(Message(role="assistant", content=content))
        self._trim()

    def _trim(self) -> None:
        while len(self.messages) > self.max_messages:
            self.messages.pop(0)


    def clear(self) -> None:
        self.messages.clear()
        logger.info("本地參考歷史已清空")
=======
    def get_context_summary(self) -> str:
        if not self.messages:
            return ""
        lines = ["=== Conversation History ==="]
        for i, msg in enumerate(self.messages):
            prefix = "User" if msg.role == "user" else "Claude"
            preview = msg.content[:500] + "..." if len(msg.content) > 500 else msg.content
            lines.append(f"[{i+1}] {prefix}: {preview}")
        lines.append("=== Current Command ===")
        return "\n".join(lines)

    def clear(self) -> None:
        self.messages.clear()
        logger.info("Conversation history cleared")


    def save(self, filepath: Path) -> None:
        try:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            data = {"messages": [asdict(m) for m in self.messages], "saved_at": datetime.now().isoformat()}
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:

            logger.error(f"保存歷史失敗: {e}")
=======
            logger.error(f"Failed to save history: {e}")

    @classmethod
    def load(cls, filepath: Path, max_rounds: int = 10):
        history = cls(max_rounds=max_rounds)
        try:
            if filepath.exists():
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                for msg_data in data.get("messages", []):
                    history.messages.append(Message(**msg_data))

                logger.info(f"已載入 {len(history.messages)} 條本地參考歷史")
        except Exception as e:
            logger.error(f"載入歷史失敗: {e}")
        return history


# === Playwright reply capture (v3.1) ===
# Public build is Playwright-first and avoids unmaintained X/Twitter API wrappers.

def _build_playwright_reply_fallback(tweet_url: str, user_criteria: str, reason: str) -> str:
    criteria = f"\nUser filtering criteria: {user_criteria}" if user_criteria else ""
    return f"""

=== X/Twitter reply capture via Playwright ===
Reason: {reason}

Use mcp__playwright__browser_* tools to:
1. Navigate to {tweet_url} using the existing browser session if available.
2. Wait for the page to load; expand more replies up to 3 times if a control is visible.
3. Extract up to 30 useful replies from the DOM, including handle, text, and visible like count.
4. Exclude pure emoji, low-information agreement, ads, and spam.{criteria}
5. Summarize selected replies in clear markdown.
6. Close the browser with mcp__playwright__browser_close unless the user is clearly continuing a browsing task.

If the page is unavailable or login is required, report that clearly.
=== reply capture end ===
"""


# === Session 狀態持久化 ===

def load_session_id() -> Optional[str]:
    """讀取上次 SDK session_id（bridge 重啟後 resume 同一條工作線）"""
    fp = CONFIG["SESSION_STATE_FILE"]
    if not fp.exists():
        return None
    try:
        with open(fp, 'r', encoding='utf-8') as f:
            data = json.load(f)
        sid = data.get("session_id")
        if sid:
            logger.info(f"載入既有 session_id: {sid[:12]}...")
        return sid
    except Exception as e:
        logger.warning(f"無法讀取 session_state.json: {e}")
        return None

def save_session_id(session_id: str) -> None:
    fp = CONFIG["SESSION_STATE_FILE"]
    try:
        data = {
            "session_id": session_id,
            "saved_at": datetime.now().isoformat(),
        }
        with open(fp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"無法寫入 session_state.json: {e}")

def clear_session_state() -> None:
    fp = CONFIG["SESSION_STATE_FILE"]
    if fp.exists():
        try:
            fp.unlink()
            logger.info("session_state.json 已刪除")
        except Exception as e:
            logger.warning(f"刪除 session_state.json 失敗: {e}")


# === 主橋接器 ===
=======
                logger.info(f"Loaded {len(history.messages)} history messages")
        except Exception as e:
            logger.error(f"Failed to load history: {e}")
        return history


# === Main Bridge ===


class ClaudeBridge:
    def __init__(self):
        self.history = ConversationHistory.load(CONFIG["HISTORY_FILE"], CONFIG["MAX_HISTORY_ROUNDS"])
        self.is_busy = False

        self._exec_lock = asyncio.Lock()  # 防止並行 query 進入同一個 SDK client
        self.metrics = Metrics(CONFIG["BASE_DIR"] / "stats.json")
        self.sdk_client: Optional[ClaudeSDKClient] = None
        self.current_session_id: Optional[str] = load_session_id()
        # SDK 必須跑在 ProactorEventLoop（Windows 上 SelectorEventLoop 不支援 async subprocess），
        # 而 python-telegram-bot 22.x 在 Windows 強制 SelectorEventLoop，
        # 所以把 SDK 隔離到獨立 thread + 獨立 Proactor loop。
        self._sdk_loop: Optional[asyncio.AbstractEventLoop] = None
        self._sdk_thread: Optional[threading.Thread] = None
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None  # PTB 主 loop，由 start_sdk 設定
        # Playwright 閒置追蹤（v3.0 改善 3）
        self._last_browser_activity: float = 0.0  # monotonic 時間戳；0 = 從未用過
        self._idle_watchdog_task: Optional[asyncio.Task] = None
=======

        self.special_commands = {
            "/clear": self._cmd_clear,
            "/history": self._cmd_show_history,
            "/help": self._cmd_help,
            "/status": self._cmd_status,

            "/stats": self._cmd_stats,
            "/session": self._cmd_session,
            "/interrupt": self._cmd_interrupt,
            "/browser-close": self._cmd_browser_close,
            "/keyboard": self._cmd_keyboard,
            # v3.1 已廢除：/fetch /extract
            #   - /fetch：常駐 Claude 自己會用 WebFetch / Playwright 抓，url_fetchers 預處理已涵蓋
            #   - /extract：Claude 自己整理結構化資料更靈活，LangExtract 模組保留但不暴露指令
=======
            "/extract": self._cmd_extract,
            "/fetch": self._cmd_fetch,

        }

    def is_authorized(self, user_id: int) -> bool:
        if not CONFIG["ALLOWED_USER_IDS"]:
            return True
        return user_id in CONFIG["ALLOWED_USER_IDS"]


    # --- SDK 生命週期 ---

    def _build_options(self, resume_id: Optional[str] = None) -> ClaudeAgentOptions:
        cli_path = CONFIG.get("CLAUDE_CLI_PATH")
        if cli_path and not Path(cli_path).exists():
            logger.warning(f"指定的 claude CLI 不存在: {cli_path}，將回退到 SDK bundled 版本（可能未登入）")
            cli_path = None

        def _cli_stderr(line: str) -> None:
            line = (line or "").rstrip()
            if line:
                logger.error(f"[CLI stderr] {line}")

        return ClaudeAgentOptions(
            cwd=str(CONFIG["WORKING_DIR"]),
            permission_mode=CONFIG["SDK_PERMISSION_MODE"],
            setting_sources=CONFIG["SDK_SETTING_SOURCES"],
            skills=CONFIG["SDK_SKILLS"],
            resume=resume_id,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                # 引導 Claude 在單次瀏覽任務結束時主動關閉瀏覽器，配合 v3.0 的閒置 watchdog
                # 雙重壓低 Chrome 進程的閒置 CPU/風扇佔用
                "append": (
                    "When you finish a self-contained browser task (single screenshot, "
                    "one-shot Q&A on a website, single page extraction), call "
                    "mcp__playwright__browser_close at the end to free resources. "
                    "Skip this if the user is in an obvious multi-step browsing session "
                    "(e.g. they explicitly say 'keep the browser open' or are mid-flow).\n\n"
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
                ),
            },
            include_partial_messages=False,
            cli_path=str(cli_path) if cli_path else None,
            stderr=_cli_stderr,
        )

    # --- 跨 loop 派送 ---

    def _ensure_sdk_thread(self) -> None:
        """啟動專屬 SDK thread + ProactorEventLoop（如尚未啟動）。"""
        if self._sdk_thread and self._sdk_thread.is_alive():
            return
        ready = threading.Event()

        def _run():
            # 在 Windows 顯式建立 ProactorEventLoop，確保支援 async subprocess
            if sys.platform == "win32":
                loop = asyncio.ProactorEventLoop()
            else:
                loop = asyncio.new_event_loop()
            self._sdk_loop = loop
            asyncio.set_event_loop(loop)
            ready.set()
            try:
                loop.run_forever()
            finally:
                loop.close()

        self._sdk_thread = threading.Thread(target=_run, daemon=True, name="SDKLoop")
        self._sdk_thread.start()
        ready.wait(timeout=10)
        if not self._sdk_loop:
            raise RuntimeError("SDK thread 啟動失敗")
        logger.info(f"SDK 專屬 thread 已啟動（loop={type(self._sdk_loop).__name__}）")

    async def _submit_to_sdk(self, coro) -> any:
        """從主 loop 把 coroutine 派到 SDK loop 執行，await 結果。"""
        if not self._sdk_loop:
            raise RuntimeError("SDK loop 尚未啟動")
        fut = asyncio.run_coroutine_threadsafe(coro, self._sdk_loop)
        return await asyncio.wrap_future(fut)

    # --- SDK 生命週期 ---

    async def start_sdk(self) -> None:
        """啟動常駐 Claude SDK client。bridge 啟動時呼叫一次。"""
        if not SDK_AVAILABLE:
            raise RuntimeError("claude-agent-sdk 未安裝。請執行 pip install claude-agent-sdk")
        self._main_loop = asyncio.get_running_loop()
        self._ensure_sdk_thread()
        await self._submit_to_sdk(self._sdk_connect_impl(self.current_session_id))
        # v3.0 改善 3b：啟動閒置 watchdog（在主 loop 上跑，不打擾 SDK loop）
        if self._idle_watchdog_task is None or self._idle_watchdog_task.done():
            self._idle_watchdog_task = asyncio.create_task(self._idle_watchdog_loop())

    async def _sdk_connect_impl(self, resume_id: Optional[str]) -> None:
        """跑在 SDK thread。"""
        options = self._build_options(resume_id=resume_id)
        self.sdk_client = ClaudeSDKClient(options=options)
        try:
            await self.sdk_client.connect()
            logger.info(f"Claude SDK 已連線（resume={resume_id[:12] + '...' if resume_id else 'new session'}）")
        except Exception as e:
            import traceback
            logger.error(f"SDK 連線失敗（resume_id={resume_id}）: {type(e).__name__}: {e}")
            logger.error(f"Traceback:\n{traceback.format_exc()}")
            if resume_id:
                logger.info("嘗試以新 session 重新連線...")
                self.current_session_id = None
                clear_session_state()
                options = self._build_options(resume_id=None)
                self.sdk_client = ClaudeSDKClient(options=options)
                try:
                    await self.sdk_client.connect()
                    logger.info("Claude SDK 已連線（new session）")
                except Exception as e2:
                    logger.error(f"新 session 連線也失敗: {type(e2).__name__}: {e2}")
                    logger.error(f"Traceback:\n{traceback.format_exc()}")
                    raise
            else:
                raise

    async def stop_sdk(self) -> None:
        # 先停 watchdog 避免關閉過程中還在試圖派任務
        if self._idle_watchdog_task and not self._idle_watchdog_task.done():
            self._idle_watchdog_task.cancel()
            try:
                await self._idle_watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.sdk_client and self._sdk_loop:
            try:
                await self._submit_to_sdk(self._sdk_disconnect_impl())
                logger.info("Claude SDK 已斷線")
            except Exception as e:
                logger.warning(f"SDK 斷線時錯誤: {e}")
        if self._sdk_loop and self._sdk_loop.is_running():
            self._sdk_loop.call_soon_threadsafe(self._sdk_loop.stop)

    async def _sdk_disconnect_impl(self) -> None:
        if self.sdk_client:
            await self.sdk_client.disconnect()
            self.sdk_client = None

    async def restart_sdk_with_new_session(self) -> None:
        """/clear 用：丟棄當前 session，啟動全新 session。"""
        if self.sdk_client:
            try:
                await self._submit_to_sdk(self._sdk_disconnect_impl())
            except Exception as e:
                logger.warning(f"舊 session disconnect 失敗（將忽略）: {e}")
        self.current_session_id = None
        clear_session_state()
        await self._submit_to_sdk(self._sdk_connect_impl(resume_id=None))
        logger.info("Claude SDK 已啟動全新 session")

    # --- 指令 ---

    async def _cmd_clear(self, chat_id: int) -> str:
        self.history.clear()
        self.history.save(CONFIG["HISTORY_FILE"])
        try:
            await self.restart_sdk_with_new_session()
            return "已啟動全新 Claude session。先前的 todos / plan mode / 檔案 context 已重置。"
        except Exception as e:
            logger.error(f"重啟 SDK 失敗: {e}")
            return f"清空成功但 SDK 重啟失敗：{e}"

    async def _cmd_show_history(self, chat_id: int) -> str:
        if not self.history.messages:
            return "目前沒有本地參考歷史（注意：v3.0 的 Claude 上下文由 SDK session 自管，本地歷史僅供 /fetch /extract 等指令參考）。"
        lines = [f"本地參考歷史 ({len(self.history.messages)} 條):"]
=======
    async def _cmd_clear(self, chat_id: int) -> str:
        self.history.clear()
        self.history.save(CONFIG["HISTORY_FILE"])
        return "Conversation history cleared. New conversations will not include previous context."

    async def _cmd_show_history(self, chat_id: int) -> str:
        if not self.history.messages:
            return "No conversation history."
        lines = [f"Conversation history ({len(self.history.messages)} messages):"]

        for i, msg in enumerate(self.history.messages):
            prefix = "User" if msg.role == "user" else "Claude"
            preview = msg.content[:100] + "..." if len(msg.content) > 100 else msg.content
            preview = preview.replace('\n', ' ')
            lines.append(f"[{i+1}] {prefix}: {preview}")
        return "\n".join(lines)


    async def _cmd_session(self, chat_id: int) -> str:
        sid = self.current_session_id or "(尚未產生，下次對話後會建立)"
        connected = "✅ 已連線" if self.sdk_client else "❌ 未連線"
        return (
            f"Claude SDK Session 狀態\n"
            f"連線：{connected}\n"
            f"Session ID：{sid}\n"
            f"Working dir：{CONFIG['WORKING_DIR']}\n"
            f"Permission mode：{CONFIG['SDK_PERMISSION_MODE']}\n"
            f"Skills：{CONFIG['SDK_SKILLS']}\n"
            f"Setting sources：{', '.join(CONFIG['SDK_SETTING_SOURCES'])}"
        )

    async def _cmd_interrupt(self, chat_id: int) -> str:
        if not self.sdk_client:
            return "SDK 未連線。"
        try:
            await self._submit_to_sdk(self._interrupt_only())
            return "已送出中斷訊號（Claude 會在當前 tool 結束後停止）。"
        except Exception as e:
            return f"中斷失敗：{e}"

    async def _interrupt_only(self) -> None:
        if self.sdk_client:
            await self.sdk_client.interrupt()

    async def _cmd_browser_close(self, chat_id: int) -> str:
        """v3.0 改善 3a：請 Claude 關閉所有 Playwright 瀏覽器以節省資源。"""
        if not self.sdk_client:
            return "SDK 未連線。"
        prompt = (
            "Please call mcp__playwright__browser_close to close all open Playwright "
            "browsers right now to free CPU/memory. Reply with one short Chinese sentence "
            "confirming success or stating no browser was open."
        )
        try:
            # 經 execute_claude 走 lock 檢查，避免與其他 query 並行
            resp = await self.execute_claude(prompt, progress_cb=None, photo_cb=None)
            self._last_browser_activity = 0.0  # 重置追蹤
            return f"🛑 已請 Claude 關閉瀏覽器：\n{resp}"
        except Exception as e:
            return f"關閉失敗：{e}"

    async def _cmd_keyboard(self, chat_id: int) -> str:
        """v3.0 改善 2：用回應觸發 message_handler 重附鍵盤。"""
        return "⌨️ 鍵盤已重新顯示。"

    # --- v3.0 改善 3b：閒置 watchdog ---

    async def _idle_watchdog_loop(self) -> None:
        """每 N 秒檢查一次：若 playwright 閒置超過閾值，背景關閉瀏覽器。"""
        check_interval = CONFIG.get("BROWSER_IDLE_CHECK_SEC", 60)
        idle_min = CONFIG.get("BROWSER_IDLE_CLOSE_MIN", 5)
        threshold_sec = idle_min * 60
        logger.info(f"[idle] watchdog 啟動：每 {check_interval}s 檢查，閾值 {idle_min} 分鐘")
        while True:
            try:
                await asyncio.sleep(check_interval)
                if self._last_browser_activity <= 0:
                    continue  # 從未用過 playwright，跳過
                if self._exec_lock.locked():
                    continue  # 正在執行任務中，不打擾
                now = asyncio.get_running_loop().time()
                idle_for = now - self._last_browser_activity
                if idle_for >= threshold_sec:
                    logger.info(f"[idle] Playwright 閒置 {idle_for:.0f}s ≥ {threshold_sec}s，自動關閉")
                    try:
                        await self.execute_claude(
                            "Silently call mcp__playwright__browser_close. "
                            "Reply with just the word 'closed' or nothing.",
                            progress_cb=None,
                            photo_cb=None,
                        )
                        self._last_browser_activity = 0.0
                        logger.info("[idle] 自動關閉 Playwright browser 完成")
                    except Exception as e:
                        logger.warning(f"[idle] 自動關閉失敗: {e}")
            except asyncio.CancelledError:
                logger.info("[idle] watchdog 結束")
                return
            except Exception as e:
                logger.warning(f"[idle] watchdog 異常: {e}")

    async def _cmd_help(self, chat_id: int) -> str:
        url_status = []
        url_status.append(f"  fxtwitter (X/Twitter): {'✅ 可用' if REQUESTS_AVAILABLE else '❌ 需要 requests'}")
        url_status.append(f"  yt-dlp (YouTube/通用): {'✅ 可用' if YTDLP_AVAILABLE else '❌ 未安裝'}")
        url_status.append(f"  HTTP fallback: {'✅ 可用' if REQUESTS_AVAILABLE else '❌ 需要 requests'}")
=======
    async def _cmd_help(self, chat_id: int) -> str:
        url_status = []
        url_status.append(f"  fxtwitter (X/Twitter): {'✅' if REQUESTS_AVAILABLE else '❌ needs requests'}")
        url_status.append(f"  yt-dlp (YouTube/general): {'✅' if YTDLP_AVAILABLE else '❌ not installed'}")
        url_status.append(f"  HTTP fallback: {'✅' if REQUESTS_AVAILABLE else '❌ needs requests'}")

        url_block = "\n".join(url_status)

        img_enabled = CONFIG.get("IMAGE_ANALYSIS_ENABLED", False)
        if img_enabled and GENAI_AVAILABLE:

            img_status = f"✅ 啟用（Gemini，最多 {CONFIG['MAX_IMAGES_PER_MESSAGE']} 張/訊息）"
        elif img_enabled:
            img_status = "⚠️ 設定啟用但 Gemini 不可用"
        else:
            img_status = "❌ 停用"

        return f"""{VERSION_LABEL} commands

Persistent Claude Agent SDK session with URL preprocessing, Playwright MCP support,
Telegram screenshot return, and optional Obsidian markdown capture.

Special commands:
/clear - Start a fresh Claude session
/session - Show current SDK session state
/interrupt - Interrupt the current Claude task
/browser-close - Close Playwright browsers to free resources
/keyboard - Re-send the Telegram shortcut keyboard
/history - Show local reference history
/status - Show system status
/stats - Show usage metrics
/help - Show this help message

Shortcuts:
/ps /cclog /tasklog /bridge /uptime

v3.1 behavior:
• /fetch and /extract are removed; send URLs directly
• General articles use trafilatura before HTTP fallback
• X/Twitter reply capture is Playwright-first
• Playwright screenshots are sent to Telegram and saved into Obsidian notes

URL preprocessing:
- X/Twitter -> fxtwitter API -> yt-dlp -> HTTP fallback
- YouTube -> yt-dlp
- GitHub -> Raw/API README fetch
- General web -> trafilatura -> HTTP fallback -> LangExtract when available

Reply triggers: {', '.join(CONFIG.get('REPLY_KEYWORDS', []))}

URL handler status:
{url_block}

Image analysis: {img_status}
"""

    # _cmd_fetch / _cmd_extract 已於 v3.1 移除：
    #   常駐 Claude SDK + Playwright MCP 後，使用者直接傳 URL 即可，
    #   url_fetchers 預處理層會自動抓取，Claude 自己會用 WebFetch / Playwright 補充。
    #   結構化萃取也由 Claude 直接做，無需專用指令。
    #   url_fetchers.extract_structured_data / save_fetch_output 仍在 handle_message 中自動使用。

    async def _cmd_status(self, chat_id: int) -> str:
        log_files = list(CONFIG["LOG_DIR"].glob("bridge.log*"))
        status = "忙碌中" if self.is_busy else "待命"
        sdk_status = "✅ 已連線" if self.sdk_client else "❌ 未連線"
        sid = (self.current_session_id[:12] + "...") if self.current_session_id else "(尚未建立)"

        if CONFIG.get("IMAGE_ANALYSIS_ENABLED"):
            img_status = f"✅ 啟用（最多 {CONFIG['MAX_IMAGES_PER_MESSAGE']} 張/訊息）" if GENAI_AVAILABLE else "⚠️ 啟用但 Gemini 不可用"
        else:
            img_status = "❌ 停用"

        return f"""系統狀態 (v{VERSION})
SDK 連線: {sdk_status}
Session ID: {sid}
Permission: {CONFIG['SDK_PERMISSION_MODE']}
本地參考歷史: {len(self.history.messages)} 條
工作目錄: {CONFIG['WORKING_DIR']}
Log 目錄: {CONFIG['LOG_DIR']}
Log 檔案數: {len(log_files)}
Claude 狀態: {status}

URL 處理器:
  fxtwitter: {'✅' if REQUESTS_AVAILABLE else '❌'}
  yt-dlp: {'✅' if YTDLP_AVAILABLE else '❌'}

📷 圖片分析: {img_status}
💬 回覆抓取: ✅ Playwright-first

當前時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""

    async def _cmd_stats(self, chat_id: int) -> str:
        return self.metrics.get_summary()

    # --- Claude 執行（v3.0 SDK 版）---

    async def execute_claude(
        self,
        prompt: str,
        progress_cb: Optional[callable] = None,
        photo_cb: Optional[callable] = None,
        screenshot_collector: Optional[List[Tuple[bytes, str]]] = None,
    ) -> str:
        """
        透過常駐 SDK client 執行 Claude 任務。
        - progress_cb(text)：tool use 事件 → 更新 Telegram 進度訊息
        - photo_cb(bytes, media_type)：tool result 含圖片 → 推播到 Telegram
        - screenshot_collector：若提供，會將擷取到的截圖 (bytes, media_type) 累積進此 list
          供呼叫端後續使用（如寫入 Obsidian vault）
        """
        if self._exec_lock.locked():
            return "Claude 正在處理另一個任務，請稍後再試（或使用 /interrupt 中斷）..."
        if not self.sdk_client:
            return "SDK 未連線。請檢查 bridge log。"

        async with self._exec_lock:
            return await self._execute_claude_locked(prompt, progress_cb, photo_cb, screenshot_collector)

    async def _execute_claude_locked(
        self,
        prompt: str,
        progress_cb: Optional[callable],
        photo_cb: Optional[callable] = None,
        screenshot_collector: Optional[List[Tuple[bytes, str]]] = None,
    ) -> str:
        self.is_busy = True
        timer = Timer().start()
        text_chunks: List[str] = []

        # progress_cb / photo_cb 都綁在主 loop 上（PTB 的 reply_text / send_photo），
        # 從 SDK thread 派回主 loop 執行
        main_loop = self._main_loop

        def _proxy_progress(text: str):
            if not progress_cb or not main_loop:
                return
            try:
                asyncio.run_coroutine_threadsafe(progress_cb(text), main_loop)
            except Exception as cb_e:
                logger.debug(f"progress_cb 派送失敗: {cb_e}")

        def _proxy_photo(data: bytes, media_type: str):
            if not photo_cb or not main_loop:
                return
            try:
                asyncio.run_coroutine_threadsafe(photo_cb(data, media_type), main_loop)
            except Exception as cb_e:
                logger.debug(f"photo_cb 派送失敗: {cb_e}")

        try:
            logger.info(f"[SDK] 送出 query: {prompt[:120]}...")
            await asyncio.wait_for(
                self._submit_to_sdk(self._run_query_on_sdk(
                    prompt, text_chunks, _proxy_progress, _proxy_photo, screenshot_collector
                )),
                timeout=CONFIG["TIMEOUT"],
            )

            full_text = "".join(text_chunks).strip() or "(任務完成，無文字輸出)"
            self.metrics.record_claude_call(timer.elapsed(), success=True)
            return self._format_output(full_text)

        except asyncio.TimeoutError:
            self.metrics.record_claude_call(timer.elapsed(), success=False)
            self.metrics.record_error("claude", f"timeout ({CONFIG['TIMEOUT']}s)")
            try:
                if self.sdk_client:
                    await self._submit_to_sdk(self._interrupt_and_drain())
            except Exception as e:
                logger.warning(f"interrupt/drain 失敗: {e}")
            partial = "".join(text_chunks).strip()
            return f"執行超時（{CONFIG['TIMEOUT']}秒）。已送出 interrupt。\n\n部分輸出：\n{partial}" if partial else f"執行超時（{CONFIG['TIMEOUT']}秒）"
        except Exception as e:
            import traceback
            logger.error(f"Claude SDK 執行錯誤：{type(e).__name__}: {e}")
            logger.error(f"Traceback:\n{traceback.format_exc()}")
            self.metrics.record_claude_call(timer.elapsed(), success=False)
            self.metrics.record_error("claude", f"{type(e).__name__}: {e}")
            partial = "".join(text_chunks).strip()
            err_line = f"執行錯誤：{type(e).__name__}: {str(e)}"
            return (partial + "\n\n---\n" + err_line) if partial else err_line
        finally:
            self.is_busy = False

    async def _run_query_on_sdk(
        self,
        prompt: str,
        text_chunks: List[str],
        proxy_progress,
        proxy_photo=None,
        screenshot_collector: Optional[List[Tuple[bytes, str]]] = None,
    ) -> None:
        """跑在 SDK thread。"""
        import base64
        await self.sdk_client.query(prompt)
        async for msg in self.sdk_client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_chunks.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        hint = self._summarize_tool_use(block)
                        logger.info(f"[SDK] tool_use: {hint}")
                        proxy_progress(f"🔧 {hint}")
                        # Playwright 活動偵測（v3.0 改善 3：閒置 watchdog）
                        if block.name and block.name.startswith("mcp__playwright__"):
                            self._last_browser_activity = asyncio.get_running_loop().time()
                    elif isinstance(block, ThinkingBlock):
                        logger.debug("[SDK] thinking block")
            elif isinstance(msg, UserMessage):
                # tool result 在 UserMessage.content 中（SDK 把 tool result 視為 user 角色）
                if msg.content:
                    proxy_progress("🔧 tool 完成，繼續處理...")
                    # v3.0 改善 1：擷取 ToolResultBlock 裡的圖片，推給 Telegram
                    if isinstance(msg.content, list) and proxy_photo:
                        for blk in msg.content:
                            if isinstance(blk, ToolResultBlock) and isinstance(blk.content, list):
                                for item in blk.content:
                                    if isinstance(item, dict) and item.get("type") == "image":
                                        src = item.get("source", {})
                                        if src.get("type") == "base64":
                                            try:
                                                data = base64.b64decode(src.get("data", ""))
                                                media_type = src.get("media_type", "image/png")
                                                logger.info(f"[SDK] 偵測到截圖 {len(data)} bytes ({media_type})")
                                                proxy_photo(data, media_type)
                                                # v3.1：同步收集供 Obsidian 落地
                                                if screenshot_collector is not None:
                                                    screenshot_collector.append((data, media_type))
                                            except Exception as e:
                                                logger.warning(f"圖片 decode 失敗: {e}")
            elif isinstance(msg, ResultMessage):
                if msg.session_id and msg.session_id != self.current_session_id:
                    self.current_session_id = msg.session_id
                    save_session_id(msg.session_id)
                    logger.info(f"[SDK] session_id 更新: {msg.session_id[:12]}...")
                if msg.is_error:
                    err = msg.result or "(unknown error)"
                    logger.error(f"[SDK] ResultMessage error: {err}")
                    text_chunks.append(f"\n\n[Claude 回報錯誤: {err}]")
                if msg.total_cost_usd is not None:
                    logger.info(f"[SDK] 成本 USD={msg.total_cost_usd:.4f}, turns={msg.num_turns}, dur={msg.duration_ms}ms")

    async def _interrupt_and_drain(self) -> None:
        """跑在 SDK thread：中斷 + 排空殘留訊息。"""
        if not self.sdk_client:
            return
        try:
            await self.sdk_client.interrupt()
        except Exception as e:
            logger.warning(f"interrupt 失敗: {e}")
        try:
            await asyncio.wait_for(self._drain_response_inner(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning("interrupt 後 drain 也超時，client 可能需要重連")

    async def _drain_response_inner(self) -> None:
        if not self.sdk_client:
            return
        async for msg in self.sdk_client.receive_response():
            if isinstance(msg, ResultMessage):
                if msg.session_id and msg.session_id != self.current_session_id:
                    self.current_session_id = msg.session_id
                    save_session_id(msg.session_id)
                logger.info(f"[SDK] drain 完成 (subtype={msg.subtype})")
                break

    @staticmethod
    def _summarize_tool_use(block) -> str:
        name = block.name
        inp = block.input or {}
        # 友善顯示常見 tool
        if name == "Bash":
            cmd = (inp.get("command") or "")[:80]
            return f"Bash: {cmd}"
        if name in ("Read", "Edit", "Write"):
            path = inp.get("file_path") or inp.get("path") or ""
            return f"{name}: {path}"
        if name == "Glob":
            return f"Glob: {inp.get('pattern','')}"
        if name == "Grep":
            return f"Grep: {inp.get('pattern','')}"
        if name == "WebFetch":
            return f"WebFetch: {inp.get('url','')}"
        if name == "WebSearch":
            return f"WebSearch: {inp.get('query','')}"
        if name == "Task":
            return f"Task(subagent): {inp.get('description','')}"
        if name.startswith("mcp__"):
            return f"MCP {name}"
        return f"Tool: {name}"

    def _format_output(self, output: str) -> str:
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        output = ansi_escape.sub('', output)
        if len(output) > 8000:
            output = output[:8000] + "\n\n...(輸出已截斷)"
        return output

    # --- 主訊息流程 ---

    async def handle_message(
        self,
        chat_id: int,
        text: str,
        progress_cb: Optional[callable] = None,
        photo_cb: Optional[callable] = None,
    ) -> Tuple[str, Optional[str]]:
        text = text.strip()
        cmd = text.split()[0].lower() if text else ""
        if cmd in self.special_commands:
            logger.info(f"收到指令 (chat_id={chat_id}): {cmd}")
            return await self.special_commands[cmd](chat_id), None

        logger.info(f"收到訊息 (chat_id={chat_id}): {text[:100]}...")
        self.metrics.record_message()

        # === URL 預處理 ===
        enhanced_text, url_summaries, obsidian_queue = await preprocess_urls(
            text, config=CONFIG, metrics=self.metrics
        )

        url_status = None
        if url_summaries:
            url_status = "🔗 URL 處理結果:\n" + "\n".join(url_summaries)
            logger.info(f"URL 預處理完成: {url_summaries}")

        # === Reply capture ===
        reply_triggered, user_criteria = detect_reply_keywords(
            text, CONFIG.get("REPLY_KEYWORDS", [])
        )
        replies_section = ""
        replies_prompt_block = ""

        if reply_triggered:
            detected = detect_urls(text)
            twitter_urls = [(u, p) for u, p in detected if p == "x_twitter"]
            if twitter_urls:
                tweet_url = twitter_urls[0][0]
                tweet_id = extract_tweet_id(tweet_url)
                reason = "Playwright-first reply capture"
                if tweet_id:
                    logger.info(f"[reply] Playwright fallback requested: tweet={tweet_id}, criteria='{user_criteria}'")
                replies_prompt_block = _build_playwright_reply_fallback(
                    tweet_url, user_criteria, reason
                )
                note = "Reply capture: Playwright-first"
            else:
                note = "Reply keyword detected, but no X/Twitter URL was found"
            url_status = (url_status + "\n" + note) if url_status else note

        if replies_prompt_block:
            enhanced_text += replies_prompt_block

        self.history.add_user_message(text)
        # v3.1：收集 Playwright 截圖供 Obsidian 一併落地
        screenshot_collector: List[Tuple[bytes, str]] = []
        response = await self.execute_claude(
            enhanced_text, progress_cb=progress_cb, photo_cb=photo_cb,
            screenshot_collector=screenshot_collector,
        )

=======
            img_status = f"✅ Enabled (Gemini 2.0 Flash, max {CONFIG['MAX_IMAGES_PER_MESSAGE']} images/msg)"
        elif img_enabled:
            img_status = "⚠️ Enabled but Gemini unavailable"
        else:
            img_status = "❌ Disabled"

        return f"""Telegram Claude Bridge v2.6

Commands:
/clear - Clear conversation history
/history - Show conversation history summary
/status - Show system status
/help - Show this help message
/exec <cmd> - Execute a shell command directly

Usage:
Send any message to chat with Claude Code.
The system automatically keeps the last {CONFIG['MAX_HISTORY_ROUNDS']} conversation rounds as context.

🔗 URL Auto-Processing:
Share any link and the system will auto-fetch content for Claude:
- X/Twitter → fxtwitter API → yt-dlp (fallback)
- YouTube → yt-dlp
- Other sites → HTTP title/description extraction

📷 Image Analysis:
Tweet images are auto-downloaded and analyzed via Gemini Vision:
- Auto-recognize charts, text, infographics, and other visual content
- GIF thumbnails are extracted for analysis
- Twitter Articles (long-form Notes) are fully parsed
- Max {CONFIG['MAX_IMAGES_PER_MESSAGE']} images per message

URL Processors:
{url_block}

📷 Image Analysis: {img_status}

Log Management:
- Daily independent log files
- Auto-cleanup after {CONFIG['LOG_RETENTION_DAYS']} days
"""

    async def _cmd_fetch(self, chat_id: int) -> str:
        if not self.history.messages:
            return "No messages. Usage: /fetch <URL> [notes]"
        last_user = None
        for msg in reversed(self.history.messages):
            if msg.role == "user":
                last_user = msg.content
                break
        if not last_user:
            return "No user message found."
        urls = detect_urls(last_user)
        if not urls:
            return "No URL found in last message."
        url = urls[0][0]
        user_note = last_user.replace(url, "").replace("/fetch", "").strip()
        enhanced_text, summaries = await preprocess_urls(url, config=CONFIG)
        fetched = enhanced_text if enhanced_text != url else "Could not fetch"
        fetch_prompt = "URL content:" + chr(10) + fetched + chr(10) + chr(10)
        if user_note:
            fetch_prompt += "User task: " + user_note + chr(10) + chr(10)
        fetch_prompt += "Provide comprehensive analysis. Structure clearly."
        response = await self.execute_claude(fetch_prompt)
        saved = await asyncio.get_event_loop().run_in_executor(
            None, save_fetch_output, url, fetched, response, user_note, CONFIG
        )
        if saved:
            return response + chr(10) + chr(10) + "---" + chr(10) + "Saved: " + saved
        return response

    async def _cmd_extract(self, chat_id: int) -> str:
        if not self.history.messages:
            return "No history to extract."
        last_assistant = None
        for msg in reversed(self.history.messages):
            if msg.role == "assistant":
                last_assistant = msg.content
                break
        if not last_assistant:
            return "No assistant reply found."
        result = await asyncio.get_event_loop().run_in_executor(None, extract_structured_data, last_assistant)
        return result or "No extraction result"

    async def _cmd_status(self, chat_id: int) -> str:
        log_files = list(CONFIG["LOG_DIR"].glob("bridge.log*"))
        status = "Busy" if self.is_busy else "Ready"

        if CONFIG.get("IMAGE_ANALYSIS_ENABLED"):
            if GENAI_AVAILABLE:
                img_status = f"✅ Enabled (max {CONFIG['MAX_IMAGES_PER_MESSAGE']} images/msg)"
            else:
                img_status = "⚠️ Enabled but Gemini unavailable"
        else:
            img_status = "❌ Disabled"

        return f"""System Status (v2.6)
History messages: {len(self.history.messages)}
History file: {CONFIG['HISTORY_FILE']}
Max history rounds: {CONFIG['MAX_HISTORY_ROUNDS']}
Working directory: {CONFIG['WORKING_DIR']}
Log directory: {CONFIG['LOG_DIR']}
Log files: {len(log_files)}
Log retention: {CONFIG['LOG_RETENTION_DAYS']} days
Claude status: {status}

URL Processors:
  fxtwitter: {'✅' if REQUESTS_AVAILABLE else '❌'}
  yt-dlp: {'✅' if YTDLP_AVAILABLE else '❌'}
  HTTP fallback: {'✅' if REQUESTS_AVAILABLE else '❌'}

📷 Image Analysis: {img_status}

Current time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""

    def _build_prompt_with_context(self, user_message: str) -> str:
        context = self.history.get_context_summary()
        safety_note = "Safety: Do not delete important files or modify system settings." if not CONFIG["ALLOW_DANGEROUS"] else ""
        if context:
            return f"{context}\n{user_message}\n\n{safety_note}\nRespond based on the conversation context above."
        return f"{user_message}\n\n{safety_note}"

    async def execute_claude(self, prompt: str) -> str:
        if self.is_busy:
            return "Claude is processing another task, please wait..."
        self.is_busy = True
        try:
            full_prompt = self._build_prompt_with_context(prompt)
            logger.info(f"Executing Claude: {prompt[:100]}...")
            process = await asyncio.create_subprocess_shell(
                f'"{CONFIG["CLAUDE_CLI"]}" --print --dangerously-skip-permissions',
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(CONFIG["WORKING_DIR"])
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=full_prompt.encode('utf-8')),
                timeout=CONFIG["TIMEOUT"]
            )
            output = stdout.decode('utf-8', errors='replace')
            error = stderr.decode('utf-8', errors='replace')
            if error and not output:
                result = f"Error:\n{error}"
            elif output:
                result = self._format_output(output)
            else:
                result = "Task completed (no output)"
            return result
        except asyncio.TimeoutError:
            return f"Execution timeout ({CONFIG['TIMEOUT']}s)"
        except FileNotFoundError:
            return "Claude CLI not found. Please ensure Claude Code is installed."
        except Exception as e:
            logger.error(f"Claude execution error: {e}")
            return f"Execution error: {str(e)}"
        finally:
            self.is_busy = False

    def _format_output(self, output: str) -> str:
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        output = ansi_escape.sub('', output)
        if len(output) > 3500:
            output = output[:3500] + "\n\n...(output truncated)"
        return output

    async def handle_message(self, chat_id: int, text: str) -> Tuple[str, Optional[str]]:
        text = text.strip()
        cmd = text.split()[0].lower() if text else ""
        if cmd in self.special_commands:
            logger.info(f"Command received (chat_id={chat_id}): {cmd}")
            return await self.special_commands[cmd](chat_id), None

        logger.info(f"Message received (chat_id={chat_id}): {text[:100]}...")

        # URL preprocessing
        enhanced_text, url_summaries = await preprocess_urls(text, config=CONFIG)

        url_status = None
        if url_summaries:
            url_status = "🔗 URL processing:\n" + "\n".join(url_summaries)
            logger.info(f"URL preprocessing done: {url_summaries}")

        self.history.add_user_message(text)
        response = await self.execute_claude(enhanced_text)

        # Auto-save fetch output when URLs present

        if url_summaries:
            detected = detect_urls(text)
            if detected:
                fetch_url = detected[0][0]
                user_note = text.replace(fetch_url, "").strip()

                await asyncio.get_running_loop().run_in_executor(
                    None, save_fetch_output, fetch_url, enhanced_text, response, user_note, CONFIG
                )

        if obsidian_queue:
            for ob_url, ob_content, ob_meta in obsidian_queue:
                saved_path = await asyncio.get_running_loop().run_in_executor(
                    None, save_to_obsidian, ob_url, ob_content, response,
                    ob_meta, CONFIG, replies_section, screenshot_collector,
                )
                if saved_path:
                    self.metrics.record_obsidian_save()

=======
                await asyncio.get_event_loop().run_in_executor(
                    None, save_fetch_output, fetch_url, enhanced_text, response, user_note, CONFIG
                )

        self.history.add_assistant_message(response)
        self.history.save(CONFIG["HISTORY_FILE"])

        return response, url_status


# === Telegram Handlers ===


bridge: Optional[ClaudeBridge] = None


def make_progress_cb(processing_msg, interval: float = 1.5):
    """
    回傳一個 async 進度回呼函式，用 closure 維護「上次更新時間」做節流。
    避免 Telegram rate limit。
    """
    state = {"last_update": 0.0, "last_text": ""}
    async def cb(text: str):
        now = asyncio.get_running_loop().time()
        if text == state["last_text"]:
            return
        if now - state["last_update"] < interval:
            return
        state["last_update"] = now
        state["last_text"] = text
        try:
            await processing_msg.edit_text(text[:500])
        except Exception:
            pass
    return cb


def make_photo_cb(reply_target):
    """
    v3.0 改善 1：把 SDK 收到的截圖即時 push 給 Telegram。
    reply_target 是 update.message（或任何有 reply_photo / reply_document 的物件）。
    """
    from io import BytesIO
    max_bytes = CONFIG.get("TG_MAX_PHOTO_BYTES", 10 * 1024 * 1024)

    async def cb(data: bytes, media_type: str = "image/png"):
        try:
            ext = "png"
            if "jpeg" in media_type or "jpg" in media_type:
                ext = "jpg"
            elif "webp" in media_type:
                ext = "webp"
            buf = BytesIO(data)
            buf.name = f"screenshot.{ext}"
            if len(data) > max_bytes:
                # 超過 photo 限制 → 改用 document
                await reply_target.reply_document(document=buf, filename=buf.name,
                    caption=f"📷 截圖（{len(data)//1024} KB，超過 photo 限制改傳檔案）")
            else:
                await reply_target.reply_photo(photo=buf)
        except Exception as e:
            logger.warning(f"[photo_cb] 推送失敗: {e}")
    return cb


# v3.0 improvement: persistent 4 x 3 shortcut keyboard.
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("/help"),    KeyboardButton("/status"),    KeyboardButton("/session")],
        [KeyboardButton("/clear"),   KeyboardButton("/stats"),     KeyboardButton("/interrupt")],
        [KeyboardButton("/ps"),      KeyboardButton("/uptime"),    KeyboardButton("/cclog")],
        [KeyboardButton("/bridge"),  KeyboardButton("/tasklog"),   KeyboardButton("/browser-close")],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="輸入訊息或點下方按鈕...",
)

=======
bridge = None


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not bridge.is_authorized(user.id):

        await update.message.reply_text(f"未授權的用戶\n你的 User ID: {user.id}")
        return

    img_text = "✅ 啟用" if (CONFIG.get("IMAGE_ANALYSIS_ENABLED") and GENAI_AVAILABLE) else "❌ 停用"
    sdk_text = "✅ 已連線（常駐）" if bridge.sdk_client else "❌ 未連線"
    sid_text = (bridge.current_session_id[:12] + "...") if bridge.current_session_id else "(新 session)"

    await update.message.reply_text(
        f"{VERSION_LABEL}\n\n"
        f"歡迎，{user.first_name}！\n\n"
        f"🆕 v3.0：常駐 Claude Agent SDK\n"
        f"  • 跨訊息保留 plan mode / todos / 檔案 context\n"
        f"  • MCP server 常駐（Playwright Chrome / Obsidian 等）\n"
        f"  • 重啟後 resume 同一條工作線\n"
        f"  • 截圖直接傳到 Telegram\n"
        f"  • 5 分鐘閒置自動關 Chrome 省電\n\n"
        f"SDK 狀態: {sdk_text}\n"
        f"Session: {sid_text}\n"
        f"📷 圖片分析: {img_text}\n\n"
        f"輸入 /help 查看所有指令，或點下方按鈕。",
        reply_markup=MAIN_KEYBOARD,
    )


async def _run_ps_shortcut(update, command, timeout=30):
    if not bridge.is_authorized(update.effective_user.id):
        await update.message.reply_text("未授權")
        return
    try:
        result = subprocess.run(
            ["powershell", "-Command", command],
            capture_output=True, text=True, timeout=timeout,
            encoding='utf-8', errors='replace'
        )
        output = result.stdout or result.stderr or "(無輸出)"
        if len(output) > 8000:
            output = output[:8000] + "\n...(已截斷)"
        await update.message.reply_text(output)
    except subprocess.TimeoutExpired:
        await update.message.reply_text("執行超時")
    except Exception as e:
        await update.message.reply_text(f"錯誤：{e}")


async def ps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = r"""
$targets = @('python', 'claude', 'node', 'Claude')
$procs = Get-Process | Where-Object {
    $name = $_.ProcessName
    $targets | ForEach-Object { if ($name -like "*$_*") { $true } }
} | Select-Object ProcessName,
    @{N='CPU(s)';E={[math]::Round($_.CPU,1)}},
    @{N='Mem(MB)';E={[math]::Round($_.WorkingSet64/1MB,1)}},
    @{N='PID';E={$_.Id}},
    @{N='Runtime';E={
        $ts = (Get-Date) - $_.StartTime
        if ($ts.TotalHours -ge 1) { '{0:0}h{1:00}m' -f [math]::Floor($ts.TotalHours), $ts.Minutes }
        else { '{0}m{1:00}s' -f $ts.Minutes, $ts.Seconds }
    }}

if ($procs) {
    Write-Output "📊 關鍵進程狀態："
    Write-Output "─────────────────────────────"
    $procs | Format-Table -AutoSize | Out-String
} else {
    Write-Output "⚠️ 未偵測到關鍵進程"
}

$uptime = (Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
Write-Output ("💻 系統已運行: {0}天 {1}時 {2}分" -f $uptime.Days, $uptime.Hours, $uptime.Minutes)
"""
    await _run_ps_shortcut(update, cmd)


async def cclog_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = 30
    if context.args:
        try:
            n = min(int(context.args[0]), 100)
        except ValueError:
            pass

    cmd = f"""
$logDir = "$env:USERPROFILE\\.claude\\logs"
if (Test-Path $logDir) {{
    $latest = Get-ChildItem $logDir -Filter "*.log" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($latest) {{
        Write-Output "📋 CC Log: $($latest.Name)"
        Write-Output "📅 最後修改: $($latest.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss'))"
        Write-Output "─────────────────────────────"
        Get-Content $latest.FullName -Tail {n} -Encoding UTF8 -ErrorAction SilentlyContinue
    }} else {{
        Write-Output "⚠️ logs 目錄存在但無 .log 檔"
    }}
}} else {{
    Write-Output "⚠️ Claude Code log 目錄不存在: $logDir"
}}
"""
    await _run_ps_shortcut(update, cmd)


async def tasklog_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = r"""
Write-Output "📅 Task Scheduler 近期執行結果："
Write-Output "─────────────────────────────"

$tasks = Get-ScheduledTask | Where-Object { $_.State -ne 'Disabled' } |
    ForEach-Object {
        $info = Get-ScheduledTaskInfo -TaskName $_.TaskName -TaskPath $_.TaskPath -ErrorAction SilentlyContinue
        if ($info -and $info.LastRunTime -gt (Get-Date).AddDays(-3)) {
            [PSCustomObject]@{
                Name     = $_.TaskName
                LastRun  = $info.LastRunTime.ToString('MM-dd HH:mm')
                Result   = if ($info.LastTaskResult -eq 0) { '✅' }
                           elseif ($info.LastTaskResult -eq 267009) { '⏳ 執行中' }
                           else { "❌ ($($info.LastTaskResult))" }
                NextRun  = if ($info.NextRunTime) { $info.NextRunTime.ToString('MM-dd HH:mm') } else { '-' }
            }
        }
    } | Sort-Object LastRun -Descending | Select-Object -First 10

if ($tasks) {
    $tasks | Format-Table -AutoSize | Out-String
} else {
    Write-Output "最近 3 天內沒有排程任務執行紀錄"
}
"""
    await _run_ps_shortcut(update, cmd, timeout=45)


async def bridge_log_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = 30
    if context.args:
        try:
            n = min(int(context.args[0]), 100)
        except ValueError:
            pass

    cmd = f"""
$logDir = "C:\\telegram-MCP-bridge\\logs"
$latest = Get-ChildItem $logDir -Filter "bridge.log*" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($latest) {{
    Write-Output "📋 T-M-B Log: $($latest.Name)"
    Write-Output "📅 最後修改: $($latest.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss'))"
    Write-Output "─────────────────────────────"
    Get-Content $latest.FullName -Tail {n} -Encoding UTF8 -ErrorAction SilentlyContinue
}} else {{
    Write-Output "⚠️ 找不到 T-M-B log 檔案"
}}
"""
    await _run_ps_shortcut(update, cmd)


async def uptime_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = r"""
$tmb = Get-Process python -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*telegram_bridge*' -or $_.CommandLine -like '*bridge*' } |
    Select-Object -First 1

if ($tmb) {
    $runtime = (Get-Date) - $tmb.StartTime
    $startStr = $tmb.StartTime.ToString('yyyy-MM-dd HH:mm:ss')
    Write-Output "🤖 T-M-B 狀態: ✅ 運行中"
    Write-Output ("   啟動時間: " + $startStr)
    Write-Output ("   已運行: {0}天 {1}時 {2}分" -f $runtime.Days, $runtime.Hours, $runtime.Minutes)
    Write-Output ("   記憶體: {0:N0} MB" -f ($tmb.WorkingSet64/1MB))
} else {
    $pyProcs = Get-Process python -ErrorAction SilentlyContinue
    if ($pyProcs) {
        Write-Output "🤖 T-M-B 狀態: ⚠️ 無法精確識別（找到 $($pyProcs.Count) 個 python 進程）"
        $pyProcs | ForEach-Object {
            $rt = (Get-Date) - $_.StartTime
            Write-Output ("   PID $($_.Id): 已運行 {0}天{1}時{2}分, {3:N0}MB" -f $rt.Days, $rt.Hours, $rt.Minutes, ($_.WorkingSet64/1MB))
        }
    } else {
        Write-Output "🤖 T-M-B 狀態: ❌ 未偵測到 python 進程"
    }
}

Write-Output ""

$os = Get-CimInstance Win32_OperatingSystem
$boot = $os.LastBootUpTime
$uptime = (Get-Date) - $boot
Write-Output ("💻 系統開機: " + $boot.ToString('yyyy-MM-dd HH:mm:ss'))
Write-Output ("   已運行: {0}天 {1}時 {2}分" -f $uptime.Days, $uptime.Hours, $uptime.Minutes)
"""
    await _run_ps_shortcut(update, cmd)

=======
        await update.message.reply_text(f"Unauthorized user\nYour User ID: {user.id}")
        return

    url_features = []
    if REQUESTS_AVAILABLE:
        url_features.append("fxtwitter (X/Twitter)")
    if YTDLP_AVAILABLE:
        url_features.append("yt-dlp (YouTube/general)")
    url_text = ", ".join(url_features) if url_features else "Not enabled"

    img_text = "✅ Enabled" if (CONFIG.get("IMAGE_ANALYSIS_ENABLED") and GENAI_AVAILABLE) else "❌ Disabled"

    await update.message.reply_text(
        f"Telegram Claude Code Bridge v2.6\n\n"
        f"Welcome, {user.first_name}!\n\n"
        f"Features:\n"
        f"- Auto-keep last {CONFIG['MAX_HISTORY_ROUNDS']} conversation rounds\n"
        f"- Daily logs, auto-cleanup after {CONFIG['LOG_RETENTION_DAYS']} days\n"
        f"- URL auto-fetch: {url_text}\n"
        f"- 📷 Image analysis: {img_text}\n\n"
        f"Type /help for all commands."
    )

async def exec_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bridge.is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized")
        return
    if not context.args:
        await update.message.reply_text("Usage: /exec <command>")
        return
    command = ' '.join(context.args)
    await update.message.reply_text(f"Executing: {command}")
    try:
        # Cross-platform: use shell=True so it works on both Windows and Linux
        result = subprocess.run(
            command, shell=True,
            capture_output=True, text=True, timeout=60,
            cwd=str(CONFIG["WORKING_DIR"]), encoding='utf-8', errors='replace'
        )
        output = result.stdout or result.stderr or "(no output)"
        if len(output) > 3500:
            output = output[:3500] + "\n...(truncated)"
        status = "Success" if result.returncode == 0 else "Failed"
        await update.message.reply_text(f"{status}:\n{output}")
    except subprocess.TimeoutExpired:
        await update.message.reply_text("Execution timeout")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not bridge.is_authorized(user_id):

        await update.message.reply_text(f"未授權\n你的 User ID: {user_id}")
=======
        await update.message.reply_text(f"Unauthorized\nYour User ID: {user_id}")

        return

    text = update.message.text


    cmd = text.strip().split()[0].lower() if text.strip() else ""
    if cmd in bridge.special_commands:
        result, _ = await bridge.handle_message(update.effective_chat.id, text)
        # /keyboard 需要實際附上 ReplyKeyboardMarkup 才能讓 Telegram 端重繪鍵盤
        if cmd == "/keyboard":
            await update.message.reply_text(result, reply_markup=MAIN_KEYBOARD)
        else:
            await update.message.reply_text(result)
        return

    urls = detect_urls(text)
    reply_kw_hit, _ = detect_reply_keywords(text, CONFIG.get("REPLY_KEYWORDS", []))
    if urls and reply_kw_hit:
        processing_msg = await update.message.reply_text(
            f"🔗💬 偵測到連結 + 回覆抓取，處理中...\n{text[:50]}{'...' if len(text) > 50 else ''}"
        )
    elif urls:
        processing_msg = await update.message.reply_text(
            f"🔗 偵測到連結，正在抓取內容...\n{text[:50]}{'...' if len(text) > 50 else ''}"
        )
    else:
        processing_msg = await update.message.reply_text(
            f"Claude 正在處理...\n{text[:50]}{'...' if len(text) > 50 else ''}"
        )

    progress_cb = make_progress_cb(processing_msg, CONFIG["SDK_PROGRESS_EDIT_INTERVAL"])
    photo_cb = make_photo_cb(update.message)
    result, url_status = await bridge.handle_message(
        update.effective_chat.id, text, progress_cb=progress_cb, photo_cb=photo_cb,
    )
=======
    # Special commands (/clear /help etc.) — fast response, no processing message
    cmd = text.strip().split()[0].lower() if text.strip() else ""
    if cmd in bridge.special_commands:
        result, _ = await bridge.handle_message(update.effective_chat.id, text)
        await update.message.reply_text(result)
        return

    urls = detect_urls(text)
    if urls:
        processing_msg = await update.message.reply_text(
            f"🔗 Link detected, fetching content...\n{text[:50]}{'...' if len(text) > 50 else ''}"
        )
    else:
        processing_msg = await update.message.reply_text(
            f"Claude is processing...\n{text[:50]}{'...' if len(text) > 50 else ''}"
        )

    result, url_status = await bridge.handle_message(update.effective_chat.id, text)

    try:
        await processing_msg.delete()
    except:
        pass

    if url_status:
        await update.message.reply_text(url_status)

    if len(result) > 4000:
        chunks = [result[i:i+4000] for i in range(0, len(result), 4000)]
        for i, chunk in enumerate(chunks):
            await update.message.reply_text(f"[{i+1}/{len(chunks)}]\n\n{chunk}")
    else:

        await update.message.reply_text(f"Claude 回應：\n\n{result}")


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not bridge.is_authorized(user_id):
        await update.message.reply_text(f"未授權\n你的 User ID: {user_id}")
        return

    caption = update.message.caption or ""
    photo = update.message.photo[-1]
    logger.info(f"收到照片 (chat_id={update.effective_chat.id}, file_id={photo.file_id}, caption={caption[:50]})")
    bridge.metrics.record_photo()

    caption_urls = detect_urls(caption) if caption else []
    has_urls = len(caption_urls) > 0

    if has_urls:
        processing_msg = await update.message.reply_text(
            f"📷🔗 收到照片（含 {len(caption_urls)} 個連結），正在處理..."
        )
    else:
        processing_msg = await update.message.reply_text("📷 正在分析圖片...")

    try:
        prompt_parts = []
        url_status = None

        if has_urls:
            logger.info(f"[photo] caption 含 {len(caption_urls)} 個 URL，啟動預處理")
            enhanced_caption, url_summaries, _ = await preprocess_urls(caption, CONFIG)
            if url_summaries:
                url_status = "\n".join(url_summaries)
            prompt_parts.append(enhanced_caption)
        elif caption:
            prompt_parts.append(caption)

        gemini_ok = GENAI_AVAILABLE and CONFIG.get("IMAGE_ANALYSIS_ENABLED")
        if gemini_ok:
            tg_file = await photo.get_file()
            image_bytes = await tg_file.download_as_bytearray()
            logger.info(f"[photo] Telegram 照片下載完成，{len(image_bytes)} bytes")

            gemini_context = caption
            if not gemini_context and bridge.history.messages:
                last_msgs = bridge.history.messages[-3:]
                gemini_context = "\n".join(m.content[:200] for m in last_msgs)

            loop = asyncio.get_running_loop()
            description = await loop.run_in_executor(
                None,
                describe_image_from_bytes,
                bytes(image_bytes), "image/jpeg", gemini_context
            )
            if description:
                prompt_parts.append(f"📷 圖片分析結果:\n{description}")
            else:
                prompt_parts.append("📷 圖片分析失敗（Gemini 暫時不可用）")
        else:
            if not has_urls:
                try:
                    await processing_msg.delete()
                except:
                    pass
                await update.message.reply_text(
                    "📷 收到照片，但圖片分析功能目前不可用。\n"
                    "請確認 GOOGLE_API_KEY 已設定且 google-generativeai 已安裝。"
                )
                return

        full_prompt = "\n\n".join(prompt_parts)
        progress_cb = make_progress_cb(processing_msg, CONFIG["SDK_PROGRESS_EDIT_INTERVAL"])
        photo_cb = make_photo_cb(update.message)
        result, _ = await bridge.handle_message(
            update.effective_chat.id, full_prompt, progress_cb=progress_cb, photo_cb=photo_cb,
        )

        try:
            await processing_msg.delete()
        except:
            pass

        if url_status:
            await update.message.reply_text(url_status)

        if len(result) > 4000:
            chunks = [result[i:i+4000] for i in range(0, len(result), 4000)]
            for i, chunk in enumerate(chunks):
                await update.message.reply_text(f"[{i+1}/{len(chunks)}]\n\n{chunk}")
        else:
            await update.message.reply_text(f"Claude 回應：\n\n{result}")

    except Exception as e:
        logger.error(f"[photo] 處理照片時發生錯誤: {e}")
        try:
            await processing_msg.delete()
        except:
            pass
        await update.message.reply_text(f"📷 處理照片時發生錯誤: {e}")


async def unsupported_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not bridge.is_authorized(user_id):
        return

    msg = update.message
    if msg.video:
        msg_type = "影片"
    elif msg.animation:
        msg_type = "GIF 動圖"
    elif msg.document:
        msg_type = f"檔案（{msg.document.file_name or '未知'}）"
    elif msg.voice:
        msg_type = "語音訊息"
    elif msg.audio:
        msg_type = "音檔"
    elif msg.video_note:
        msg_type = "圓形影片訊息"
    elif msg.sticker:
        msg_type = "貼圖"
    elif msg.contact:
        msg_type = "聯絡人"
    elif msg.location:
        msg_type = "位置"
    else:
        msg_type = "未知格式"

    logger.info(f"收到不支援的訊息類型: {msg_type} (chat_id={update.effective_chat.id})")
    await msg.reply_text(
        f"⚠️ 目前不支援「{msg_type}」格式。\n"
        "支援的輸入方式：\n"
        "• 文字訊息（含 URL 自動抓取）\n"
        "• 照片（含 caption + URL）\n"
        "• /clear /help /history /status /session /interrupt"
    )


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram.error import TimedOut, NetworkError, Conflict

    error = context.error

    if isinstance(error, TimedOut):
        logger.debug(f"Polling timeout (正常): {error}")
        return

    if isinstance(error, Conflict):
        logger.warning(f"Bot 實例衝突: {error}")
        return

    if isinstance(error, NetworkError):
        logger.warning(f"網路錯誤 (將自動重試): {error}")
        return

    logger.error(f"發生錯誤：{error}")
    if update and update.message:
        try:
            await update.message.reply_text("發生錯誤，請查看日誌")
        except Exception:
            pass


# === 啟動流程 ===

async def _post_init(application):
    """python-telegram-bot 的 post_init hook：在 polling 開始前啟動 SDK。"""
    global bridge
    try:
        await bridge.start_sdk()
    except Exception as e:
        logger.error(f"SDK 啟動失敗，bridge 將以受限模式繼續：{e}")


async def _post_shutdown(application):
    global bridge
    if bridge:
        try:
            await bridge.stop_sdk()
        except Exception as e:
            logger.warning(f"SDK 關閉時錯誤: {e}")

=======
        await update.message.reply_text(f"Claude:\n\n{result}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}")
    if update and update.message:
        await update.message.reply_text("An error occurred, please check logs")

def find_claude_cli():
    paths = [CONFIG["CLAUDE_CLI"], "claude"]
    # Windows-specific paths
    if sys.platform == "win32":
        paths.append(os.path.expandvars(r"%APPDATA%\npm\claude.cmd"))
    for path in paths:
        try:
            result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10, shell=True)
            if result.returncode == 0:
                logger.info(f"Found Claude CLI: {path}")
                return path
        except:
            continue
    return None


def main():
    global bridge

    logger.info("=" * 50)

    logger.info(f"啟動 {VERSION_LABEL}")
    logger.info("(Persistent Claude Agent SDK)")
=======
    logger.info("Starting Telegram Claude Code Bridge v2.6")

    logger.info("=" * 50)

    cleanup_old_logs()


    if not TELEGRAM_LIB_AVAILABLE:
        print("錯誤: 請先安裝 python-telegram-bot")
        sys.exit(1)
    if not SDK_AVAILABLE:
        print("錯誤: 請先安裝 claude-agent-sdk (pip install claude-agent-sdk)")
        sys.exit(1)
    if CONFIG["TELEGRAM_BOT_TOKEN"] == "MISSING_TG_BOT_TOKEN_ENV":
        print("錯誤: 環境變數 TG_BOT_TOKEN 未設定。")
        print("       請在 start_bridge_v3.0.bat 開頭加入：set TG_BOT_TOKEN=<your_bot_token>")
        print("       或在系統環境變數中永久設定。")
        sys.exit(1)

    bridge = ClaudeBridge()

    from telegram.request import HTTPXRequest

    request = HTTPXRequest(
        read_timeout=30,
        write_timeout=30,
        connect_timeout=30,
        pool_timeout=10,
    )
    get_updates_request = HTTPXRequest(
        read_timeout=60,
        write_timeout=30,
        connect_timeout=30,
        pool_timeout=10,
    )
    application = (
        Application.builder()
        .token(CONFIG["TELEGRAM_BOT_TOKEN"])
        .request(request)
        .get_updates_request(get_updates_request)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("ps", ps_command))
    application.add_handler(CommandHandler("cclog", cclog_command))
    application.add_handler(CommandHandler("tasklog", tasklog_command))
    application.add_handler(CommandHandler("bridge", bridge_log_command))
    application.add_handler(CommandHandler("uptime", uptime_command))
    application.add_handler(MessageHandler(filters.TEXT, message_handler))
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    application.add_handler(MessageHandler(filters.ALL, unsupported_handler))
    application.add_error_handler(error_handler)

    logger.info(f"工作目錄：{CONFIG['WORKING_DIR']}")
    logger.info(f"Claude CLI：{CONFIG.get('CLAUDE_CLI_PATH')} (exists={Path(CONFIG['CLAUDE_CLI_PATH']).exists() if CONFIG.get('CLAUDE_CLI_PATH') else 'N/A'})")
    logger.info(f"Permission mode：{CONFIG['SDK_PERMISSION_MODE']}")
    logger.info(f"Setting sources：{CONFIG['SDK_SETTING_SOURCES']}")
    logger.info(f"Skills：{CONFIG['SDK_SKILLS']}")
    logger.info(f"既有 session_id：{(bridge.current_session_id[:12] + '...') if bridge.current_session_id else '(新 session)'}")
    logger.info(f"URL 處理器: fxtwitter={'✅' if REQUESTS_AVAILABLE else '❌'}, yt-dlp={'✅' if YTDLP_AVAILABLE else '❌'}")
    img_flag = "✅" if (CONFIG.get("IMAGE_ANALYSIS_ENABLED") and GENAI_AVAILABLE) else "❌"
    logger.info(f"圖片分析: {img_flag}")
    logger.info("回覆抓取: ✅ Playwright-first")
    logger.info(f"Obsidian 自動落地: ✅ → {CONFIG['OBSIDIAN_MOBILE_DIR']}")
    logger.info("Bot 啟動中（SDK 將在 post_init 連線）...")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        bootstrap_retries=5,
    )

=======
    if not CONFIG["TELEGRAM_BOT_TOKEN"]:
        print("Error: TELEGRAM_BOT_TOKEN not set. Please configure .env file.")
        sys.exit(1)

    if not TELEGRAM_LIB_AVAILABLE:
        print("Error: python-telegram-bot not installed")
        print("Run: pip install python-telegram-bot")
        sys.exit(1)

    claude_path = find_claude_cli()
    if claude_path:
        CONFIG["CLAUDE_CLI"] = claude_path
    else:
        logger.error("Claude CLI not found!")
        print("Error: Claude CLI not found. Please install: npm install -g @anthropic-ai/claude-code")
        return

    bridge = ClaudeBridge()

    application = Application.builder().token(CONFIG["TELEGRAM_BOT_TOKEN"]).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("exec", exec_command))
    # Do NOT use ~filters.COMMAND: /clear /help /history /status /extract /fetch
    # must enter message_handler for bridge.handle_message() internal routing.
    # /start and /exec are already caught by CommandHandler above (takes priority).
    application.add_handler(MessageHandler(filters.TEXT, message_handler))
    application.add_error_handler(error_handler)

    logger.info(f"History messages: {len(bridge.history.messages)}")
    logger.info(f"Max history rounds: {CONFIG['MAX_HISTORY_ROUNDS']}")
    logger.info(f"Log directory: {CONFIG['LOG_DIR']}")
    logger.info(f"URL processors: fxtwitter={'✅' if REQUESTS_AVAILABLE else '❌'}, yt-dlp={'✅' if YTDLP_AVAILABLE else '❌'}")
    img_flag = "✅" if (CONFIG.get("IMAGE_ANALYSIS_ENABLED") and GENAI_AVAILABLE) else "❌"
    logger.info(f"Image analysis: {img_flag} (Gemini={'✅' if GENAI_AVAILABLE else '❌'}, enabled={CONFIG.get('IMAGE_ANALYSIS_ENABLED')})")
    logger.info("Bot started, waiting for Telegram messages...")

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
