"""
T-M-B Telegram Handlers — message, photo, start, exec, unsupported, error.

Extracted from telegram_bridge_claude_v3.1.py (Phase 1 module split, 2026-05-06).
"""

import asyncio
import subprocess
import logging
from io import BytesIO

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes

import bridge_core
from bridge_core import CONFIG, VERSION_LABEL
from url_fetchers import detect_urls
from reply_fetcher import detect_reply_keywords
from vision import GENAI_AVAILABLE, describe_image_from_bytes

logger = logging.getLogger(__name__)


# === Callback factories ===

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
                await reply_target.reply_document(document=buf, filename=buf.name,
                    caption=f"📷 截圖（{len(data)//1024} KB，超過 photo 限制改傳檔案）")
            else:
                await reply_target.reply_photo(photo=buf)
        except Exception as e:
            logger.warning(f"[photo_cb] 推送失敗: {e}")
    return cb


# === 快捷鍵盤 ===

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


# === Command Handlers ===

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not bridge_core.bridge.is_authorized(user.id):
        await update.message.reply_text(f"未授權的用戶\n你的 User ID: {user.id}")
        return

    img_text = "✅ 啟用" if (CONFIG.get("IMAGE_ANALYSIS_ENABLED") and GENAI_AVAILABLE) else "❌ 停用"
    sdk_text = "✅ 已連線（常駐）" if bridge_core.bridge.sdk_client else "❌ 未連線"
    sid_text = (bridge_core.bridge.current_session_id[:12] + "...") if bridge_core.bridge.current_session_id else "(新 session)"

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


async def exec_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bridge_core.bridge.is_authorized(update.effective_user.id):
        await update.message.reply_text("未授權")
        return
    if not context.args:
        await update.message.reply_text("用法：/exec <PowerShell命令>")
        return
    command = ' '.join(context.args)
    await update.message.reply_text(f"執行中：{command}")
    try:
        result = subprocess.run(
            ["powershell", "-Command", command],
            capture_output=True, text=True, timeout=60,
            cwd=str(CONFIG["WORKING_DIR"]), encoding='utf-8', errors='replace'
        )
        output = result.stdout or result.stderr or "(無輸出)"
        if len(output) > 8000:
            output = output[:8000] + "\n...(已截斷)"
        status = "成功" if result.returncode == 0 else "失敗"
        await update.message.reply_text(f"{status} 結果：\n{output}")
    except subprocess.TimeoutExpired:
        await update.message.reply_text("執行超時")
    except Exception as e:
        await update.message.reply_text(f"錯誤：{e}")


# === Message Handlers ===

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not bridge_core.bridge.is_authorized(user_id):
        await update.message.reply_text(f"未授權\n你的 User ID: {user_id}")
        return

    text = update.message.text

    cmd = text.strip().split()[0].lower() if text.strip() else ""
    if cmd in bridge_core.bridge.special_commands:
        result, _ = await bridge_core.bridge.handle_message(update.effective_chat.id, text)
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
    result, url_status = await bridge_core.bridge.handle_message(
        update.effective_chat.id, text, progress_cb=progress_cb, photo_cb=photo_cb,
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


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not bridge_core.bridge.is_authorized(user_id):
        await update.message.reply_text(f"未授權\n你的 User ID: {user_id}")
        return

    caption = update.message.caption or ""
    photo = update.message.photo[-1]
    logger.info(f"收到照片 (chat_id={update.effective_chat.id}, file_id={photo.file_id}, caption={caption[:50]})")
    bridge_core.bridge.metrics.record_photo()

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
        # v3.1.2 修正（2026-05-07）：移除 photo_handler 內的 preprocess_urls 呼叫，
        # 改由下游 bridge.handle_message 統一處理 URL，避免雙重 fetch / prompt 膨脹。
        # url_status 從 handle_message 回傳值取得（仍會顯示給用戶）。
        if caption:
            prompt_parts.append(caption)

        gemini_ok = GENAI_AVAILABLE and CONFIG.get("IMAGE_ANALYSIS_ENABLED")
        if gemini_ok:
            tg_file = await photo.get_file()
            image_bytes = await tg_file.download_as_bytearray()
            logger.info(f"[photo] Telegram 照片下載完成，{len(image_bytes)} bytes")

            gemini_context = caption
            if not gemini_context and bridge_core.bridge.history.messages:
                last_msgs = bridge_core.bridge.history.messages[-3:]
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
                    "請確認 GOOGLE_API_KEY 已設定且 google-genai 已安裝。"
                )
                return

        full_prompt = "\n\n".join(prompt_parts)
        progress_cb = make_progress_cb(processing_msg, CONFIG["SDK_PROGRESS_EDIT_INTERVAL"])
        photo_cb = make_photo_cb(update.message)
        # v3.1.2：接收 handle_message 回傳的 url_status（取代原本 photo_handler 內的預處理）
        result, url_status = await bridge_core.bridge.handle_message(
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
    if not bridge_core.bridge.is_authorized(user_id):
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
