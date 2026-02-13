#!/usr/bin/env python3
"""
Telegram <-> Claude Code Bridge with Context Memory v2.4
========================================================
- 對話歷史維護機制
- 每日獨立 log 檔案
- 自動清理 14 天前的舊 log
- URL 預處理：自動抓取連結內容（fxtwitter 為主，yt-dlp 為輔）
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
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field, asdict
import logging
from logging.handlers import TimedRotatingFileHandler
from urllib.parse import urlparse

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# === Configuration ===
BASE_DIR = Path(__file__).parent.resolve()

CONFIG = {
    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "ALLOWED_USER_IDS": [int(x) for x in os.getenv("ALLOWED_USER_ID", "").split(",") if x.strip().isdigit()],
    "CLAUDE_CLI": os.getenv("CLAUDE_CLI_PATH", os.path.expandvars(r"%APPDATA%\\npm\\claude.cmd")),
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
}

CONFIG["WORKING_DIR"].mkdir(parents=True, exist_ok=True)
CONFIG["LOG_DIR"].mkdir(parents=True, exist_ok=True)
CONFIG["FETCH_OUTPUT_DIR"].mkdir(parents=True, exist_ok=True)

# === 日誌設定（每日輪換）===
def setup_logging():
    """設定每日輪換的日誌系統"""
    log_file = CONFIG["LOG_DIR"] / "bridge.log"
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    file_handler = TimedRotatingFileHandler(
        log_file, when='midnight', interval=1,
        backupCount=CONFIG["LOG_RETENTION_DAYS"], encoding='utf-8'
    )
    file_handler.suffix = "%Y-%m-%d.log"
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logging.getLogger(__name__)

def cleanup_old_logs():
    """清理超過保留天數的舊 log 檔案"""
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

try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
    TELEGRAM_LIB_AVAILABLE = True
except ImportError:
    TELEGRAM_LIB_AVAILABLE = False

# === URL 預處理模組 ===

# 檢查 yt-dlp 是否可用
try:
    import yt_dlp
    YTDLP_AVAILABLE = True
    logger.info("yt-dlp 可用，已啟用作為備用 URL 處理器")
except ImportError:
    YTDLP_AVAILABLE = False
    logger.info("yt-dlp 未安裝，僅使用 fxtwitter/HTTP 方案處理 URL")

# 檢查 requests 是否可用
# --- LangExtract ---
try:
    import langextract as lx
    LANGEXTRACT_AVAILABLE = True
except ImportError:
    LANGEXTRACT_AVAILABLE = False
    lx = None

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    logger.warning("requests 未安裝，URL 預處理功能將受限")


# --- URL 偵測與分類 ---

# 支援的平台 domain 對應
PLATFORM_PATTERNS = {
    "x_twitter": [
        r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/\S+",
        r"(?:https?://)?t\.co/\S+",
    ],
    "youtube": [
        r"(?:https?://)?(?:www\.)?youtube\.com/watch\S+",
        r"(?:https?://)?youtu\.be/\S+",
        r"(?:https?://)?(?:www\.)?youtube\.com/shorts/\S+",
    ],
    "general": [
        r"https?://\S+",
    ],
}

def detect_urls(text: str) -> List[Tuple[str, str]]:
    """
    從訊息中偵測 URL 並分類平台。
    回傳 [(url, platform), ...] 的列表。
    優先匹配特定平台，最後才匹配 general。
    """
    found = []
    found_urls = set()
    
    # 先匹配特定平台
    for platform in ["x_twitter", "youtube"]:
        for pattern in PLATFORM_PATTERNS[platform]:
            for match in re.finditer(pattern, text):
                url = match.group(0)
                if url not in found_urls:
                    found_urls.add(url)
                    found.append((url, platform))
    
    # 再匹配其他所有 URL
    for pattern in PLATFORM_PATTERNS["general"]:
        for match in re.finditer(pattern, text):
            url = match.group(0)
            if url not in found_urls:
                found_urls.add(url)
                found.append((url, "general"))
    
    return found


# --- 方案 D: fxtwitter (X/Twitter 專用) ---

def fetch_via_fxtwitter(url: str) -> Optional[str]:
    """
    用 fxtwitter.com API 抓取 X/Twitter 推文內容。
    將 x.com / twitter.com 替換成 api.fxtwitter.com 取得 JSON。
    """
    if not REQUESTS_AVAILABLE:
        return None
    
    try:
        # 將 URL 轉成 fxtwitter API 格式
        api_url = re.sub(
            r"https?://(www\.)?(twitter\.com|x\.com)",
            "https://api.fxtwitter.com",
            url
        )
        
        logger.info(f"[fxtwitter] 嘗試抓取: {api_url}")
        
        resp = requests.get(api_url, timeout=CONFIG["URL_FETCH_TIMEOUT"], headers={
            "User-Agent": "TelegramClaudeBridge/2.2"
        })
        
        if resp.status_code != 200:
            logger.warning(f"[fxtwitter] HTTP {resp.status_code}")
            return None
        
        data = resp.json()
        tweet = data.get("tweet", {})
        
        if not tweet:
            logger.warning("[fxtwitter] 回應中無 tweet 資料")
            return None
        
        # 組裝推文內容
        parts = []
        parts.append(f"📌 推文來源: {url}")
        
        author = tweet.get("author", {})
        if author:
            parts.append(f"👤 作者: {author.get('name', '?')} (@{author.get('screen_name', '?')})")
        
        text = tweet.get("text", "")
        if text:
            parts.append(f"📝 內容:\n{text}")
        
        # 媒體資訊
        media = tweet.get("media", {})
        if media:
            photos = media.get("photos", [])
            videos = media.get("videos", [])
            if photos:
                parts.append(f"🖼️ 包含 {len(photos)} 張圖片")
            if videos:
                parts.append(f"🎬 包含 {len(videos)} 個影片")
        
        # 互動數據
        likes = tweet.get("likes", 0)
        retweets = tweet.get("retweets", 0)
        replies = tweet.get("replies", 0)
        if likes or retweets or replies:
            parts.append(f"💬 互動: {likes} 讚 / {retweets} 轉推 / {replies} 回覆")
        
        created = tweet.get("created_at", "")
        if created:
            parts.append(f"📅 發布時間: {created}")
        
        # 引用推文
        quote = tweet.get("quote", {})
        if quote:
            quote_author = quote.get("author", {})
            quote_text = quote.get("text", "")
            parts.append(f"\n↩️ 引用推文 (@{quote_author.get('screen_name', '?')}):\n{quote_text}")
        
        result = "\n".join(parts)
        logger.info(f"[fxtwitter] 成功抓取推文，{len(result)} 字元")
        return result
        
    except requests.Timeout:
        logger.warning(f"[fxtwitter] 請求超時")
        return None
    except Exception as e:
        logger.error(f"[fxtwitter] 錯誤: {e}")
        return None


# --- 方案 C: yt-dlp (通用備用) ---

def fetch_via_ytdlp(url: str) -> Optional[str]:
    """
    用 yt-dlp 提取 URL 的 metadata（不下載檔案）。
    支援 X/Twitter、YouTube、TikTok 等上千個平台。
    """
    if not YTDLP_AVAILABLE:
        return None
    
    try:
        logger.info(f"[yt-dlp] 嘗試抓取: {url}")
        
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'socket_timeout': CONFIG["URL_FETCH_TIMEOUT"],
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        
        if not info:
            return None
        
        # 組裝 metadata
        parts = []
        parts.append(f"🔗 來源: {url}")
        
        title = info.get('title')
        if title:
            parts.append(f"📌 標題: {title}")
        
        uploader = info.get('uploader') or info.get('channel')
        if uploader:
            parts.append(f"👤 作者/頻道: {uploader}")
        
        description = info.get('description', '')
        if description:
            # 限制描述長度避免太長
            desc_preview = description[:1000]
            if len(description) > 1000:
                desc_preview += "...(已截斷)"
            parts.append(f"📝 描述/內容:\n{desc_preview}")
        
        duration = info.get('duration')
        if duration:
            mins, secs = divmod(int(duration), 60)
            hours, mins = divmod(mins, 60)
            if hours:
                parts.append(f"⏱️ 時長: {hours}:{mins:02d}:{secs:02d}")
            else:
                parts.append(f"⏱️ 時長: {mins}:{secs:02d}")
        
        view_count = info.get('view_count')
        like_count = info.get('like_count')
        if view_count or like_count:
            stats = []
            if view_count:
                stats.append(f"{view_count:,} 觀看")
            if like_count:
                stats.append(f"{like_count:,} 讚")
            parts.append(f"📊 數據: {' / '.join(stats)}")
        
        upload_date = info.get('upload_date')
        if upload_date:
            try:
                formatted = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
                parts.append(f"📅 發布日期: {formatted}")
            except:
                pass
        
        # 如果有字幕/自動字幕，提示可用
        subtitles = info.get('subtitles', {})
        auto_subs = info.get('automatic_captions', {})
        if subtitles or auto_subs:
            langs = list(subtitles.keys()) + list(auto_subs.keys())
            parts.append(f"💬 可用字幕語言: {', '.join(langs[:10])}")
        
        result = "\n".join(parts)
        logger.info(f"[yt-dlp] 成功抓取 metadata，{len(result)} 字元")
        return result
        
    except Exception as e:
        logger.error(f"[yt-dlp] 錯誤: {e}")
        return None


# --- 方案 fallback: 基本 HTTP 抓取 ---

def fetch_via_http(url: str) -> Optional[str]:
    """
    基本 HTTP GET，嘗試抓取頁面標題和 meta description。
    作為最後的 fallback。
    """
    if not REQUESTS_AVAILABLE:
        return None
    
    try:
        logger.info(f"[http] 嘗試抓取: {url}")
        
        resp = requests.get(url, timeout=CONFIG["URL_FETCH_TIMEOUT"], headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }, allow_redirects=True)
        
        if resp.status_code != 200:
            return None
        
        content = resp.text[:10000]  # 只取前 10K
        
        parts = [f"🔗 來源: {url}"]
        
        # 提取 <title>
        title_match = re.search(r'<title[^>]*>(.*?)</title>', content, re.IGNORECASE | re.DOTALL)
        if title_match:
            title = re.sub(r'\s+', ' ', title_match.group(1)).strip()
            parts.append(f"📌 標題: {title}")
        
        # 提取 og:title, og:description
        og_title = re.search(r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\'](.*?)["\']', content, re.IGNORECASE)
        og_desc = re.search(r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\'](.*?)["\']', content, re.IGNORECASE)
        meta_desc = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\'](.*?)["\']', content, re.IGNORECASE)
        
        if og_title:
            parts.append(f"📌 OG 標題: {og_title.group(1)}")
        
        desc = (og_desc and og_desc.group(1)) or (meta_desc and meta_desc.group(1))
        if desc:
            parts.append(f"📝 描述: {desc}")
        
        if len(parts) <= 1:
            # 幾乎什麼都沒抓到
            return None
        
        result = "\n".join(parts)
        logger.info(f"[http] 成功抓取基本資訊，{len(result)} 字元")
        return result
        
    except Exception as e:
        logger.error(f"[http] 錯誤: {e}")
        return None


# --- URL 預處理調度器 ---


def enhance_with_langextract(raw_content, url):
    # Use LE to extract structured info from fetched web content
    if not LANGEXTRACT_AVAILABLE or len(raw_content) < 200:
        return None
    try:
        import os
        if not os.getenv('GOOGLE_API_KEY'):
            return None
        logger.info(f'[langextract] extracting ({len(raw_content)} chars)...')
        extract_results = lx.extract(
            text=raw_content[:5000],
            prompt='Extract key information: main topic, key claims/data, people/orgs, numbers/stats, conclusion.',
            model='gemini-2.0-flash'
        )
        if not extract_results:
            return None
        result_text = str(extract_results)
        if len(result_text) < 50:
            return None
        sep = chr(10) + chr(10)
        return raw_content + sep + '=== LangExtract ===' + chr(10) + result_text[:2000] + chr(10) + '=== end ==='
    except Exception as e:
        logger.error(f'[langextract] failed: {e}')
        return None


def extract_structured_data(text, prompt=None):
    # /extract command: structured extraction on any text
    if not LANGEXTRACT_AVAILABLE:
        return 'LangExtract not installed'
    import os
    if not os.getenv('GOOGLE_API_KEY'):
        return 'GOOGLE_API_KEY not set'
    try:
        dp = 'Extract all key entities, facts, numbers, relationships. Organize in structured format.'
        res = lx.extract(text=text[:8000], prompt=prompt or dp, model='gemini-2.0-flash')
        if res:
            return 'LangExtract result:' + chr(10) + chr(10) + str(res)[:3000]
        return 'Extraction complete but no results'
    except Exception as e:
        return f'Extraction failed: {e}'



def save_fetch_output(url, fetched_content, claude_response, user_note=""):
    # Save AI-friendly markdown summary to fetch_outputs/
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_url = re.sub(r"[^a-zA-Z0-9]", "_", url[:60])
        filename = f"fetch_{ts}_{safe_url}.md"
        filepath = CONFIG["FETCH_OUTPUT_DIR"] / filename
        sep = chr(10)
        parts = []
        parts.append("# AI-Friendly Content Summary")
        parts.append("")
        parts.append(f"- **Source**: {url}")
        parts.append(f"- **Fetched**: {datetime.now().isoformat()}")
        if user_note:
            parts.append(f"- **User Note**: {user_note}")
        parts.append("")
        parts.append("---")
        parts.append("")
        parts.append("## Fetched Content")
        parts.append("")
        parts.append(fetched_content)
        parts.append("")
        parts.append("---")
        parts.append("")
        parts.append("## Claude Analysis")
        parts.append("")
        parts.append(claude_response)
        content = sep.join(parts)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"[fetch] Saved: {filepath} ({len(content)} chars)")
        return str(filepath)
    except Exception as e:
        logger.error(f"[fetch] Save failed: {e}")
        return None


async def preprocess_urls(text: str) -> Tuple[str, List[str]]:
    """
    偵測訊息中的 URL，自動抓取內容，回傳增強後的訊息。
    
    策略：
    - X/Twitter: fxtwitter (方案D) → yt-dlp (方案C) → http fallback
    - YouTube/其他 yt-dlp 支援平台: yt-dlp (方案C) → http fallback
    - 其他 URL: http fallback
    
    回傳: (增強後的完整訊息, 處理摘要列表)
    """
    urls = detect_urls(text)
    
    if not urls:
        return text, []
    
    logger.info(f"偵測到 {len(urls)} 個 URL: {urls}")
    
    enrichments = []
    summaries = []
    
    for url, platform in urls:
        content = None
        method_used = None
        
        if platform == "x_twitter":
            # X/Twitter: D → C → http
            content = await asyncio.get_event_loop().run_in_executor(
                None, fetch_via_fxtwitter, url
            )
            if content:
                method_used = "fxtwitter"
            else:
                content = await asyncio.get_event_loop().run_in_executor(
                    None, fetch_via_ytdlp, url
                )
                if content:
                    method_used = "yt-dlp"
        
        elif platform == "youtube":
            # YouTube: C → http
            content = await asyncio.get_event_loop().run_in_executor(
                None, fetch_via_ytdlp, url
            )
            if content:
                method_used = "yt-dlp"
        
        # 通用 fallback
        if not content:
            content = await asyncio.get_event_loop().run_in_executor(
                None, fetch_via_http, url
            )
            if content:
                method_used = "http"
        
        if content:
            # LangExtract enhancement for general URLs
            if platform == "general" and LANGEXTRACT_AVAILABLE and len(content) > 300:
                enhanced = await asyncio.get_event_loop().run_in_executor(None, enhance_with_langextract, content, url)
                if enhanced:
                    content = enhanced
                    method_used = f"{method_used}+LE"
            enrichments.append(content)
            summaries.append(f"✅ {url} → {method_used}")
            logger.info(f"URL 處理成功: {url} via {method_used}")
        else:
            summaries.append(f"⚠️ {url} → 無法抓取")
            logger.warning(f"URL 處理失敗: {url}")
    
    # 組裝增強訊息
    if enrichments:
        enriched_block = "\n\n---\n".join(enrichments)
        enhanced_text = (
            f"{text}\n\n"
            f"=== 以下是自動抓取的連結內容 ===\n\n"
            f"{enriched_block}\n\n"
            f"=== 連結內容結束 ===\n"
            f"請基於上述連結內容來回應使用者的訊息。"
        )
        return enhanced_text, summaries
    
    return text, summaries


# === 對話歷史 ===

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
        lines = ["=== 對話歷史脈絡 ==="]
        for i, msg in enumerate(self.messages):
            prefix = "User" if msg.role == "user" else "Claude"
            preview = msg.content[:500] + "..." if len(msg.content) > 500 else msg.content
            lines.append(f"[{i+1}] {prefix}: {preview}")
        lines.append("=== 當前指令 ===")
        return "\n".join(lines)
    
    def clear(self) -> None:
        self.messages.clear()
        logger.info("對話歷史已清空")
    
    def save(self, filepath: Path) -> None:
        try:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            data = {"messages": [asdict(m) for m in self.messages], "saved_at": datetime.now().isoformat()}
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存歷史失敗: {e}")
    
    @classmethod
    def load(cls, filepath: Path, max_rounds: int = 10):
        history = cls(max_rounds=max_rounds)
        try:
            if filepath.exists():
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                for msg_data in data.get("messages", []):
                    history.messages.append(Message(**msg_data))
                logger.info(f"已載入 {len(history.messages)} 條歷史訊息")
        except Exception as e:
            logger.error(f"載入歷史失敗: {e}")
        return history


# === 主橋接器 ===

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
        return "對話歷史已清空。新的對話將不會包含之前的脈絡。"
    
    async def _cmd_show_history(self, chat_id: int) -> str:
        if not self.history.messages:
            return "目前沒有對話歷史。"
        lines = [f"對話歷史 ({len(self.history.messages)} 條):"]
        for i, msg in enumerate(self.history.messages):
            prefix = "User" if msg.role == "user" else "Claude"
            preview = msg.content[:100] + "..." if len(msg.content) > 100 else msg.content
            preview = preview.replace('\n', ' ')
            lines.append(f"[{i+1}] {prefix}: {preview}")
        return "\n".join(lines)
    
    async def _cmd_help(self, chat_id: int) -> str:
        # 動態顯示可用功能
        url_status = []
        url_status.append(f"  fxtwitter (X/Twitter): {'✅ 可用' if REQUESTS_AVAILABLE else '❌ 需要 requests'}")
        url_status.append(f"  yt-dlp (YouTube/通用): {'✅ 可用' if YTDLP_AVAILABLE else '❌ 未安裝'}")
        url_status.append(f"  HTTP fallback: {'✅ 可用' if REQUESTS_AVAILABLE else '❌ 需要 requests'}")
        url_block = "\n".join(url_status)
        
        return f"""Telegram Claude Bridge v2.4 指令說明

特殊指令：
/clear - 清空對話歷史
/history - 顯示目前的對話歷史摘要
/status - 顯示系統狀態
/help - 顯示此幫助訊息
/exec <cmd> - 直接執行 PowerShell 命令

一般使用：
直接輸入訊息即可與 Claude Code 對話。
系統會自動保留最近 {CONFIG['MAX_HISTORY_ROUNDS']} 輪對話作為上下文。

🔗 URL 自動處理（v2.4 新增）：
分享任何連結，系統會自動抓取內容並提供給 Claude：
- X/Twitter → fxtwitter API（快速）→ yt-dlp（備用）
- YouTube → yt-dlp
- 其他網站 → HTTP 抓取標題/描述

URL 處理器狀態：
{url_block}

Log 管理：
- 每日產生獨立 log 檔案
- 自動清理 {CONFIG['LOG_RETENTION_DAYS']} 天前的舊 log
"""
    
    async def _cmd_fetch(self, chat_id: int) -> str:
        # Fetch URL, analyze, save AI-friendly output
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
        enhanced_text, summaries = await preprocess_urls(url)
        fetched = enhanced_text if enhanced_text != url else "Could not fetch"
        fetch_prompt = "URL content:" + chr(10) + fetched + chr(10) + chr(10)
        if user_note:
            fetch_prompt += "User task: " + user_note + chr(10) + chr(10)
        fetch_prompt += "Provide comprehensive analysis in Traditional Chinese. "
        fetch_prompt += "Structure clearly. This will be shared with other AI models."
        response = await self.execute_claude(fetch_prompt)
        saved = await asyncio.get_event_loop().run_in_executor(
            None, save_fetch_output, url, fetched, response, user_note
        )
        if saved:
            return response + chr(10) + chr(10) + "---" + chr(10) + "Saved: " + saved
        return response
    
    async def _cmd_extract(self, chat_id: int) -> str:
        # Structured extraction on last assistant message
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
        status = "忙碌中" if self.is_busy else "待命"
        return f"""系統狀態 (v2.4)
歷史訊息數: {len(self.history.messages)}
歷史檔案: {CONFIG['HISTORY_FILE']}
最大保留輪數: {CONFIG['MAX_HISTORY_ROUNDS']}
工作目錄: {CONFIG['WORKING_DIR']}
Log 目錄: {CONFIG['LOG_DIR']}
Log 檔案數: {len(log_files)}
Log 保留天數: {CONFIG['LOG_RETENTION_DAYS']}
Claude 狀態: {status}

URL 處理器:
  fxtwitter: {'✅' if REQUESTS_AVAILABLE else '❌'}
  yt-dlp: {'✅' if YTDLP_AVAILABLE else '❌'}
  HTTP fallback: {'✅' if REQUESTS_AVAILABLE else '❌'}

當前時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""

    def _build_prompt_with_context(self, user_message: str) -> str:
        context = self.history.get_context_summary()
        safety_note = "安全限制：不要刪除重要檔案，不要修改系統設定。" if not CONFIG["ALLOW_DANGEROUS"] else ""
        if context:
            return f"{context}\n{user_message}\n\n{safety_note}\n請基於上述對話脈絡來理解和執行當前指令。回應請使用繁體中文。"
        return f"{user_message}\n\n{safety_note}\n回應請使用繁體中文。"

    async def execute_claude(self, prompt: str) -> str:
        if self.is_busy:
            return "Claude 正在處理另一個任務，請稍後再試..."
        self.is_busy = True
        try:
            full_prompt = self._build_prompt_with_context(prompt)
            logger.info(f"執行 Claude 指令：{prompt[:100]}...")
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
                result = f"錯誤：\n{error}"
            elif output:
                result = self._format_output(output)
            else:
                result = "任務完成（無輸出）"
            return result
        except asyncio.TimeoutError:
            return f"執行超時（{CONFIG['TIMEOUT']}秒）"
        except FileNotFoundError:
            return "找不到 Claude CLI。請確保已安裝 Claude Code。"
        except Exception as e:
            logger.error(f"Claude 執行錯誤：{e}")
            return f"執行錯誤：{str(e)}"
        finally:
            self.is_busy = False
    
    def _format_output(self, output: str) -> str:
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        output = ansi_escape.sub('', output)
        if len(output) > 3500:
            output = output[:3500] + "\n\n...(輸出已截斷)"
        return output
    
    async def handle_message(self, chat_id: int, text: str) -> Tuple[str, Optional[str]]:
        """
        處理訊息。回傳 (claude_response, url_status_msg)。
        url_status_msg 為 None 表示沒有 URL 處理。
        """
        text = text.strip()
        cmd = text.split()[0].lower() if text else ""
        if cmd in self.special_commands:
            return await self.special_commands[cmd](chat_id), None
        
        logger.info(f"收到訊息 (chat_id={chat_id}): {text[:100]}...")
        
        # === URL 預處理 ===
        enhanced_text, url_summaries = await preprocess_urls(text)
        
        url_status = None
        if url_summaries:
            url_status = "🔗 URL 處理結果:\n" + "\n".join(url_summaries)
            logger.info(f"URL 預處理完成: {url_summaries}")
        
        # 記錄原始訊息（不含抓取內容，避免歷史過長）
        self.history.add_user_message(text)
        
        # 傳送增強後的訊息給 Claude
        response = await self.execute_claude(enhanced_text)
        
        # Auto-save fetch output when URLs present
        if url_summaries:
            detected = detect_urls(text)
            if detected:
                fetch_url = detected[0][0]
                user_note = text.replace(fetch_url, "").strip()
                await asyncio.get_event_loop().run_in_executor(None, save_fetch_output, fetch_url, enhanced_text, response, user_note)
        self.history.add_assistant_message(response)
        self.history.save(CONFIG["HISTORY_FILE"])
        
        return response, url_status


# === Telegram Handlers ===

bridge = None

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not bridge.is_authorized(user.id):
        await update.message.reply_text(f"未授權的用戶\n你的 User ID: {user.id}")
        return
    
    url_features = []
    if REQUESTS_AVAILABLE:
        url_features.append("fxtwitter (X/Twitter)")
    if YTDLP_AVAILABLE:
        url_features.append("yt-dlp (YouTube/通用)")
    url_text = "、".join(url_features) if url_features else "未啟用"
    
    await update.message.reply_text(
        f"Telegram Claude Code 橋接器 v2.4\n\n"
        f"歡迎，{user.first_name}！\n\n"
        f"功能：\n"
        f"- 自動保留最近 {CONFIG['MAX_HISTORY_ROUNDS']} 輪對話\n"
        f"- 每日獨立 log，自動清理 {CONFIG['LOG_RETENTION_DAYS']} 天前的舊檔\n"
        f"- 🆕 URL 自動抓取: {url_text}\n\n"
        f"輸入 /help 查看所有指令。"
    )

async def exec_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bridge.is_authorized(update.effective_user.id):
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
        if len(output) > 3500:
            output = output[:3500] + "\n...(已截斷)"
        status = "成功" if result.returncode == 0 else "失敗"
        await update.message.reply_text(f"{status} 結果：\n{output}")
    except subprocess.TimeoutExpired:
        await update.message.reply_text("執行超時")
    except Exception as e:
        await update.message.reply_text(f"錯誤：{e}")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not bridge.is_authorized(user_id):
        await update.message.reply_text(f"未授權\n你的 User ID: {user_id}")
        return
    
    text = update.message.text
    
    # 偵測是否包含 URL，給不同的等待訊息
    urls = detect_urls(text)
    if urls:
        url_list = ", ".join([u[0][:40] + "..." if len(u[0]) > 40 else u[0] for u, _ in [urls[0]]])
        processing_msg = await update.message.reply_text(
            f"🔗 偵測到連結，正在抓取內容...\n{text[:50]}{'...' if len(text) > 50 else ''}"
        )
    else:
        processing_msg = await update.message.reply_text(
            f"Claude 正在處理...\n{text[:50]}{'...' if len(text) > 50 else ''}"
        )
    
    result, url_status = await bridge.handle_message(update.effective_chat.id, text)
    
    try:
        await processing_msg.delete()
    except:
        pass
    
    # 如果有 URL 處理結果，先發一條狀態訊息
    if url_status:
        await update.message.reply_text(url_status)
    
    # 發送 Claude 回應
    if len(result) > 4000:
        chunks = [result[i:i+4000] for i in range(0, len(result), 4000)]
        for i, chunk in enumerate(chunks):
            await update.message.reply_text(f"[{i+1}/{len(chunks)}]\n\n{chunk}")
    else:
        await update.message.reply_text(f"Claude 回應：\n\n{result}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"發生錯誤：{context.error}")
    if update and update.message:
        await update.message.reply_text("發生錯誤，請查看日誌")

def find_claude_cli():
    paths = [
        CONFIG["CLAUDE_CLI"], "claude",
        os.path.expandvars(r"%APPDATA%\npm\claude.cmd"),
        r"C:\Users\USER\AppData\Roaming\npm\claude.cmd"
    ]
    for path in paths:
        try:
            result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10, shell=True)
            if result.returncode == 0:
                logger.info(f"找到 Claude CLI：{path}")
                return path
        except:
            continue
    return None

def main():
    global bridge
    
    logger.info("=" * 50)
    logger.info("啟動 Telegram Claude Code 橋接器 v2.4")
    logger.info("=" * 50)
    
    cleanup_old_logs()
    
    if not TELEGRAM_LIB_AVAILABLE:
        print("錯誤: 請先安裝 python-telegram-bot")
        sys.exit(1)
    
    claude_path = find_claude_cli()
    if claude_path:
        CONFIG["CLAUDE_CLI"] = claude_path
    else:
        logger.error("找不到 Claude CLI！")
        return
    
    bridge = ClaudeBridge()
    
    application = Application.builder().token(CONFIG["TELEGRAM_BOT_TOKEN"]).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("exec", exec_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    application.add_error_handler(error_handler)
    
    logger.info(f"歷史訊息數：{len(bridge.history.messages)}")
    logger.info(f"最大保留輪數：{CONFIG['MAX_HISTORY_ROUNDS']}")
    logger.info(f"Log 目錄：{CONFIG['LOG_DIR']}")
    logger.info(f"Log 保留天數：{CONFIG['LOG_RETENTION_DAYS']}")
    logger.info(f"URL 處理器: fxtwitter={'✅' if REQUESTS_AVAILABLE else '❌'}, yt-dlp={'✅' if YTDLP_AVAILABLE else '❌'}")
    logger.info("Bot 已啟動，等待 Telegram 訊息...")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
