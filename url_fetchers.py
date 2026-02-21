"""
url_fetchers.py — URL 抓取與平台解析模組
==========================================
包含 URL 偵測、平台特定 fetcher、LangExtract 增強、
以及 preprocess_urls() 編排函式。
"""

import os
import re
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple

logger = logging.getLogger(__name__)

# --- 可用性檢測 ---

try:
    import yt_dlp
    YTDLP_AVAILABLE = True
    logger.info("yt-dlp 可用，已啟用作為備用 URL 處理器")
except ImportError:
    YTDLP_AVAILABLE = False
    logger.info("yt-dlp 未安裝，僅使用 fxtwitter/HTTP 方案處理 URL")

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

# vision 模組 — 延遲 import 避免循環依賴
from vision import analyze_images, GENAI_AVAILABLE


# --- URL 偵測與分類 ---

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

    for platform in ["x_twitter", "youtube"]:
        for pattern in PLATFORM_PATTERNS[platform]:
            for match in re.finditer(pattern, text):
                url = match.group(0)
                if url not in found_urls:
                    found_urls.add(url)
                    found.append((url, platform))

    for pattern in PLATFORM_PATTERNS["general"]:
        for match in re.finditer(pattern, text):
            url = match.group(0)
            if url not in found_urls:
                found_urls.add(url)
                found.append((url, "general"))

    return found


# --- 方案 D: fxtwitter (X/Twitter 專用) ---

def fetch_via_fxtwitter(url: str, config: dict = None) -> Optional[Tuple[str, List[str]]]:
    """
    用 fxtwitter.com API 抓取 X/Twitter 推文內容。
    將 x.com / twitter.com 替換成 api.fxtwitter.com 取得 JSON。
    回傳 (text_content, image_urls) tuple，或 None。
    """
    if not REQUESTS_AVAILABLE:
        return None

    cfg = config or {}
    fetch_timeout = cfg.get("URL_FETCH_TIMEOUT", 15)
    max_images = cfg.get("MAX_IMAGES_PER_MESSAGE", 5)

    try:
        api_url = re.sub(
            r"https?://(www\.)?(twitter\.com|x\.com)",
            "https://api.fxtwitter.com",
            url
        )

        logger.info(f"[fxtwitter] 嘗試抓取: {api_url}")

        resp = requests.get(api_url, timeout=fetch_timeout, headers={
            "User-Agent": "TelegramClaudeBridge/2.6"
        })

        if resp.status_code != 200:
            logger.warning(f"[fxtwitter] HTTP {resp.status_code}")
            return None

        data = resp.json()
        tweet = data.get("tweet", {})

        if not tweet:
            logger.warning("[fxtwitter] 回應中無 tweet 資料")
            return None

        parts = []
        parts.append(f"📌 推文來源: {url}")

        author = tweet.get("author", {})
        if author:
            parts.append(f"👤 作者: {author.get('name', '?')} (@{author.get('screen_name', '?')})")

        text = tweet.get("text", "")
        if text:
            parts.append(f"📝 內容:\n{text}")

        # Twitter Article（長文 / Notes）
        article = tweet.get("article")
        if article:
            article_title = article.get("title", "")
            if article_title:
                parts.append(f"📰 長文標題: {article_title}")
            # 解析 article content blocks
            content_blocks = article.get("content", {}).get("blocks", [])
            if content_blocks:
                article_texts = []
                for block in content_blocks:
                    block_text = block.get("text", "").strip()
                    if block_text:
                        block_type = block.get("type", "unstyled")
                        if block_type.startswith("header"):
                            article_texts.append(f"\n## {block_text}")
                        elif block_type == "blockquote":
                            article_texts.append(f"> {block_text}")
                        elif block_type in ("ordered-list-item", "unordered-list-item"):
                            article_texts.append(f"- {block_text}")
                        else:
                            article_texts.append(block_text)
                if article_texts:
                    article_body = "\n".join(article_texts)
                    parts.append(f"📝 長文內容:\n{article_body}")
                    logger.info(f"[fxtwitter] 解析到 Article，{len(content_blocks)} 個 blocks，{len(article_body)} 字元")

        # 媒體資訊
        media = tweet.get("media", {})
        image_urls = []
        if media:
            photos = media.get("photos", [])
            videos = media.get("videos", [])
            if photos:
                for photo in photos[:max_images]:
                    photo_url = photo.get("url")
                    if photo_url:
                        image_urls.append(photo_url)
                parts.append(f"🖼️ 包含 {len(photos)} 張圖片")
            if videos:
                # Twitter GIF 被歸類為 video（type="gif"），取其 thumbnail 進圖片分析
                gif_count = 0
                for video in videos:
                    if video.get("type") == "gif" and video.get("thumbnail_url"):
                        if len(image_urls) < max_images:
                            image_urls.append(video["thumbnail_url"])
                            gif_count += 1
                if gif_count:
                    parts.append(f"🎞️ 包含 {gif_count} 個 GIF（已擷取縮圖供分析）")
                real_video_count = len(videos) - gif_count
                if real_video_count > 0:
                    parts.append(f"🎬 包含 {real_video_count} 個影片")

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
        logger.info(f"[fxtwitter] 成功抓取推文，{len(result)} 字元，{len(image_urls)} 張圖片 URL")
        return result, image_urls

    except requests.Timeout:
        logger.warning(f"[fxtwitter] 請求超時")
        return None
    except Exception as e:
        logger.error(f"[fxtwitter] 錯誤: {e}")
        return None


# --- 方案 C: yt-dlp (通用備用) ---

def fetch_via_ytdlp(url: str, config: dict = None) -> Optional[str]:
    """
    用 yt-dlp 提取 URL 的 metadata（不下載檔案）。
    支援 X/Twitter、YouTube、TikTok 等上千個平台。
    """
    if not YTDLP_AVAILABLE:
        return None

    cfg = config or {}
    fetch_timeout = cfg.get("URL_FETCH_TIMEOUT", 15)

    try:
        logger.info(f"[yt-dlp] 嘗試抓取: {url}")

        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'socket_timeout': fetch_timeout,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return None

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

def fetch_via_http(url: str, config: dict = None) -> Optional[str]:
    """
    基本 HTTP GET，嘗試抓取頁面標題和 meta description。
    作為最後的 fallback。
    """
    if not REQUESTS_AVAILABLE:
        return None

    cfg = config or {}
    fetch_timeout = cfg.get("URL_FETCH_TIMEOUT", 15)

    try:
        logger.info(f"[http] 嘗試抓取: {url}")

        resp = requests.get(url, timeout=fetch_timeout, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }, allow_redirects=True)

        if resp.status_code != 200:
            return None

        content = resp.text[:10000]

        parts = [f"🔗 來源: {url}"]

        title_match = re.search(r'<title[^>]*>(.*?)</title>', content, re.IGNORECASE | re.DOTALL)
        if title_match:
            title = re.sub(r'\s+', ' ', title_match.group(1)).strip()
            parts.append(f"📌 標題: {title}")

        og_title = re.search(r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\'](.*?)["\']', content, re.IGNORECASE)
        og_desc = re.search(r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\'](.*?)["\']', content, re.IGNORECASE)
        meta_desc = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\'](.*?)["\']', content, re.IGNORECASE)

        if og_title:
            parts.append(f"📌 OG 標題: {og_title.group(1)}")

        desc = (og_desc and og_desc.group(1)) or (meta_desc and meta_desc.group(1))
        if desc:
            parts.append(f"📝 描述: {desc}")

        if len(parts) <= 1:
            return None

        result = "\n".join(parts)
        logger.info(f"[http] 成功抓取基本資訊，{len(result)} 字元")
        return result

    except Exception as e:
        logger.error(f"[http] 錯誤: {e}")
        return None


# --- LangExtract ---

def enhance_with_langextract(raw_content, url):
    """Use LangExtract to extract structured info from fetched web content."""
    if not LANGEXTRACT_AVAILABLE or len(raw_content) < 200:
        return None
    try:
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
    """/extract command: structured extraction on any text."""
    if not LANGEXTRACT_AVAILABLE:
        return 'LangExtract not installed'
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


# --- Fetch Output 儲存 ---

def save_fetch_output(url, fetched_content, claude_response, user_note="", config: dict = None):
    """Save AI-friendly markdown summary to fetch_outputs/."""
    cfg = config or {}
    output_dir = cfg.get("FETCH_OUTPUT_DIR", Path(r"C:\telegram-MCP-bridge\fetch_outputs"))

    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_url = re.sub(r"[^a-zA-Z0-9]", "_", url[:60])
        filename = f"fetch_{ts}_{safe_url}.md"
        filepath = output_dir / filename
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


# --- URL 預處理編排器 ---

async def preprocess_urls(text: str, config: dict = None) -> Tuple[str, List[str]]:
    """
    偵測訊息中的 URL，自動抓取內容，回傳增強後的訊息。

    策略：
    - X/Twitter: fxtwitter (方案D) → yt-dlp (方案C) → http fallback
    - YouTube/其他 yt-dlp 支援平台: yt-dlp (方案C) → http fallback
    - 其他 URL: http fallback

    回傳: (增強後的完整訊息, 處理摘要列表)
    """
    cfg = config or {}
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
            # X/Twitter: fxtwitter (回傳 tuple) → yt-dlp → http
            fxt_result = await asyncio.get_event_loop().run_in_executor(
                None, fetch_via_fxtwitter, url, cfg
            )
            if fxt_result is not None:
                content, image_urls = fxt_result
                method_used = "fxtwitter"

                # 層次二：通用圖片分析
                if image_urls:
                    tweet_text = ""
                    for line in content.split("\n"):
                        if line.startswith("📝 內容:"):
                            tweet_text = line.replace("📝 內容:", "").strip()
                            break
                    image_descriptions = await asyncio.get_event_loop().run_in_executor(
                        None, analyze_images, image_urls, tweet_text, cfg
                    )
                    if image_descriptions:
                        content = content + "\n\n" + image_descriptions
                        method_used = "fxtwitter+img"
            else:
                content = await asyncio.get_event_loop().run_in_executor(
                    None, fetch_via_ytdlp, url, cfg
                )
                if content:
                    method_used = "yt-dlp"

        elif platform == "youtube":
            content = await asyncio.get_event_loop().run_in_executor(
                None, fetch_via_ytdlp, url, cfg
            )
            if content:
                method_used = "yt-dlp"

        # 通用 fallback
        if not content:
            content = await asyncio.get_event_loop().run_in_executor(
                None, fetch_via_http, url, cfg
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
