#!/usr/bin/env python3
"""
Telegram <-> Claude Code Bridge with Context Memory
====================================================
A lightweight bridge that allows you to control Claude Code CLI
from your phone via Telegram, with conversation history support.

Features:
- Maintains conversation context across messages
- Daily log rotation with automatic cleanup
- Simple single-file deployment
- Windows native support

GitHub: https://github.com/YOUR_USERNAME/telegram-claude-bridge
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
from typing import Optional, List
from dataclasses import dataclass, field, asdict
import logging
from logging.handlers import TimedRotatingFileHandler

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, use system env vars

# === Configuration ===
BASE_DIR = Path(__file__).parent.resolve()

CONFIG = {
    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "ALLOWED_USER_IDS": [int(x) for x in os.getenv("ALLOWED_USER_ID", "").split(",") if x.strip().isdigit()],
    "CLAUDE_CLI": os.getenv("CLAUDE_CLI_PATH", os.path.expandvars(r"%APPDATA%\npm\claude.cmd")),
    "WORKING_DIR": Path.home() / "claude-workspace",
    "HISTORY_FILE": BASE_DIR / "conversation_history.json",
    "LOG_DIR": BASE_DIR / "logs",
    "TIMEOUT": int(os.getenv("TIMEOUT", "300")),
    "MAX_HISTORY_ROUNDS": int(os.getenv("MAX_HISTORY_ROUNDS", "10")),
    "ALLOW_DANGEROUS": os.getenv("ALLOW_DANGEROUS", "false").lower() == "true",
    "LOG_RETENTION_DAYS": int(os.getenv("LOG_RETENTION_DAYS", "14")),
}

CONFIG["WORKING_DIR"].mkdir(parents=True, exist_ok=True)
CONFIG["LOG_DIR"].mkdir(parents=True, exist_ok=True)

# === Logging Setup (Daily Rotation) ===
def setup_logging():
    log_file = CONFIG["LOG_DIR"] / "bridge.log"
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    file_handler = TimedRotatingFileHandler(
        log_file,
        when='midnight',
        interval=1,
        backupCount=CONFIG["LOG_RETENTION_DAYS"],
        encoding='utf-8'
    )
    file_handler.suffix = "%Y-%m-%d.log"
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
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
        logging.info(f"Cleaned up {deleted_count} old log files (older than {CONFIG['LOG_RETENTION_DAYS']} days)")

logger = setup_logging()

# === Telegram Bot Import ===
try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
    TELEGRAM_LIB_AVAILABLE = True
except ImportError:
    TELEGRAM_LIB_AVAILABLE = False
    logger.error("python-telegram-bot not installed. Run: pip install python-telegram-bot")

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
        lines.append("=== Current Request ===")
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
                logger.info(f"Loaded {len(history.messages)} messages from history")
        except Exception as e:
            logger.error(f"Failed to load history: {e}")
        return history

class ClaudeBridge:
    def __init__(self):
        self.history = ConversationHistory.load(CONFIG["HISTORY_FILE"], CONFIG["MAX_HISTORY_ROUNDS"])
        self.is_busy = False
        self.special_commands = {
            "/clear": self._cmd_clear,
            "/history": self._cmd_show_history,
            "/help": self._cmd_help,
            "/status": self._cmd_status
        }
    
    def is_authorized(self, user_id: int) -> bool:
        if not CONFIG["ALLOWED_USER_IDS"]:
            logger.warning("No ALLOWED_USER_IDS configured! Anyone can use this bot.")
            return True
        return user_id in CONFIG["ALLOWED_USER_IDS"]
    
    async def _cmd_clear(self, chat_id: int) -> str:
        self.history.clear()
        self.history.save(CONFIG["HISTORY_FILE"])
        return "Conversation history cleared. New messages will not include previous context."
    
    async def _cmd_show_history(self, chat_id: int) -> str:
        if not self.history.messages:
            return "No conversation history."
        lines = [f"Conversation History ({len(self.history.messages)} messages):"]
        for i, msg in enumerate(self.history.messages):
            prefix = "You" if msg.role == "user" else "Claude"
            preview = msg.content[:100] + "..." if len(msg.content) > 100 else msg.content
            preview = preview.replace('\n', ' ')
            lines.append(f"[{i+1}] {prefix}: {preview}")
        return "\n".join(lines)
    
    async def _cmd_help(self, chat_id: int) -> str:
        return f"""Telegram Claude Bridge - Help

Commands:
/clear - Clear conversation history
/history - Show conversation history
/status - Show system status
/help - Show this help message
/exec <cmd> - Execute PowerShell command directly

Usage:
Just send any message to interact with Claude Code.
The bot maintains the last {CONFIG['MAX_HISTORY_ROUNDS']} conversation rounds as context.

Tips:
- Claude understands references like "this", "that", "the three items" based on context
- Use /clear to start a fresh conversation
"""
    
    async def _cmd_status(self, chat_id: int) -> str:
        log_files = list(CONFIG["LOG_DIR"].glob("bridge.log*"))
        status = "Busy" if self.is_busy else "Ready"
        return f"""System Status

History Messages: {len(self.history.messages)}
Max History Rounds: {CONFIG['MAX_HISTORY_ROUNDS']}
Working Directory: {CONFIG['WORKING_DIR']}
Log Directory: {CONFIG['LOG_DIR']}
Log Files: {len(log_files)}
Log Retention: {CONFIG['LOG_RETENTION_DAYS']} days
Claude Status: {status}
Current Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""

    def _build_prompt_with_context(self, user_message: str) -> str:
        context = self.history.get_context_summary()
        safety_note = "Safety: Do not delete important files or modify system settings." if not CONFIG["ALLOW_DANGEROUS"] else ""
        if context:
            return f"{context}\n{user_message}\n\n{safety_note}\nPlease understand and execute the current request based on the conversation context above."
        return f"{user_message}\n\n{safety_note}"

    async def execute_claude(self, prompt: str) -> str:
        if self.is_busy:
            return "Claude is processing another task. Please wait..."
        self.is_busy = True
        try:
            full_prompt = self._build_prompt_with_context(prompt)
            logger.info(f"Executing Claude command: {prompt[:100]}...")
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
            return f"Execution timeout ({CONFIG['TIMEOUT']} seconds)"
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
    
    async def handle_message(self, chat_id: int, text: str) -> str:
        text = text.strip()
        cmd = text.split()[0].lower() if text else ""
        if cmd in self.special_commands:
            return await self.special_commands[cmd](chat_id)
        logger.info(f"Received message (chat_id={chat_id}): {text[:100]}...")
        self.history.add_user_message(text)
        response = await self.execute_claude(text)
        self.history.add_assistant_message(response)
        self.history.save(CONFIG["HISTORY_FILE"])
        return response

bridge = None

# === Telegram Handlers ===

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not bridge.is_authorized(user.id):
        await update.message.reply_text(f"Unauthorized user\nYour User ID: {user.id}")
        return
    await update.message.reply_text(
        f"Telegram Claude Code Bridge\n\n"
        f"Welcome, {user.first_name}!\n\n"
        f"Features:\n"
        f"- Maintains last {CONFIG['MAX_HISTORY_ROUNDS']} conversation rounds\n"
        f"- Daily logs with {CONFIG['LOG_RETENTION_DAYS']}-day auto cleanup\n\n"
        f"Type /help to see all commands."
    )

async def exec_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bridge.is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized")
        return
    if not context.args:
        await update.message.reply_text("Usage: /exec <PowerShell command>")
        return
    command = ' '.join(context.args)
    await update.message.reply_text(f"Executing: {command}")
    try:
        result = subprocess.run(
            ["powershell", "-Command", command],
            capture_output=True, text=True, timeout=60,
            cwd=str(CONFIG["WORKING_DIR"]),
            encoding='utf-8', errors='replace'
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
    processing_msg = await update.message.reply_text(
        f"Claude is processing...\n{text[:50]}{'...' if len(text) > 50 else ''}"
    )
    result = await bridge.handle_message(update.effective_chat.id, text)
    try:
        await processing_msg.delete()
    except:
        pass
    if len(result) > 4000:
        chunks = [result[i:i+4000] for i in range(0, len(result), 4000)]
        for i, chunk in enumerate(chunks):
            await update.message.reply_text(f"[{i+1}/{len(chunks)}]\n\n{chunk}")
    else:
        await update.message.reply_text(f"Claude Response:\n\n{result}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error occurred: {context.error}")
    if update and update.message:
        await update.message.reply_text("An error occurred. Check logs for details.")

def find_claude_cli():
    paths = [
        CONFIG["CLAUDE_CLI"],
        "claude",
        os.path.expandvars(r"%APPDATA%\npm\claude.cmd"),
        os.path.expandvars(r"%USERPROFILE%\AppData\Roaming\npm\claude.cmd"),
    ]
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
    logger.info("Starting Telegram Claude Code Bridge")
    logger.info("=" * 50)
    
    # Cleanup old logs on startup
    cleanup_old_logs()
    
    if not TELEGRAM_LIB_AVAILABLE:
        print("Error: python-telegram-bot not installed")
        print("Run: pip install -r requirements.txt")
        sys.exit(1)
    
    if not CONFIG["TELEGRAM_BOT_TOKEN"]:
        print("Error: TELEGRAM_BOT_TOKEN not configured")
        print("1. Copy .env.example to .env")
        print("2. Add your bot token from @BotFather")
        sys.exit(1)
    
    claude_path = find_claude_cli()
    if claude_path:
        CONFIG["CLAUDE_CLI"] = claude_path
    else:
        logger.error("Claude CLI not found!")
        logger.error("Please install: npm install -g @anthropic-ai/claude-code")
        return
    
    # Initialize bridge
    bridge = ClaudeBridge()
    
    application = Application.builder().token(CONFIG["TELEGRAM_BOT_TOKEN"]).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("exec", exec_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    application.add_error_handler(error_handler)
    
    logger.info(f"History messages: {len(bridge.history.messages)}")
    logger.info(f"Max history rounds: {CONFIG['MAX_HISTORY_ROUNDS']}")
    logger.info(f"Log directory: {CONFIG['LOG_DIR']}")
    logger.info(f"Log retention: {CONFIG['LOG_RETENTION_DAYS']} days")
    logger.info("Bot started, waiting for messages...")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

