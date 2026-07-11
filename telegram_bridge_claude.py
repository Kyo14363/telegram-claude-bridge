#!/usr/bin/env python3
"""
Telegram <-> Claude Code Bridge v3.3 — Entry Point
====================================================
Public modular layout (Phase 1 module split):
  config.py       — env-driven CONFIG, system_prompt fragments, log redaction
  bridge_core.py  — ClaudeBridge, SDK lifecycle, session persistence
  handlers.py     — Telegram message/photo/start/exec/error handlers
  shortcuts.py    — PowerShell shortcut commands (/ps /cclog /tasklog /bridge /uptime)
  metrics.py      — usage stats backed by stats.json
  url_fetchers.py — X/Twitter, YouTube, GitHub, general article extraction
  vision.py       — optional Gemini Vision image description
  reply_fetcher.py — optional twikit-based X/Twitter reply capture

This file just wires python-telegram-bot up and starts polling.
"""

import sys
import logging
from pathlib import Path

import bridge_core
from bridge_core import (
    CONFIG, VERSION, VERSION_LABEL, cleanup_old_logs,
    init_bridge, SDK_AVAILABLE,
    REQUESTS_AVAILABLE, YTDLP_AVAILABLE, GENAI_AVAILABLE, TWIKIT_AVAILABLE,
)

try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, MessageHandler, filters
    from handlers import (
        start_command, exec_command, resume_command, handoff_command,
        message_handler, photo_handler,
        unsupported_handler, error_handler,
    )
    from shortcuts import (
        ps_command, cclog_command, tasklog_command, bridge_log_command, uptime_command,
    )
    TELEGRAM_LIB_AVAILABLE = True
except ImportError:
    TELEGRAM_LIB_AVAILABLE = False

logger = logging.getLogger(__name__)


# === Startup hooks ===

async def _post_init(application):
    """python-telegram-bot post_init: connect SDK before polling starts."""
    try:
        await bridge_core.bridge.start_sdk()
    except Exception as e:
        logger.error(f"SDK 啟動失敗，bridge 將以受限模式繼續：{e}")


async def _post_shutdown(application):
    if bridge_core.bridge:
        try:
            await bridge_core.bridge.stop_sdk()
        except Exception as e:
            logger.warning(f"SDK 關閉時錯誤: {e}")


def main():
    logger.info("=" * 50)
    logger.info(f"啟動 {VERSION_LABEL}")
    logger.info("(Persistent Claude Agent SDK)")
    logger.info("=" * 50)

    cleanup_old_logs()

    if not TELEGRAM_LIB_AVAILABLE:
        print("錯誤: 請先安裝 python-telegram-bot")
        sys.exit(1)
    if not SDK_AVAILABLE:
        print("錯誤: 請先安裝 claude-agent-sdk (pip install claude-agent-sdk)")
        sys.exit(1)
    if CONFIG["TELEGRAM_BOT_TOKEN"].startswith("MISSING_"):
        print("錯誤: 環境變數 TELEGRAM_BOT_TOKEN 未設定。")
        print("       請複製 .env.example 為 .env 並填入 Bot Token，")
        print("       或在系統環境變數中永久設定。")
        sys.exit(1)

    init_bridge()

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
    application.add_handler(CommandHandler("exec", exec_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("handoff", handoff_command))
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
    cli_path = CONFIG.get("CLAUDE_CLI_PATH")
    cli_exists = Path(cli_path).exists() if cli_path else "N/A"
    logger.info(f"Claude CLI：{cli_path} (exists={cli_exists})")
    logger.info(f"模型（已釘選，免疫全域 settings 漂移）：{CONFIG['SDK_MODEL']}")
    logger.info(f"Permission mode：{CONFIG['SDK_PERMISSION_MODE']}")
    logger.info(f"Setting sources：{CONFIG['SDK_SETTING_SOURCES']}")
    logger.info(f"Skills：{CONFIG['SDK_SKILLS']}")
    sid = bridge_core.bridge.current_session_id
    logger.info(f"既有 session_id：{(sid[:12] + '...') if sid else '(新 session)'}")
    logger.info(f"URL 處理器: fxtwitter={'OK' if REQUESTS_AVAILABLE else 'OFF'}, yt-dlp={'OK' if YTDLP_AVAILABLE else 'OFF'}")
    img_flag = "OK" if (CONFIG.get("IMAGE_ANALYSIS_ENABLED") and GENAI_AVAILABLE) else "OFF"
    logger.info(f"圖片分析: {img_flag}")
    logger.info(f"回覆抓取: {'OK' if TWIKIT_AVAILABLE else 'OFF'}")
    logger.info(f"Obsidian 自動落地：{CONFIG['OBSIDIAN_MOBILE_DIR']}")
    logger.info("Bot 啟動中（SDK 將在 post_init 連線）...")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        bootstrap_retries=5,
    )


if __name__ == "__main__":
    main()
