"""
reply_fetcher.py — 推文回覆抓取與 AI 篩選模組
================================================
使用 twikit 抓取推文下方回覆，並透過 Gemini Flash 進行二次篩選。
此功能為關鍵字觸發，非預設行為。
"""

import re
import json
import logging
import asyncio
import traceback
from pathlib import Path
from typing import Optional, List, Dict, Tuple

logger = logging.getLogger(__name__)

# --- 可用性檢測 ---

try:
    from twikit import Client as TwikitClient
    TWIKIT_AVAILABLE = True
except ImportError:
    TWIKIT_AVAILABLE = False
    logger.info("twikit 未安裝，回覆抓取功能不可用")

try:
    from google import genai as _genai
    GENAI_AVAILABLE = True
except ImportError:
    _genai = None
    GENAI_AVAILABLE = False


# --- 關鍵字偵測 ---

# 寬鬆匹配的動詞 / 名詞集合。觸發條件：text 同時含任一動詞 AND 任一名詞。
# 設計依據：handoff.md 4/17、4/18 條目指出嚴格子字串匹配（如「收錄留言」）
# 中間插字就不觸發（如「收錄這推文中的精華留言」）。寬鬆規則保留 CONFIG
# REPLY_KEYWORDS 為向後相容路徑，同時補上自然語句的觸發。
_LOOSE_FETCH_VERBS = ("收錄", "抓取")
_LOOSE_REPLY_NOUNS = ("留言", "回覆")


def detect_reply_keywords(text: str, keywords: list) -> Tuple[bool, str]:
    """
    偵測訊息中是否包含回覆抓取觸發關鍵字。

    觸發規則（任一成立）：
    1. **嚴格**：text 含 ``keywords`` 任一完整字串（與舊版相容）。
    2. **寬鬆**：text 同時含 fetch 動詞（收錄/抓取）與 reply 名詞（留言/回覆）。

    回傳 ``(是否觸發, 去除關鍵字與 URL 後的剩餘文字作為使用者篩選條件)``。
    """
    matched_tokens: list = []

    # 1. 嚴格匹配（向後相容 CONFIG.REPLY_KEYWORDS）
    for kw in keywords:
        if kw in text:
            matched_tokens.append(kw)
            break

    # 2. 寬鬆匹配（動詞 + 名詞 共現）
    if not matched_tokens:
        verb_hit = next((v for v in _LOOSE_FETCH_VERBS if v in text), None)
        noun_hit = next((n for n in _LOOSE_REPLY_NOUNS if n in text), None)
        if verb_hit and noun_hit:
            matched_tokens.extend([verb_hit, noun_hit])

    if not matched_tokens:
        return False, ""

    # 提取使用者篩選條件：去除 URL、所有 matched tokens
    remainder = text
    remainder = re.sub(r"https?://\S+", "", remainder)
    for tok in matched_tokens:
        remainder = remainder.replace(tok, "")
    remainder = re.sub(r"[，、。,.\s]+", " ", remainder).strip()

    return True, remainder


# --- 從 URL 提取 tweet ID ---

def extract_tweet_id(url: str) -> Optional[str]:
    """從 X/Twitter URL 中提取 tweet ID。"""
    match = re.search(r"(?:twitter\.com|x\.com)/\w+/status/(\d+)", url)
    if match:
        return match.group(1)
    return None


# --- twikit 回覆抓取 ---

def _classify_error(exc: Exception) -> str:
    """
    將例外歸類為可操作的錯誤類別。未知類別回傳 "unknown:<ExceptionName>"
    以便 stats.json 的 recent_errors 能顯示具體型別，供後續根因分析。
    """
    name = type(exc).__name__
    msg = str(exc).lower()

    if "cookies" in msg or "401" in msg or "unauthorized" in msg or "forbidden" in msg \
            or "login" in msg or "authenticat" in msg:
        return "cookies_invalid"
    if "not found" in msg or "404" in msg or "does not exist" in msg:
        return "tweet_not_found"
    if "timeout" in msg or "timed out" in msg or name in ("TimeoutError", "ReadTimeout", "ConnectTimeout"):
        return "network_timeout"
    if name in ("ConnectionError", "ConnectionResetError", "ClientConnectorError"):
        return "network_conn"
    if name in ("AttributeError", "KeyError", "TypeError"):
        # 這類通常是 twikit API 對不上（X 前端 JS 變動 → transaction.py 破）
        return "twikit_api"
    return f"unknown:{name}"


async def fetch_tweet_replies(
    tweet_id: str,
    cookies_path: str,
    max_count: int = 80,
    max_retries: int = 2,
    backoff: float = 1.0,
) -> Tuple[List[Dict], Optional[str]]:
    """
    使用 twikit 抓取指定推文的回覆。

    回傳 (replies, error_code)：
      - 成功：(list_of_dicts, None)
      - 失敗：([], error_code) ；error_code 為 _classify_error 輸出

    在網路類錯誤上會以線性 backoff 重試 max_retries 次；cookies/API
    類錯誤不重試（重試也不會好）。每一次嘗試的 traceback 都寫入 log。
    """
    if not TWIKIT_AVAILABLE:
        logger.warning("[reply] twikit 不可用")
        return [], "twikit_unavailable"

    # 事前檢查：cookies 檔案
    cookies_file = Path(cookies_path)
    if not cookies_file.exists():
        logger.error(f"[reply] cookies 檔案不存在: {cookies_path}")
        return [], "cookies_missing"

    last_error_code: Optional[str] = None

    for attempt in range(1 + max_retries):
        try:
            result = await _fetch_once(cookies_file, tweet_id, max_count)
            if attempt > 0:
                logger.info(f"[reply] 第 {attempt + 1} 次嘗試成功")
            return result, None

        except Exception as e:
            err_code = _classify_error(e)
            last_error_code = err_code
            logger.error(
                f"[reply] 抓取失敗 (attempt {attempt + 1}/{1 + max_retries}) "
                f"[{err_code}] {type(e).__name__}: {e}"
            )
            logger.debug(f"[reply] traceback:\n{traceback.format_exc()}")

            # 不可重試類別：直接結束
            retryable = err_code.startswith("network_") or err_code.startswith("unknown")
            if not retryable or attempt >= max_retries:
                break

            wait = backoff * (attempt + 1)
            logger.info(f"[reply] 等待 {wait:.1f}s 後重試")
            await asyncio.sleep(wait)

    return [], last_error_code or "unknown:NoException"


async def _fetch_once(cookies_file: Path, tweet_id: str, max_count: int) -> List[Dict]:
    """單次抓取嘗試。把例外往上拋給 retry 邏輯分類。"""
    client = TwikitClient("zh-Hant")

    with open(cookies_file, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if isinstance(raw, dict) and "auth_token" in raw:
        client.set_cookies(raw)
        logger.info(f"[reply] 載入 Chrome cookies ({len(raw)} 個)")
    else:
        client.load_cookies(str(cookies_file))
        logger.info(f"[reply] 載入 twikit 格式 cookies")

    logger.info(f"[reply] 開始抓取 tweet {tweet_id} 的回覆")

    tweet = await client.get_tweet_by_id(tweet_id)
    if not tweet:
        # 讓 classifier 接住這個情境
        raise LookupError(f"tweet {tweet_id} not found")

    replies_result = await tweet.get_replies()

    all_replies: List[Dict] = []
    if replies_result:
        for reply in replies_result:
            all_replies.append(_parse_reply(reply))
            if len(all_replies) >= max_count:
                break

    while len(all_replies) < max_count and replies_result:
        try:
            next_result = await replies_result.next()
            if not next_result:
                break
            for reply in next_result:
                all_replies.append(_parse_reply(reply))
                if len(all_replies) >= max_count:
                    break
            replies_result = next_result
        except Exception as e:
            # 翻頁終止通常是正常結束（沒有下一頁），不上拋
            logger.warning(f"[reply] 翻頁結束或錯誤: {type(e).__name__}: {e}")
            break

    logger.info(f"[reply] 成功抓取 {len(all_replies)} 則回覆 (目標 {max_count})")
    return all_replies


def _parse_reply(reply) -> Dict:
    """將 twikit Tweet 物件轉為 dict。"""
    try:
        return {
            "author": getattr(reply.user, "screen_name", "unknown") if reply.user else "unknown",
            "author_name": getattr(reply.user, "name", "") if reply.user else "",
            "text": reply.text or "",
            "likes": getattr(reply, "favorite_count", 0) or 0,
            "reply_count": getattr(reply, "reply_count", 0) or 0,
            "created_at": str(getattr(reply, "created_at", "")),
        }
    except Exception:
        return {
            "author": "unknown",
            "author_name": "",
            "text": str(reply) if reply else "",
            "likes": 0,
            "reply_count": 0,
            "created_at": "",
        }


# --- 底線過濾 (模式 B) ---

def _baseline_filter(replies: List[Dict]) -> List[Dict]:
    """
    底線過濾：去除明顯無價值的回覆。
    - 純 emoji / 表情符號
    - 極短且無實質內容 (< 5 字元，扣除 @mention)
    - 常見無意義回覆模式
    """
    NOISE_PATTERNS = [
        r"^[\U0001F600-\U0001FAFF\u2600-\u27BF\uFE00-\uFE0F\u200D\s]+$",  # 純 emoji
        r"^(同意|推|讚|真的|確實|\+1|this|facts?|fr|real|w|W|L|lol|lmao)[\s!！。.]*$",
        r"^@\w+\s*$",  # 純 @mention 無內容
    ]

    filtered = []
    for reply in replies:
        text = reply.get("text", "").strip()
        # 去掉開頭的 @mention 再判斷長度
        text_no_mention = re.sub(r"^(@\w+\s*)+", "", text).strip()

        if len(text_no_mention) < 3:
            continue

        is_noise = False
        for pattern in NOISE_PATTERNS:
            if re.match(pattern, text_no_mention, re.IGNORECASE):
                is_noise = True
                break

        if not is_noise:
            filtered.append(reply)

    removed = len(replies) - len(filtered)
    if removed:
        logger.info(f"[reply] 底線過濾去除 {removed} 則無意義回覆")

    return filtered


# --- AI 篩選 (模式 A + B) ---

async def filter_replies_with_ai(
    replies: List[Dict],
    user_criteria: str = "",
    config: dict = None,
) -> List[Dict]:
    """
    使用 Gemini Flash 對回覆進行二次篩選。

    流程：
    1. 先做底線過濾 (模式 B)
    2. 再用 AI 根據用戶條件篩選 (模式 A)
    3. 如果 Gemini 不可用，僅回傳底線過濾結果
    """
    # Step 1: 底線過濾
    baseline_filtered = _baseline_filter(replies)

    if not baseline_filtered:
        return []

    # 如果 Gemini 不可用或沒有用戶條件且數量已經不多，直接回傳
    if not GENAI_AVAILABLE:
        logger.warning("[reply] Gemini 不可用，僅使用底線過濾")
        return baseline_filtered

    # Step 2: AI 篩選
    try:
        import os
        if not os.getenv("GOOGLE_API_KEY"):
            logger.warning("[reply] GOOGLE_API_KEY 未設定，跳過 AI 篩選")
            return baseline_filtered

        # 準備回覆資料給 AI
        replies_for_ai = []
        for i, r in enumerate(baseline_filtered):
            replies_for_ai.append({
                "index": i,
                "author": f"@{r['author']}",
                "text": r["text"][:300],  # 截斷避免 token 爆炸
                "likes": r["likes"],
            })

        criteria_block = ""
        if user_criteria:
            criteria_block = f"\n用戶額外篩選條件：「{user_criteria}」\n請特別根據此條件進行篩選。\n"

        prompt = f"""你是推文回覆篩選助手。以下是一則推文的 {len(replies_for_ai)} 則回覆。

請篩選出有實質內容、值得收錄的回覆。

去除標準：
- 純附和/灌水（如「真的」「太強了」「感謝分享」等無實質補充）
- spam / 廣告 / 自我推銷
- 與主題完全無關的閒聊
{criteria_block}
保留標準：
- 提供額外觀點、補充資訊、專業見解
- 提出有建設性的問題或質疑
- 分享相關經驗或案例
- 有實質討論價值的回覆

回覆資料：
{json.dumps(replies_for_ai, ensure_ascii=False, indent=1)}

請回傳 JSON array，僅包含你認為值得收錄的回覆 index 數字。
例如：[0, 3, 7, 12]
只回傳 JSON array，不要其他文字。"""

        client = _genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
        loop = asyncio.get_event_loop()

        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
            ),
        )

        if not response or not response.text:
            logger.warning("[reply] Gemini 回應為空，回傳底線過濾結果")
            return baseline_filtered

        # 解析 AI 回傳的 index array
        response_text = response.text.strip()
        # 嘗試從回應中提取 JSON array
        json_match = re.search(r"\[[\d\s,]*\]", response_text)
        if json_match:
            selected_indices = json.loads(json_match.group())
            selected = [
                baseline_filtered[i]
                for i in selected_indices
                if 0 <= i < len(baseline_filtered)
            ]
            logger.info(
                f"[reply] AI 篩選: {len(baseline_filtered)} → {len(selected)} 則 "
                f"(用戶條件: {'有' if user_criteria else '無'})"
            )
            return selected
        else:
            logger.warning(f"[reply] AI 回應格式異常: {response_text[:200]}")
            return baseline_filtered

    except Exception as e:
        logger.error(f"[reply] AI 篩選失敗: {e}")
        return baseline_filtered


# --- 格式化輸出 ---

def format_replies_for_obsidian(replies: List[Dict]) -> str:
    """將篩選後的回覆格式化為 Obsidian markdown 區塊。"""
    if not replies:
        return ""

    lines = []
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"## 精選回覆 ({len(replies)} 則)")
    lines.append("")

    for i, r in enumerate(replies, 1):
        author = r.get("author", "unknown")
        author_name = r.get("author_name", "")
        text = r.get("text", "").strip()
        likes = r.get("likes", 0)

        # 顯示格式
        name_display = f"**{author_name}** (@{author})" if author_name else f"@{author}"
        lines.append(f"### {i}. {name_display}")
        if likes:
            lines.append(f"> ❤️ {likes}")
        lines.append("")
        lines.append(text)
        lines.append("")

    return "\n".join(lines)


def format_replies_for_prompt(replies: List[Dict]) -> str:
    """將篩選後的回覆格式化為 Claude prompt 的一部分。"""
    if not replies:
        return ""

    lines = [f"\n=== 精選回覆 ({len(replies)} 則) ===\n"]
    for i, r in enumerate(replies, 1):
        author = r.get("author", "unknown")
        text = r.get("text", "").strip()
        likes = r.get("likes", 0)
        like_str = f" (❤️{likes})" if likes else ""
        lines.append(f"[{i}] @{author}{like_str}: {text}")
    lines.append("\n=== 回覆結束 ===")

    return "\n".join(lines)
