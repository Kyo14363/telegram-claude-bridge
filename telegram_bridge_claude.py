#!/usr/bin/env python3
"""
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
import re
import glob
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Tuple
from dataclasses import dataclass, field, asdict
import logging
from logging.handlers import TimedRotatingFileHandler

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
        logging.info(f"Cleaned up {deleted_count} log files older than {CONFIG['LOG_RETENTION_DAYS']} days")

logger = setup_logging()

# === External modules ===
try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
    TELEGRAM_LIB_AVAILABLE = True
except ImportError:
    TELEGRAM_LIB_AVAILABLE = False

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
                logger.info(f"Loaded {len(history.messages)} history messages")
        except Exception as e:
            logger.error(f"Failed to load history: {e}")
        return history


# === Main Bridge ===

class ClaudeBridge:
    def __init__(self):
        self.history = ConversationHistory.load(CONFIG["HISTORY_FILE"], CONFIG["MAX_HISTORY_ROUNDS"])
        self.is_busy = False
        self.special_commands = {
            "/clear": self._cmd_clear,
            "/history": self._cmd_show_history,
            "/help": self._cmd_help,
            "/status": self._cmd_status,
            "/extract": self._cmd_extract,
            "/fetch": self._cmd_fetch,
        }

    def is_authorized(self, user_id: int) -> bool:
        if not CONFIG["ALLOWED_USER_IDS"]:
            return True
        return user_id in CONFIG["ALLOWED_USER_IDS"]

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

    async def _cmd_help(self, chat_id: int) -> str:
        url_status = []
        url_status.append(f"  fxtwitter (X/Twitter): {'✅' if REQUESTS_AVAILABLE else '❌ needs requests'}")
        url_status.append(f"  yt-dlp (YouTube/general): {'✅' if YTDLP_AVAILABLE else '❌ not installed'}")
        url_status.append(f"  HTTP fallback: {'✅' if REQUESTS_AVAILABLE else '❌ needs requests'}")
        url_block = "\n".join(url_status)

        img_enabled = CONFIG.get("IMAGE_ANALYSIS_ENABLED", False)
        if img_enabled and GENAI_AVAILABLE:
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
                await asyncio.get_event_loop().run_in_executor(
                    None, save_fetch_output, fetch_url, enhanced_text, response, user_note, CONFIG
                )
        self.history.add_assistant_message(response)
        self.history.save(CONFIG["HISTORY_FILE"])

        return response, url_status


# === Telegram Handlers ===

bridge = None

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not bridge.is_authorized(user.id):
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
        await update.message.reply_text(f"Unauthorized\nYour User ID: {user_id}")
        return

    text = update.message.text

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
    logger.info("Starting Telegram Claude Code Bridge v2.6")
    logger.info("=" * 50)

    cleanup_old_logs()

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
