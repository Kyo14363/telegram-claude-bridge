#!/usr/bin/env python3
"""
T-M-B Bridge Core — ClaudeBridge + CONFIG + infrastructure.

Extracted from telegram_bridge_claude_v3.1.py (Phase 1 module split, 2026-05-06).
Contains: CONFIG, logging, SDK lifecycle, ClaudeBridge class, session persistence.
"""

import os
import sys
import json
import asyncio
import subprocess
import threading
import re
import glob
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Tuple
from dataclasses import dataclass, field, asdict
import logging
from logging.handlers import TimedRotatingFileHandler

try:
    import psutil  # noqa: F401  — used by _terminate_descendants for /clear cleanup
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

# v3.1.2 Phase 1.5：CONFIG / 路徑 / Token redact filter / system_prompt 片段已抽至 config.py
from config import (
    CONFIG, VERSION, VERSION_LABEL,
    _TokenRedactFilter, SYSTEM_PROMPT_APPEND,
)


# === 日誌設定（每日輪換）===
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
logging.getLogger().addFilter(_TokenRedactFilter())

# === 外部模組 ===
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
from vision import GENAI_AVAILABLE
from reply_fetcher import (
    detect_reply_keywords, extract_tweet_id,
    fetch_tweet_replies, filter_replies_with_ai,
    format_replies_for_obsidian, format_replies_for_prompt,
    TWIKIT_AVAILABLE,
)
from metrics import Metrics, Timer


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

    def clear(self) -> None:
        self.messages.clear()
        logger.info("本地參考歷史已清空")

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
                logger.info(f"已載入 {len(history.messages)} 條本地參考歷史")
        except Exception as e:
            logger.error(f"載入歷史失敗: {e}")
        return history


# === Playwright 回覆抓取 fallback (v3.1) ===

def _build_playwright_reply_fallback(tweet_url: str, user_criteria: str, twikit_err: str) -> str:
    criteria = f"\n篩選條件：「{user_criteria}」" if user_criteria else ""
    return f"""

=== twikit 抓取失敗 → Playwright fallback ===
twikit 錯誤：{twikit_err}

請改用 mcp__playwright__browser_* 工具完成以下任務：
1. 導航到 {tweet_url}（使用既有已登入的 X/Twitter session）
2. 等待頁面載入；若有「顯示更多回覆」按鈕請點開（最多展 3 次）
3. 從 DOM 提取最多 30 則最有價值的回覆，每則含：
   - 作者 handle（@xxx）
   - 內文
   - 按讚數（若可見）
4. 自動排除純 emoji、純附和、廣告/spam{criteria}
5. 整理成繁體中文 markdown 區塊（### 1. **作者** (@handle) → 內文 → ❤️ 數）
6. 完成後（除非還有後續任務）呼叫 mcp__playwright__browser_close 釋放資源

如此頁面不存在或登入失效，回報具體狀況不要硬撐。
=== fallback 結束 ===
"""


# === Session 狀態持久化 ===

def load_session_id() -> Optional[str]:
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

class ClaudeBridge:
    def __init__(self):
        self.history = ConversationHistory.load(CONFIG["HISTORY_FILE"], CONFIG["MAX_HISTORY_ROUNDS"])
        self.is_busy = False
        self._exec_lock = asyncio.Lock()
        self.metrics = Metrics(CONFIG["BASE_DIR"] / "stats.json")
        self.sdk_client: Optional[ClaudeSDKClient] = None
        self.current_session_id: Optional[str] = load_session_id()
        self._sdk_loop: Optional[asyncio.AbstractEventLoop] = None
        self._sdk_thread: Optional[threading.Thread] = None
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_browser_activity: float = 0.0
        self._idle_watchdog_task: Optional[asyncio.Task] = None
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
                # 結構化片段組裝於 config.py：
                # Browser autoclose + Provenance protocol + Telegram output style (v3.1.2 P2)
                "append": SYSTEM_PROMPT_APPEND,
            },
            include_partial_messages=False,
            cli_path=str(cli_path) if cli_path else None,
            stderr=_cli_stderr,
        )

    # --- 跨 loop 派送 ---

    def _ensure_sdk_thread(self) -> None:
        if self._sdk_thread and self._sdk_thread.is_alive():
            return
        ready = threading.Event()

        def _run():
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
        if not self._sdk_loop:
            raise RuntimeError("SDK loop 尚未啟動")
        fut = asyncio.run_coroutine_threadsafe(coro, self._sdk_loop)
        return await asyncio.wrap_future(fut)

    # --- SDK 生命週期 ---

    async def start_sdk(self) -> None:
        if not SDK_AVAILABLE:
            raise RuntimeError("claude-agent-sdk 未安裝。請執行 pip install claude-agent-sdk")
        self._main_loop = asyncio.get_running_loop()
        self._ensure_sdk_thread()
        await self._submit_to_sdk(self._sdk_connect_impl(self.current_session_id))
        if self._idle_watchdog_task is None or self._idle_watchdog_task.done():
            self._idle_watchdog_task = asyncio.create_task(self._idle_watchdog_loop())

    async def _sdk_connect_impl(self, resume_id: Optional[str]) -> None:
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

    def _capture_sdk_subprocess_pid(self) -> Optional[int]:
        if not self.sdk_client:
            return None
        try:
            transport = getattr(self.sdk_client, "_transport", None)
            if transport is None:
                return None
            proc = getattr(transport, "_process", None)
            if proc is None:
                return None
            return proc.pid
        except Exception:
            return None

    @staticmethod
    def _terminate_descendants(parent_pid: Optional[int], grace_sec: float = 3.0) -> int:
        if not PSUTIL_AVAILABLE or parent_pid is None:
            return 0
        try:
            import psutil
            try:
                parent = psutil.Process(parent_pid)
            except psutil.NoSuchProcess:
                return 0
            descendants = parent.children(recursive=True)
            if not descendants:
                return 0
            for p in descendants:
                try:
                    p.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            gone, alive = psutil.wait_procs(descendants, timeout=grace_sec)
            for p in alive:
                try:
                    p.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            killed = len(descendants)
            logger.info(f"[clear] 清掃 SDK 子進程後代：{killed} 個 (claude.exe pid={parent_pid})")
            return killed
        except Exception as e:
            logger.warning(f"[clear] descendants 清掃異常（不影響主流程）: {e}")
            return 0

    async def _sdk_disconnect_impl(self) -> None:
        if self.sdk_client:
            await self.sdk_client.disconnect()
            self.sdk_client = None

    async def restart_sdk_with_new_session(self) -> None:
        watchdog_was_running = (
            self._idle_watchdog_task is not None and not self._idle_watchdog_task.done()
        )
        if watchdog_was_running:
            self._idle_watchdog_task.cancel()
            try:
                await self._idle_watchdog_task
            except (asyncio.CancelledError, Exception):
                pass

        old_pid = self._capture_sdk_subprocess_pid()

        if self.sdk_client:
            try:
                await self._submit_to_sdk(self._sdk_disconnect_impl())
            except Exception as e:
                logger.warning(f"舊 session disconnect 失敗（將忽略）: {e}")

        if old_pid is not None:
            self._terminate_descendants(old_pid)

        self.current_session_id = None
        clear_session_state()
        self._last_browser_activity = 0.0

        await self._submit_to_sdk(self._sdk_connect_impl(resume_id=None))
        logger.info("Claude SDK 已啟動全新 session")

        if watchdog_was_running:
            self._idle_watchdog_task = asyncio.create_task(self._idle_watchdog_loop())

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
        if not self.sdk_client:
            return "SDK 未連線。"
        prompt = (
            "Please call mcp__playwright__browser_close to close all open Playwright "
            "browsers right now to free CPU/memory. Reply with one short Chinese sentence "
            "confirming success or stating no browser was open."
        )
        try:
            resp = await self.execute_claude(prompt, progress_cb=None, photo_cb=None)
            self._last_browser_activity = 0.0
            return f"🛑 已請 Claude 關閉瀏覽器：\n{resp}"
        except Exception as e:
            return f"關閉失敗：{e}"

    async def _cmd_keyboard(self, chat_id: int) -> str:
        return "⌨️ 鍵盤已重新顯示。"

    # --- 閒置 watchdog ---

    async def _idle_watchdog_loop(self) -> None:
        check_interval = CONFIG.get("BROWSER_IDLE_CHECK_SEC", 60)
        idle_min = CONFIG.get("BROWSER_IDLE_CLOSE_MIN", 5)
        threshold_sec = idle_min * 60
        logger.info(f"[idle] watchdog 啟動：每 {check_interval}s 檢查，閾值 {idle_min} 分鐘")
        while True:
            try:
                await asyncio.sleep(check_interval)
                if self._last_browser_activity <= 0:
                    continue
                if self._exec_lock.locked():
                    continue
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
        url_block = "\n".join(url_status)

        img_enabled = CONFIG.get("IMAGE_ANALYSIS_ENABLED", False)
        if img_enabled and GENAI_AVAILABLE:
            img_status = f"✅ 啟用（Gemini，最多 {CONFIG['MAX_IMAGES_PER_MESSAGE']} 張/訊息）"
        elif img_enabled:
            img_status = "⚠️ 設定啟用但 Gemini 不可用"
        else:
            img_status = "❌ 停用"

        return f"""{VERSION_LABEL} 指令說明

🆕 v3.0：常駐 Claude Agent SDK，跨訊息保留 plan mode/todos/檔案 context/MCP server 連線

特殊指令：
/clear - 啟動全新 Claude session（重置所有上下文）
/session - 顯示當前 SDK session 狀態與 ID
/interrupt - 中斷正在跑的 Claude 任務
/browser-close - 立即關閉 Playwright Chrome 釋放資源
/keyboard - 重新顯示快捷鍵盤
/history - 顯示本地參考歷史（非 Claude 上下文）
/status - 顯示系統狀態
/stats - 顯示使用指標
/help - 顯示此幫助訊息
/exec <cmd> - 直接執行 PowerShell 命令（不經 Claude；不在快捷鍵盤上避免誤觸）

Shortcut（直接 PowerShell）：
/ps /cclog /tasklog /bridge /uptime

🆕 v3.1 變更：
• /fetch /extract 已廢除 — 直接傳 URL 即可，Claude 自己會用最佳工具
• twikit 失敗自動 fallback 到 Playwright（不再受困於 transaction.py 維護）
• Playwright 截圖同時推到 Telegram + 嵌入 Obsidian 筆記

🆕 v3.0 既有：
• 截圖直接以 Telegram 圖片回傳
• Playwright Chrome 閒置 5 分鐘自動關閉省電
• 12 鍵持久化快捷鍵盤（手機輸入區下方）

一般使用：
直接輸入訊息與 Claude 對話。session 跨訊息延續，
重啟 bridge 後也能 resume 同一條工作線（透過 session_state.json）。

🔗 URL 自動處理：
- X/Twitter → fxtwitter API → yt-dlp（備用）
- YouTube → yt-dlp
- 其他網站 → HTTP 抓取 + LangExtract 增強

📷 圖片分析：自動下載推文/Telegram 圖片並透過 Gemini Vision 分析

💬 回覆抓取（關鍵字觸發）：
觸發詞: {', '.join(CONFIG.get('REPLY_KEYWORDS', []))}
- twikit: {'✅ 可用' if TWIKIT_AVAILABLE else '❌ 未安裝'}

URL 處理器狀態：
{url_block}

📷 圖片分析: {img_status}
"""

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
💬 回覆抓取: {'✅ twikit 可用' if TWIKIT_AVAILABLE else '❌ twikit 未安裝'}

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
                        if block.name and block.name.startswith("mcp__playwright__"):
                            self._last_browser_activity = asyncio.get_running_loop().time()
                    elif isinstance(block, ThinkingBlock):
                        logger.debug("[SDK] thinking block")
            elif isinstance(msg, UserMessage):
                if msg.content:
                    proxy_progress("🔧 tool 完成，繼續處理...")
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

        # === 回覆抓取 ===
        reply_triggered, user_criteria = detect_reply_keywords(
            text, CONFIG.get("REPLY_KEYWORDS", [])
        )
        replies_section = ""
        replies_prompt_block = ""

        if reply_triggered and TWIKIT_AVAILABLE:
            detected = detect_urls(text)
            twitter_urls = [(u, p) for u, p in detected if p == "x_twitter"]
            if twitter_urls:
                tweet_url = twitter_urls[0][0]
                tweet_id = extract_tweet_id(tweet_url)
                if tweet_id:
                    logger.info(f"[reply] 觸發回覆抓取: tweet={tweet_id}, 條件='{user_criteria}'")
                    raw_replies, reply_err = await fetch_tweet_replies(
                        tweet_id,
                        str(CONFIG["TWIKIT_COOKIES"]),
                        max_count=CONFIG.get("REPLY_MAX_FETCH", 80),
                    )
                    if reply_err is None and raw_replies:
                        self.metrics.record_reply_fetch(success=True)
                        filtered = await filter_replies_with_ai(
                            raw_replies, user_criteria, CONFIG
                        )
                        replies_section = format_replies_for_obsidian(filtered)
                        replies_prompt_block = format_replies_for_prompt(filtered)
                        note = f"💬 回覆: 抓取 {len(raw_replies)} → 篩選 {len(filtered)} 則"
                        url_status = (url_status + "\n" + note) if url_status else note
                    elif reply_err is None and not raw_replies:
                        self.metrics.record_reply_fetch(success=True)
                        note = "💬 回覆: 此推文暫無回覆"
                        url_status = (url_status + "\n" + note) if url_status else note
                    else:
                        self.metrics.record_reply_fetch(success=False, error_code=reply_err)
                        err_msg_map = {
                            "cookies_invalid": "cookies 過期，請重跑 extract_cookies.bat",
                            "cookies_missing": "cookies.json 不存在",
                            "tweet_not_found": "找不到此推文（可能已刪除或受保護）",
                            "network_timeout": "網路逾時（已重試）",
                            "network_conn": "網路連線失敗（已重試）",
                            "twikit_api": "twikit API 異常（可能需要更新 twikit 或 patch transaction.py）",
                            "twikit_unavailable": "twikit 未安裝",
                        }
                        msg_detail = err_msg_map.get(reply_err, f"錯誤: {reply_err}")
                        replies_prompt_block = _build_playwright_reply_fallback(
                            tweet_url, user_criteria, msg_detail
                        )
                        note = f"💬 twikit 失敗（{msg_detail}），改由 Playwright fallback 抓取"
                        url_status = (url_status + "\n" + note) if url_status else note
        elif reply_triggered and not TWIKIT_AVAILABLE:
            detected = detect_urls(text)
            twitter_urls = [(u, p) for u, p in detected if p == "x_twitter"]
            if twitter_urls:
                replies_prompt_block = _build_playwright_reply_fallback(
                    twitter_urls[0][0], user_criteria, "twikit 未安裝"
                )
                note = "💬 twikit 不可用，改由 Playwright fallback 抓取"
            else:
                note = "💬 偵測到回覆抓取關鍵字但無 X/Twitter URL"
            url_status = (url_status + "\n" + note) if url_status else note

        if replies_prompt_block:
            enhanced_text += replies_prompt_block

        self.history.add_user_message(text)
        screenshot_collector: List[Tuple[bytes, str]] = []
        response = await self.execute_claude(
            enhanced_text, progress_cb=progress_cb, photo_cb=photo_cb,
            screenshot_collector=screenshot_collector,
        )

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

        self.history.add_assistant_message(response)
        self.history.save(CONFIG["HISTORY_FILE"])

        return response, url_status


# === Module-level singleton ===

bridge: Optional[ClaudeBridge] = None

def init_bridge() -> ClaudeBridge:
    global bridge
    bridge = ClaudeBridge()
    return bridge
