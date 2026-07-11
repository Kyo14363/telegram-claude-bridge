"""
url_fetchers.py — URL 抓取與平台解析模組
==========================================
包含 URL 偵測、平台特定 fetcher、LangExtract 增強、
以及 preprocess_urls() 編排函式。
"""

import os
import re
import asyncio
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple

logger = logging.getLogger(__name__)


# --- 重試邏輯 ---

def retry_fetch(func, *args, max_retries: int = 2, backoff: float = 1.0, **kwargs):
    """
    通用重試包裝器，用於 URL fetcher 函式。
    首次失敗後最多重試 max_retries 次，每次等待 backoff 秒（線性遞增）。
    回傳 (result, attempts, elapsed_sec)。
    """
    last_error = None
    start = time.monotonic()
    for attempt in range(1 + max_retries):
        try:
            result = func(*args, **kwargs)
            elapsed = time.monotonic() - start
            if result is not None:
                if attempt > 0:
                    logger.info(f"[retry] {func.__name__} 第 {attempt + 1} 次嘗試成功")
                return result, attempt + 1, elapsed
            # result is None 不算異常，但也不算成功 — 嘗試下一次
            last_error = "returned None"
        except Exception as e:
            last_error = str(e)
            logger.warning(f"[retry] {func.__name__} 第 {attempt + 1} 次失敗: {e}")
        if attempt < max_retries:
            wait = backoff * (attempt + 1)
            time.sleep(wait)
    elapsed = time.monotonic() - start
    return None, 1 + max_retries, elapsed

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

# v3.1 P0：trafilatura 正文抽取（取代純 HTML title/desc fallback 的薄輸出）
try:
    import trafilatura
    TRAFILATURA_AVAILABLE = True
    logger.info(f"trafilatura {trafilatura.__version__} 可用，啟用為 general URL 主路徑")
except ImportError:
    TRAFILATURA_AVAILABLE = False
    logger.info("trafilatura 未安裝，general URL 將只能拿到 title/og:description")

# Fix#1 (2026-06-13)：PDF 抽取。arxiv.org/pdf 與一般 .pdf 之前被當 HTML 丟給
# trafilatura，只會吐回 %PDF 原始位元組。改用 PyMuPDF(fitz) 正規解析。
try:
    import fitz  # PyMuPDF
    PYMUPDF_AVAILABLE = True
    logger.info(f"PyMuPDF {fitz.__doc__.splitlines()[0] if fitz.__doc__ else ''} 可用，啟用 PDF 抽取路徑")
except ImportError:
    PYMUPDF_AVAILABLE = False
    logger.info("PyMuPDF 未安裝，PDF 連結將無法抽取正文")

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
    "github": [
        r"(?:https?://)?(?:www\.)?github\.com/[^/\s]+/[^/\s]+",
    ],
    "general": [
        r"https?://\S+",
    ],
}


# v3.1.6 P1：中文輸入「URL，說明」不加空格是常態，\S+ 會把全形標點與後續文字
# 吞進 URL → fetch 404 → 白白降級 Playwright fallback。
# 截斷規則：從第一個「全形標點」起砍掉尾巴（CJK 符號區 3000-303F、全形 ASCII 變體
# FF00-FF65、省略號/彎引號）。刻意不截漢字本身 — 原生中文路徑
# （如 zh.wikipedia.org/wiki/台灣）是合法 URL，requests 會自行編碼。
_FW_PUNCT_TAIL = re.compile(r"[　-〿＀-･…‘’“”].*$", re.DOTALL)


def _clean_url(url: str) -> str:
    url = _FW_PUNCT_TAIL.sub("", url)
    # ASCII 結尾標點剝除（「看 URL.」「URL,」等）；逐字元處理保留路徑內的同字元
    while url and url[-1] in ".,;:!?'\"":
        url = url[:-1]
    # 括號平衡：尾端多出的 ) 通常是「(見 URL)」的外括號；
    # 但 wikipedia 的 /Foo_(bar) 內有成對 ( 則保留
    while url.endswith(")") and url.count("(") < url.count(")"):
        url = url[:-1]
    return url


def detect_urls(text: str) -> List[Tuple[str, str]]:
    """
    從訊息中偵測 URL 並分類平台。
    回傳 [(url, platform), ...] 的列表。
    優先匹配特定平台，最後才匹配 general。
    """
    found = []
    found_urls = set()

    for platform in ["x_twitter", "youtube", "github"]:
        for pattern in PLATFORM_PATTERNS[platform]:
            for match in re.finditer(pattern, text):
                url = _clean_url(match.group(0))
                if len(url) < 10 or url.endswith("://"):
                    continue
                if url not in found_urls:
                    found_urls.add(url)
                    found.append((url, platform))

    for pattern in PLATFORM_PATTERNS["general"]:
        for match in re.finditer(pattern, text):
            url = _clean_url(match.group(0))
            if len(url) < 10 or url.endswith("://"):
                continue
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

        # 結構化 metadata 供 Obsidian 落地使用
        tweet_meta = {
            "platform": "x_twitter",
            "author_name": author.get("name", "") if author else "",
            "author_handle": author.get("screen_name", "unknown") if author else "unknown",
            "published": created[:10] if created else "",  # YYYY-MM-DD
            "tweet_text": text,
        }

        return result, image_urls, tweet_meta

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


# --- 方案 GitHub: GitHub API / Raw 專用 fetcher ---

def fetch_via_github_api(url: str, config: dict = None):
    """
    GitHub 專用 fetcher：透過 raw.githubusercontent.com / GitHub REST API
    抓取 repo README + metadata。
    回傳 (formatted_content, obsidian_meta) 或 None。
    """
    if not REQUESTS_AVAILABLE:
        return None

    cfg = config or {}
    fetch_timeout = cfg.get("URL_FETCH_TIMEOUT", 15)

    try:
        match = re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)", url)
        if not match:
            logger.warning(f"[github] 無法解析 owner/repo: {url}")
            return None

        owner = match.group(1)
        repo = match.group(2).rstrip("/")
        logger.info(f"[github] 解析到 {owner}/{repo}")

        headers = {
            "User-Agent": "TelegramClaudeBridge/2.6",
            "Accept": "application/vnd.github.v3+json",
        }
        gh_token = os.environ.get("GITHUB_TOKEN") or cfg.get("GITHUB_TOKEN")
        if gh_token:
            headers["Authorization"] = f"token {gh_token}"

        readme_content = None
        repo_meta = {}

        # 策略 1: raw.githubusercontent.com（快、無需 API rate limit）
        for branch in ["main", "master"]:
            for readme_name in ["README.md", "readme.md", "README.rst", "README"]:
                raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{readme_name}"
                try:
                    resp = requests.get(raw_url, timeout=fetch_timeout, headers={
                        "User-Agent": "TelegramClaudeBridge/2.6"
                    })
                    if resp.status_code == 200 and len(resp.text) > 50:
                        readme_content = resp.text
                        logger.info(f"[github] raw 成功: {raw_url} ({len(resp.text)} chars)")
                        break
                except Exception:
                    continue
            if readme_content:
                break

        # 策略 2: GitHub API fallback（需 base64 decode）
        if not readme_content:
            import base64
            api_url = f"https://api.github.com/repos/{owner}/{repo}/readme"
            try:
                resp = requests.get(api_url, timeout=fetch_timeout, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    readme_content = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
                    logger.info(f"[github] API 成功 ({len(readme_content)} chars)")
            except Exception as e:
                logger.warning(f"[github] API fallback 失敗: {e}")

        # 抓 repo metadata
        try:
            resp = requests.get(f"https://api.github.com/repos/{owner}/{repo}", timeout=fetch_timeout, headers=headers)
            if resp.status_code == 200:
                rdata = resp.json()
                repo_meta = {
                    "description": rdata.get("description", ""),
                    "stars": rdata.get("stargazers_count", 0),
                    "forks": rdata.get("forks_count", 0),
                    "language": rdata.get("language", ""),
                    "topics": rdata.get("topics", []),
                    "updated_at": rdata.get("updated_at", ""),
                    "license": (rdata.get("license") or {}).get("spdx_id", ""),
                }
        except Exception as e:
            logger.warning(f"[github] metadata 失敗: {e}")

        if not readme_content:
            logger.warning(f"[github] README 全部失敗: {url}")
            return None

        # 組裝輸出
        parts = []
        parts.append(f"📦 GitHub Repo: {url}")
        parts.append(f"📂 {owner}/{repo}")
        if repo_meta.get("description"):
            parts.append(f"📝 簡介: {repo_meta['description']}")
        if repo_meta.get("stars"):
            parts.append(f"⭐ Stars: {repo_meta['stars']:,} | 🍴 Forks: {repo_meta['forks']:,}")
        if repo_meta.get("language"):
            parts.append(f"💻 語言: {repo_meta['language']}")
        if repo_meta.get("license"):
            parts.append(f"📄 授權: {repo_meta['license']}")
        if repo_meta.get("topics"):
            parts.append(f"🏷️ Topics: {', '.join(repo_meta['topics'][:10])}")
        parts.append("")
        parts.append("--- README ---")
        parts.append("")

        max_len = cfg.get("GITHUB_README_MAX_LEN", 8000)
        if len(readme_content) > max_len:
            readme_content = readme_content[:max_len] + f"\n\n...(截斷，原始 {len(readme_content)} chars)"
        parts.append(readme_content)

        result = "\n".join(parts)
        logger.info(f"[github] 完成: {len(result)} chars")

        obsidian_meta = {
            "platform": "github",
            "author_name": owner,
            "author_handle": owner,
            "published": repo_meta.get("updated_at", "")[:10],
            "title": f"{owner}/{repo}" + (f" - {repo_meta['description']}" if repo_meta.get("description") else ""),
        }

        return result, obsidian_meta

    except Exception as e:
        logger.error(f"[github] 錯誤: {e}")
        return None



# --- v3.1 P0: trafilatura 正文抽取（general URL 主路徑） ---

# --- PDF 抽取（arxiv / 一般 .pdf）---

def _looks_like_pdf(url: str) -> bool:
    """URL 啟發式：是否可能指向 PDF。實際內容由 fetch_via_pdf 再以 magic bytes 確認。"""
    u = url.lower().split("?")[0].split("#")[0]
    if u.endswith(".pdf"):
        return True
    if "arxiv.org/pdf/" in u:
        return True
    return False


def _pdf_date_to_iso(raw: str) -> str:
    """PyMuPDF metadata 日期 'D:20260529120000Z' → '2026-05-29'；無法解析回空字串。"""
    if not raw:
        return ""
    m = re.search(r"(\d{4})(\d{2})(\d{2})", raw)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def fetch_via_pdf(url: str, config: dict = None) -> Optional[str]:
    """
    下載 PDF bytes 並用 PyMuPDF 抽純文字。專治 arxiv.org/pdf 與一般 .pdf —
    trafilatura 對這類連結只會吐回 %PDF 原始位元組（Fix#1, 2026-06-13）。
    非 PDF 內容（magic bytes 不符）回 None，交回 trafilatura 一般路徑。
    """
    if not (REQUESTS_AVAILABLE and PYMUPDF_AVAILABLE):
        return None
    cfg = config or {}
    fetch_timeout = cfg.get("URL_FETCH_TIMEOUT", 15)
    max_pages = cfg.get("PDF_MAX_PAGES", 40)
    try:
        logger.info(f"[pdf] 嘗試抓取: {url}")
        resp = requests.get(url, timeout=fetch_timeout, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }, allow_redirects=True)
        if resp.status_code != 200:
            return None
        raw = resp.content
        ctype = resp.headers.get("Content-Type", "").lower()
        if not (raw[:5] == b"%PDF-" or "application/pdf" in ctype):
            logger.info(f"[pdf] 非 PDF 內容（ctype={ctype}），交回一般路徑")
            return None

        doc = fitz.open(stream=raw, filetype="pdf")
        try:
            page_count = doc.page_count
            pages = []
            for i, page in enumerate(doc):
                if i >= max_pages:
                    break
                pages.append(page.get_text())
            meta = doc.metadata or {}
        finally:
            doc.close()

        body = re.sub(r"\n{3,}", "\n\n", "\n".join(pages)).strip()
        if len(body) < 100:
            logger.info(f"[pdf] 抽到的文字過短（{len(body)} chars），可能是掃描影像 PDF，視為失敗")
            return None

        title = (meta.get("title") or "").strip()
        author = (meta.get("author") or "").strip()
        date_iso = _pdf_date_to_iso(meta.get("creationDate") or meta.get("modDate") or "")

        parts = [f"🔗 來源: {url}"]
        if title:
            parts.append(f"📌 標題: {title}")
        if author:
            parts.append(f"👤 作者: {author}")
        if date_iso:
            parts.append(f"📅 發布日期: {date_iso}")
        parts.append(f"📄 頁數: {page_count}（已抽 {min(page_count, max_pages)} 頁）")
        parts.append("")
        if len(body) > 12000:
            body = body[:12000] + "\n\n…(內容過長已截斷)"
        parts.append(body)
        result = "\n".join(parts)
        logger.info(f"[pdf] 成功抽取 {page_count} 頁，{len(result)} 字元")
        return result
    except Exception as e:
        logger.warning(f"[pdf] 失敗，將 fallback 到 trafilatura/http: {e}")
        return None


def fetch_via_trafilatura(url: str, config: dict = None) -> Optional[str]:
    """
    用 trafilatura 抽正文。對新聞站、Perplexity、Substack、各類 article 頁面
    比 fetch_via_http 的 title/og:description 質好上一個量級。
    """
    if not TRAFILATURA_AVAILABLE:
        return None
    cfg = config or {}
    fetch_timeout = cfg.get("URL_FETCH_TIMEOUT", 15)
    try:
        logger.info(f"[trafilatura] 嘗試抓取: {url}")
        # 自帶 fetch（內部用 urllib，吃 timeout 透過環境變數比較囉嗦，直接用 requests 拿 HTML 再交給 extract）
        if REQUESTS_AVAILABLE:
            resp = requests.get(url, timeout=fetch_timeout, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }, allow_redirects=True)
            if resp.status_code != 200:
                return None
            # 防護：若回應其實是 PDF（啟發式漏掉、或重導向到 PDF），不要把原始位元組
            # 當 HTML 抽取（會吐回 %PDF...）。改走 PDF 路徑或直接視為失敗。
            ctype = resp.headers.get("Content-Type", "").lower()
            if resp.content[:5] == b"%PDF-" or "application/pdf" in ctype:
                logger.info("[trafilatura] 偵測到 PDF 內容，改用 fetch_via_pdf")
                return fetch_via_pdf(url, config)
            html = resp.text
        else:
            html = trafilatura.fetch_url(url)
            if not html:
                return None

        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            include_links=False,
            favor_precision=True,
            url=url,
        )
        if not text or len(text.strip()) < 100:
            logger.info(f"[trafilatura] 內容過短或抽不到，視為失敗: {len(text) if text else 0} chars")
            return None

        # 順便抽 metadata（title / author / date）
        meta = trafilatura.extract_metadata(html)
        parts = [f"🔗 來源: {url}"]
        if meta:
            if meta.title:
                parts.append(f"📌 標題: {meta.title}")
            if meta.author:
                parts.append(f"👤 作者: {meta.author}")
            if meta.date:
                parts.append(f"📅 日期: {meta.date}")
        parts.append("")
        # 限制 12000 字元，避免 prompt 爆掉（多數新聞 5-8k 內）
        body = text.strip()
        if len(body) > 12000:
            body = body[:12000] + "\n\n…(內容過長已截斷)"
        parts.append(body)
        result = "\n".join(parts)
        logger.info(f"[trafilatura] 成功抽取正文，{len(result)} 字元")
        return result
    except Exception as e:
        logger.warning(f"[trafilatura] 失敗，將 fallback 到 fetch_via_http: {e}")
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

# Fix#3 (2026-06-13)：langextract 新版 API = 位置參數 text_or_documents +
# prompt_description + examples(few-shot) + model_id。舊碼的 text=/prompt=/model=
# 已被拒收（→ 每個 general URL 的結構化抽取靜默失敗、軟降級回原文）。
# 另：fetch_urls 預設 True 會去抓內文裡的 URL（網路地雷），一律關閉；
# show_progress 預設 True 會把進度條噴進 log，一併關閉。
_LX_MODEL = "gemini-2.5-flash"


def _lx_example():
    """共用 few-shot：示範要抽的類別（組織/人/產品/日期/數據/宣稱）。"""
    return lx.data.ExampleData(
        text="OpenAI released GPT-5 on 2025-03-01, reaching 100M users; CEO Sam Altman called it a milestone.",
        extractions=[
            lx.data.Extraction(extraction_class="organization", extraction_text="OpenAI"),
            lx.data.Extraction(extraction_class="product", extraction_text="GPT-5"),
            lx.data.Extraction(extraction_class="date", extraction_text="2025-03-01"),
            lx.data.Extraction(extraction_class="metric", extraction_text="100M users"),
            lx.data.Extraction(extraction_class="person", extraction_text="Sam Altman"),
            lx.data.Extraction(extraction_class="claim", extraction_text="called it a milestone"),
        ],
    )


def _lx_run(text, prompt_description):
    """以新版 langextract API 抽取，回傳格式化條列字串（無結果回 None）。"""
    res = lx.extract(
        text,
        prompt_description=prompt_description,
        examples=[_lx_example()],
        model_id=_LX_MODEL,
        api_key=os.getenv("GOOGLE_API_KEY"),
        fetch_urls=False,
        show_progress=False,
    )
    exts = getattr(res, "extractions", None) or []
    lines = []
    for e in exts:
        cls = (getattr(e, "extraction_class", "") or "").strip()
        txt = (getattr(e, "extraction_text", "") or "").strip()
        if txt:
            lines.append(f"- {cls}: {txt}" if cls else f"- {txt}")
    return "\n".join(lines) if lines else None


def enhance_with_langextract(raw_content, url):
    """Use LangExtract to extract structured info from fetched web content."""
    if not LANGEXTRACT_AVAILABLE or len(raw_content) < 200:
        return None
    try:
        if not os.getenv('GOOGLE_API_KEY'):
            return None
        logger.info(f'[langextract] extracting ({len(raw_content)} chars)...')
        result_text = _lx_run(
            raw_content[:5000],
            'Extract key information: organizations, people, products, dates, '
            'numeric metrics/stats, and the main claims/conclusions.',
        )
        if not result_text or len(result_text) < 50:
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
        result_text = _lx_run(text[:8000], prompt or dp)
        if result_text:
            return 'LangExtract result:' + chr(10) + chr(10) + result_text[:3000]
        return 'Extraction complete but no results'
    except Exception as e:
        return f'Extraction failed: {e}'


# --- Obsidian 落地 ---

def _extract_meta_from_content(content: str, url: str, platform: str) -> dict:
    """從抓取內容中提取結構化 metadata，供非 X/Twitter URL 的 Obsidian 落地使用。"""
    meta = {"platform": platform, "author_handle": "", "author_name": "", "published": "", "tweet_text": ""}

    for line in content.split("\n"):
        if line.startswith("📌 標題:") or line.startswith("📌 OG 標題:"):
            meta["title"] = line.split(":", 1)[1].strip()
        elif line.startswith("👤 作者") or line.startswith("👤 頻道"):
            meta["author_name"] = line.split(":", 1)[1].strip()
        elif line.startswith("📅 發布日期:") or line.startswith("📅 發布時間:"):
            date_str = line.split(":", 1)[1].strip()
            # 嘗試取前 10 字元作為 YYYY-MM-DD
            if len(date_str) >= 10:
                meta["published"] = date_str[:10]

    # fallback title: 從 URL 取 domain + path
    if "title" not in meta:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        meta["title"] = f"{parsed.netloc}{parsed.path}"

    return meta


def save_to_obsidian(url: str, fetched_content: str, claude_response: str,
                     meta: dict = None, config: dict = None,
                     replies_section: str = "",
                     screenshots: list = None) -> Optional[str]:
    """
    將任意 URL 內容以 Obsidian Web Clipper 相容格式寫入 Obsidian vault。
    支援 X/Twitter（有結構化 tweet_meta）和一般 URL（從內容提取 metadata）。
    落地位置由 config OBSIDIAN_MOBILE_DIR 決定。

    v3.1：screenshots 參數接受 [(bytes, media_type), ...]，會把 Playwright
    截圖寫到 vault 的 attachments 子目錄並用 markdown image 語法嵌入筆記。
    """
    cfg = config or {}
    obsidian_dir = Path(cfg.get("OBSIDIAN_MOBILE_DIR", Path(__file__).resolve().parent / "obsidian_clippings"))

    try:
        obsidian_dir.mkdir(parents=True, exist_ok=True)

        m = meta or {}
        platform = m.get("platform", "x_twitter")
        author_name = m.get("author_name", "")
        author_handle = m.get("author_handle", "")
        published = m.get("published", "")
        now = datetime.now().strftime("%Y-%m-%d")

        # --- Title & Author 依平台決定 ---
        if platform == "x_twitter":
            title = f"{author_name} (@{author_handle})" if author_name else f"Thread by @{author_handle}"
            author_field = f'"[[{author_handle}]]"'
        else:
            title = m.get("title", url)
            # 清理 title 中的雙引號避免 YAML 壞掉
            title = title.replace('"', "'")
            author_field = f'"{author_name}"' if author_name else '""'

        # --- 平台 tag ---
        platform_tag = {
            "x_twitter": "x-twitter",
            "youtube": "youtube",
            "general": "web",
        }.get(platform, "web")

        # --- 構建 frontmatter ---
        lines = []
        lines.append("---")
        lines.append(f'title: "{title}"')
        lines.append(f'source: "{url}"')
        lines.append(f"author: {author_field}")
        if published:
            lines.append(f"published: {published}")
        lines.append(f"created: {now}")
        lines.append("tags:")
        lines.append('  - "clippings"')
        lines.append('  - "via-telegram"')
        lines.append(f'  - "{platform_tag}"')
        lines.append("---")
        lines.append("")

        # 原文內容
        lines.append(fetched_content)

        # Claude 分析（以水平線隔開）
        if claude_response:
            lines.append("")
            lines.append("---")
            lines.append("")
            lines.append("## Claude 初步分析")
            lines.append("")
            lines.append(claude_response)

        # 精選回覆（由 reply_fetcher 或 Playwright fallback 產生）
        if replies_section:
            lines.append(replies_section)

        # --- 檔名：依平台用不同策略 ---
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if platform == "x_twitter" and author_handle:
            safe_name = re.sub(r"[^a-zA-Z0-9_]", "", author_handle)
        else:
            # 從 URL 取 domain 作為檔名前綴
            from urllib.parse import urlparse
            domain = urlparse(url).netloc.replace("www.", "")
            safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", domain)
        filename = f"{safe_name}_{ts}.md"
        filepath = obsidian_dir / filename

        # v3.1：把 Playwright 截圖寫到 vault 並嵌入 markdown
        if screenshots:
            attach_dir = obsidian_dir / "attachments"
            attach_dir.mkdir(parents=True, exist_ok=True)
            lines.append("")
            lines.append("---")
            lines.append("")
            lines.append(f"## 截圖 ({len(screenshots)} 張)")
            lines.append("")
            for i, (data, media_type) in enumerate(screenshots, 1):
                ext = "png"
                if "jpeg" in media_type or "jpg" in media_type:
                    ext = "jpg"
                elif "webp" in media_type:
                    ext = "webp"
                img_name = f"{safe_name}_{ts}_{i}.{ext}"
                img_path = attach_dir / img_name
                try:
                    with open(img_path, "wb") as f:
                        f.write(data)
                    # 相對路徑（Obsidian 從 note 位置解析）
                    lines.append(f"![[attachments/{img_name}]]")
                    lines.append("")
                except Exception as e:
                    logger.warning(f"[obsidian] 寫入截圖失敗 {img_name}: {e}")

        content = "\n".join(lines)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info(f"[obsidian] Saved to vault: {filepath} ({len(content)} chars)")
        return str(filepath)

    except Exception as e:
        logger.error(f"[obsidian] Save to Obsidian failed: {e}")
        return None


def save_fetch_output(url, fetched_content, claude_response, user_note="", config: dict = None):
    """Save AI-friendly markdown summary to fetch_outputs/."""
    cfg = config or {}
    output_dir = cfg.get("FETCH_OUTPUT_DIR", Path(__file__).resolve().parent / "fetch_outputs")

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

async def preprocess_urls(text: str, config: dict = None,
                         metrics=None) -> Tuple[str, List[str]]:
    """
    偵測訊息中的 URL，自動抓取內容，回傳增強後的訊息。

    策略：
    - X/Twitter: fxtwitter (方案D) → yt-dlp (方案C) → http fallback
    - YouTube/其他 yt-dlp 支援平台: yt-dlp (方案C) → http fallback
    - 其他 URL: http fallback

    回傳: (增強後的完整訊息, 處理摘要列表, obsidian_queue)
    """
    cfg = config or {}
    urls = detect_urls(text)

    if not urls:
        return text, [], []

    logger.info(f"偵測到 {len(urls)} 個 URL: {urls}")

    enrichments = []
    summaries = []
    obsidian_queue = []  # 收集需要落地 Obsidian 的內容 (url, content, meta)
    max_retries = cfg.get("FETCH_MAX_RETRIES", 2)

    for url, platform in urls:
        content = None
        method_used = None
        fetch_elapsed = 0.0

        if platform == "x_twitter":
            # X/Twitter: fxtwitter (回傳 3-tuple) → yt-dlp → http (with retry)
            fxt_result, attempts, fetch_elapsed = await asyncio.get_event_loop().run_in_executor(
                None, retry_fetch, fetch_via_fxtwitter, url, cfg,
            )
            if fxt_result is not None:
                content, image_urls, tweet_meta = fxt_result
                method_used = "fxtwitter"
                if attempts > 1:
                    method_used += f"(retry:{attempts})"
                # 加入 Obsidian 落地佇列
                obsidian_queue.append((url, content, tweet_meta))

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
                        method_used = method_used.replace("fxtwitter", "fxtwitter+img")
            else:
                result, attempts, fetch_elapsed = await asyncio.get_event_loop().run_in_executor(
                    None, retry_fetch, fetch_via_ytdlp, url, cfg,
                )
                if result is not None:
                    content = result
                    method_used = "yt-dlp"
                    if attempts > 1:
                        method_used += f"(retry:{attempts})"

        elif platform == "youtube":
            result, attempts, fetch_elapsed = await asyncio.get_event_loop().run_in_executor(
                None, retry_fetch, fetch_via_ytdlp, url, cfg,
            )
            if result is not None:
                content = result
                method_used = "yt-dlp"
                if attempts > 1:
                    method_used += f"(retry:{attempts})"

        elif platform == "github":
            gh_result, attempts, fetch_elapsed = await asyncio.get_event_loop().run_in_executor(
                None, retry_fetch, fetch_via_github_api, url, cfg,
            )
            if gh_result is not None:
                content, gh_meta = gh_result
                method_used = "github-api"
                if attempts > 1:
                    method_used += f"(retry:{attempts})"
                obsidian_queue.append((url, content, gh_meta))

        # Fix#1 (2026-06-13)：PDF 主路徑（arxiv.org/pdf、一般 .pdf）。必須跑在
        # trafilatura 之前，否則 PDF 會被當 HTML 抽成 %PDF 原始位元組餵給 Claude。
        if not content and PYMUPDF_AVAILABLE and _looks_like_pdf(url):
            result, attempts, pdf_elapsed = await asyncio.get_event_loop().run_in_executor(
                None, retry_fetch, fetch_via_pdf, url, cfg,
            )
            fetch_elapsed += pdf_elapsed
            if result is not None:
                content = result
                method_used = "pdf"
                if attempts > 1:
                    method_used += f"(retry:{attempts})"

        # v3.1 P0：trafilatura 主路徑（general / 任何尚未抓到內容的 URL）
        # 對新聞站、Perplexity、Substack 等 article 頁面，比 fetch_via_http 厚很多
        if not content and TRAFILATURA_AVAILABLE:
            result, attempts, tf_elapsed = await asyncio.get_event_loop().run_in_executor(
                None, retry_fetch, fetch_via_trafilatura, url, cfg,
            )
            fetch_elapsed += tf_elapsed
            if result is not None:
                content = result
                method_used = "trafilatura"
                if attempts > 1:
                    method_used += f"(retry:{attempts})"

        # 通用 fallback (with retry) — title / og:description 兜底
        if not content:
            result, attempts, fb_elapsed = await asyncio.get_event_loop().run_in_executor(
                None, retry_fetch, fetch_via_http, url, cfg,
            )
            fetch_elapsed += fb_elapsed
            if result is not None:
                content = result
                method_used = "http"
                if attempts > 1:
                    method_used += f"(retry:{attempts})"

        if content:
            # LangExtract enhancement for general URLs（trafilatura 後也可再跑，正文乾淨度更好）
            # Fix#3 (2026-06-13)：預設關閉 — 對 Claude 下游而言這層 entity 抽取多半冗餘，
            # 且每個 general URL 會多一次 ~2-4s Gemini 呼叫。需要時設 LANGEXTRACT_AUTO_ENHANCE=True。
            if (platform == "general" and LANGEXTRACT_AVAILABLE
                    and cfg.get("LANGEXTRACT_AUTO_ENHANCE", False) and len(content) > 300):
                enhanced = await asyncio.get_event_loop().run_in_executor(None, enhance_with_langextract, content, url)
                if enhanced:
                    content = enhanced
                    method_used = f"{method_used}+LE"

            # v3.1 P0：內容過薄 → 注入 Playwright fallback 提示給 Claude
            # 200 字元門檻：典型 title+og 約 100-150 字，trafilatura 抽到正文通常 >1000 字
            thin_threshold = cfg.get("THIN_CONTENT_THRESHOLD", 200)
            if len(content) < thin_threshold and method_used in ("http", "trafilatura"):
                content += (
                    f"\n\n⚠️ 上述抓取內容過薄（{len(content)} 字元 < {thin_threshold}）。"
                    f"\n若需要完整內容，請呼叫 mcp__playwright__browser_navigate({url}) "
                    f"開瀏覽器補抓 JS 渲染後的正文，並在任務結束後 mcp__playwright__browser_close 釋放資源。"
                )
                method_used += "+thin-hint"

            enrichments.append(content)
            summaries.append(f"✅ {url} → {method_used}")
            logger.info(f"URL 處理成功: {url} via {method_used}")

            # 記錄指標
            if metrics:
                metrics.record_fetch(url, platform, method_used or "", True, fetch_elapsed)

            # 非特定平台的 URL 加入 Obsidian 落地佇列（x_twitter 和 github 已在各自分支處理）
            if platform not in ("x_twitter", "github"):
                generic_meta = _extract_meta_from_content(content, url, platform)
                obsidian_queue.append((url, content, generic_meta))
        else:
            summaries.append(f"⚠️ {url} → 無法抓取（將提示 Claude 改用 Playwright）")
            logger.warning(f"URL 處理失敗: {url}")
            if metrics:
                metrics.record_fetch(url, platform, "", False, fetch_elapsed)
                metrics.record_error("fetch", f"{url} ({platform}) 所有方案失敗")
            # v3.1.2：所有 fetcher 都失敗 → 明確注入 Playwright fallback 提示
            # 用於 Perplexity / Cloudflare / 反爬硬的網站，Claude 自動走 user-data-dir 已登入瀏覽器
            fallback_content = (
                f"🔗 來源: {url}\n\n"
                f"⚠️ 所有 HTTP 抓取方案都失敗（trafilatura / http fallback 全部 4xx 或被擋）。\n"
                f"請呼叫 mcp__playwright__browser_navigate({url}) 用已登入的真實瀏覽器抓取，\n"
                f"完成後若無後續任務請呼叫 mcp__playwright__browser_close 釋放資源。\n"
                f"若 Playwright 也拿不到，回報具體錯誤不要硬撐。"
            )
            enrichments.append(fallback_content)
            # v3.1.2 修補：之前漏了這行 — 全失敗分支也要進 Obsidian 落地佇列，
            # 否則 Claude 用 Playwright 抓到內容後，handle_message 不會 save_to_obsidian。
            # 給最小 placeholder meta（title 用 URL，作者空白），Claude 的回應會是內容主體。
            if platform not in ("x_twitter", "github"):
                from urllib.parse import urlparse
                domain = urlparse(url).netloc.replace("www.", "")
                placeholder_meta = {
                    "platform": platform,
                    "title": f"[via Playwright fallback] {domain}",
                    "author_name": "",
                }
                obsidian_queue.append((url, fallback_content, placeholder_meta))

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
        return enhanced_text, summaries, obsidian_queue

    return text, summaries, obsidian_queue
