"""
vision.py — 通用圖片理解模組
==============================
平台無關的圖片下載 + Gemini Vision 分析服務。
接收圖片 URL list + 可選 context，回傳文字描述。
不知道 Twitter / YouTube / 任何平台的存在。
"""

import os
import base64
import logging
from typing import Optional, List, Tuple

logger = logging.getLogger(__name__)

# --- 可用性檢測 ---

try:
    import requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

try:
    import google.generativeai as genai
    _genai_api_key = os.getenv('GOOGLE_API_KEY')
    if _genai_api_key:
        genai.configure(api_key=_genai_api_key)
        GENAI_AVAILABLE = True
        logger.info("google.generativeai 可用，圖片分析功能就緒")
    else:
        GENAI_AVAILABLE = False
        logger.info("google.generativeai 可用但 GOOGLE_API_KEY 未設定，圖片分析功能停用")
except ImportError:
    GENAI_AVAILABLE = False
    genai = None
    logger.info("google.generativeai 未安裝，圖片分析功能停用")

# --- 預設值（可被外部 config 覆蓋）---

_DEFAULT_CONFIG = {
    "IMAGE_ANALYSIS_ENABLED": True,
    "MAX_IMAGES_PER_MESSAGE": 5,
    "IMAGE_ANALYSIS_TIMEOUT": 30,
}


# === 核心函式 ===

def download_image_to_base64(image_url: str, timeout: int = 30) -> Optional[Tuple[str, str]]:
    """
    下載圖片到記憶體並轉換為 base64。
    回傳 (base64_data, mime_type) 或 None。
    不寫入磁碟，全程在記憶體中處理。
    """
    if not _REQUESTS_AVAILABLE:
        return None
    try:
        logger.info(f"[image] 下載圖片: {image_url[:80]}")
        resp = requests.get(
            image_url,
            timeout=timeout,
            headers={"User-Agent": "TelegramClaudeBridge/2.6"},
        )
        if resp.status_code != 200:
            logger.warning(f"[image] HTTP {resp.status_code} for {image_url[:80]}")
            return None

        content_type = resp.headers.get("Content-Type", "image/jpeg")
        if "png" in content_type:
            mime_type = "image/png"
        elif "gif" in content_type:
            mime_type = "image/gif"
        elif "webp" in content_type:
            mime_type = "image/webp"
        else:
            mime_type = "image/jpeg"

        image_bytes = resp.content
        if len(image_bytes) < 1000:
            logger.warning(f"[image] 圖片太小 ({len(image_bytes)} bytes)，跳過")
            return None
        if len(image_bytes) > 20 * 1024 * 1024:
            logger.warning(f"[image] 圖片太大 ({len(image_bytes)} bytes)，跳過")
            return None

        b64_data = base64.b64encode(image_bytes).decode('utf-8')
        logger.info(f"[image] 下載成功，{len(image_bytes)} bytes, {mime_type}")
        return b64_data, mime_type

    except requests.Timeout:
        logger.warning(f"[image] 下載超時: {image_url[:80]}")
        return None
    except Exception as e:
        logger.error(f"[image] 下載錯誤: {e}")
        return None


def describe_image_via_gemini(b64_data: str, mime_type: str, context: str = "") -> Optional[str]:
    """
    使用 Gemini 2.0 Flash Vision API 描述單張圖片。
    context: 可選的上下文提示（例如推文文字），幫助 Gemini 更好理解圖片。
    回傳圖片描述文字或 None。
    """
    if not GENAI_AVAILABLE:
        return None
    try:
        model = genai.GenerativeModel('gemini-2.0-flash')

        if context:
            prompt_text = (
                f"這張圖片來自一則社群媒體貼文，貼文內容為：{context[:500]}\n\n"
                "請根據上下文，詳細描述圖片中的內容。"
                "包含圖片中可見的所有文字、數據、圖表或視覺資訊。"
                "請使用繁體中文回答。"
            )
        else:
            prompt_text = (
                "請詳細描述這張圖片的內容。"
                "包含圖片中可見的所有文字、數據、圖表或視覺資訊。"
                "請使用繁體中文回答。"
            )

        response = model.generate_content([
            prompt_text,
            {"mime_type": mime_type, "data": b64_data}
        ])
        description = response.text.strip()

        if description:
            logger.info(f"[image] Gemini 描述成功，{len(description)} 字元")
            return description
        return None

    except Exception as e:
        logger.error(f"[image] Gemini 分析錯誤: {e}")
        return None


def analyze_images(image_urls: List[str], context: str = "", config: dict = None) -> Optional[str]:
    """
    通用圖片分析模組（平台無關）。
    接收圖片 URL 列表，下載並透過 Gemini Vision 分析。
    回傳合併的圖片描述文字，或 None（若全部失敗）。

    config: 可選的設定 dict，支援鍵：
        IMAGE_ANALYSIS_ENABLED, MAX_IMAGES_PER_MESSAGE, IMAGE_ANALYSIS_TIMEOUT
    """
    cfg = config if config is not None else _DEFAULT_CONFIG

    if not cfg.get("IMAGE_ANALYSIS_ENABLED", False):
        logger.info("[image] 圖片分析功能已停用")
        return None

    if not GENAI_AVAILABLE:
        logger.info("[image] Gemini 不可用，跳過圖片分析")
        return None

    if not image_urls:
        return None

    max_images = cfg.get("MAX_IMAGES_PER_MESSAGE", 5)
    timeout = cfg.get("IMAGE_ANALYSIS_TIMEOUT", 30)
    urls_to_process = image_urls[:max_images]
    logger.info(f"[image] 開始分析 {len(urls_to_process)} 張圖片")

    descriptions = []
    for i, img_url in enumerate(urls_to_process):
        dl_result = download_image_to_base64(img_url, timeout=timeout)
        if dl_result is None:
            descriptions.append(f"[圖片 {i+1}] 下載失敗，無法分析")
            continue

        b64_data, mime_type = dl_result
        desc = describe_image_via_gemini(b64_data, mime_type, context)
        if desc:
            descriptions.append(f"[圖片 {i+1}] {desc}")
        else:
            descriptions.append(f"[圖片 {i+1}] 分析失敗，無法取得描述")

    if not descriptions:
        return None

    header = f"📷 圖片分析結果（共 {len(urls_to_process)} 張）:"
    result = header + "\n\n" + "\n\n".join(descriptions)
    logger.info(f"[image] 分析完成，{len(descriptions)} 張圖片，{len(result)} 字元")
    return result
