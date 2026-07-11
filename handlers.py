"""
T-M-B Telegram Handlers — message, photo, start, exec, unsupported, error.

Extracted from telegram_bridge_claude_v3.1.py (Phase 1 module split, 2026-05-06).
"""

import asyncio
import subprocess
import logging
import os
import json
import time
from pathlib import Path
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

# v3.2.2：/session /history 移除後縮為 11 鍵（末列 2 鍵）
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("/help"),    KeyboardButton("/status"),    KeyboardButton("/stats")],
        [KeyboardButton("/clear"),   KeyboardButton("/interrupt"), KeyboardButton("/browser-close")],
        [KeyboardButton("/ps"),      KeyboardButton("/uptime"),    KeyboardButton("/cclog")],
        [KeyboardButton("/bridge"),  KeyboardButton("/tasklog")],
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


# === /resume：headless 接續桌面 CC session（v3.2.6） ===
# 與常駐 B（普通訊息 → SDK）不同：resume 開一個一次性 `claude -p --resume <sid>`
# 進程，接續你桌面最近那條 Claude Code session 的同一份 transcript/todos，並 cd 回
# 原專案夾。實測見 config.py RESUME_* 說明。

# 記住上一次 resume 的 session/時間：resume 會 append 回同一個 jsonl，讓該檔 mtime
# 變新；若不記著，下一次 /resume 會把「自己剛寫的新鮮度」誤判成「桌面 A 還活著」而擋下。
_RESUME_STATE = {"last_sid": None, "last_ts": 0.0}


def _extract_cwd(jsonl_path: Path):
    """從 transcript 取原始 cwd（--resume 必須在對的 cwd 才找得到 session）。
    cwd 幾乎都在最前段的 user/assistant 行；用子字串預過濾避免逐行 json parse。"""
    try:
        with open(jsonl_path, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i > 500:
                    break
                if '"cwd"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("cwd"):
                    return d["cwd"]
    except Exception:
        pass
    return None


def _find_resume_target():
    """掃 ~/.claude/projects/*/*.jsonl，找最近活動、且非 bridge 自己 SDK 的桌面 session。
    回傳 (info, None) 或 (None, error)。info = {sid, cwd, slug, age_min}。
    排除 cwd == WORKING_DIR 的 transcript——那是常駐 B 自己的工作階段，不是桌面 A。"""
    proj = Path.home() / ".claude" / "projects"
    if not proj.exists():
        return None, f"找不到 Claude Code transcripts 目錄：{proj}"
    candidates = sorted(
        proj.glob("*/*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None, "沒有任何 session transcript"

    def _norm(p):
        return os.path.normcase(os.path.normpath(str(p).rstrip("\\/")))

    bridge_wd = _norm(CONFIG["WORKING_DIR"])
    for f in candidates[:30]:
        cwd = _extract_cwd(f)
        if not cwd:
            continue  # 無 cwd 無法 resume
        if _norm(cwd) == bridge_wd:
            continue  # 常駐 B 自己的 session，跳過
        return {
            "sid": f.stem,
            "cwd": cwd,
            "slug": f.parent.name,
            "age_min": (time.time() - f.stat().st_mtime) / 60.0,
        }, None
    return None, "找不到可續接的桌面 session（最近的都是 bridge 自己的工作階段，或缺 cwd）"


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bridge_core.bridge.is_authorized(update.effective_user.id):
        await update.message.reply_text("未授權")
        return

    args = list(context.args)
    force = False
    if args and args[0] == "--force":
        force, args = True, args[1:]
    instruction = " ".join(args).strip()
    if not instruction:
        await update.message.reply_text(
            "用法：/resume <給桌面 CC 的下一步指令>\n"
            "接續你桌面最近那條 Claude Code session（同一份脈絡/todos，落在原專案夾）。\n"
            "桌面 session 若可能還開著會被擋；確定已關閉/閒置可用 /resume --force <指令>。"
        )
        return

    info, err = _find_resume_target()
    if err:
        await update.message.reply_text(f"⚠️ {err}")
        return

    # 併發護欄：太新且非上次自己 resume 的 session → 桌面 A 可能還開著會交錯寫入
    guard_min = CONFIG.get("RESUME_LIVE_GUARD_MIN", 5)
    is_self = (info["sid"] == _RESUME_STATE["last_sid"]
               and (time.time() - _RESUME_STATE["last_ts"]) < 1800)
    if info["age_min"] < guard_min and not force and not is_self:
        await update.message.reply_text(
            f"🛑 最近的 session（{info['slug']}）{info['age_min']:.0f} 分鐘前才寫入，"
            f"桌面 CC 可能還開著。\nheadless resume 會與它交錯寫同一份 transcript。\n"
            f"確定桌面那條已關閉/閒置，請改用：\n/resume --force {instruction}"
        )
        return

    model = CONFIG.get("RESUME_MODEL", "claude-sonnet-5")
    timeout = CONFIG.get("RESUME_TIMEOUT", 300)
    cli = CONFIG.get("CLAUDE_CLI_PATH")
    cli = str(cli) if cli and Path(cli).exists() else "claude"

    await update.message.reply_text(
        f"🔄 接續桌面 session\n"
        f"📁 {info['slug']}\n"
        f"📂 {info['cwd']}\n"
        f"🤖 {model}｜最多等 {timeout}s\n"
        f"▶️ {instruction[:80]}"
    )

    # 用同步 subprocess.run 丟到 thread executor，而非 asyncio.create_subprocess_exec。
    # 原因：bridge 主迴圈被 twikit 匯入時改成 SelectorEventLoop（見
    # [[feedback_twikit_asyncio_policy]]），Windows 上 asyncio 子進程需要
    # ProactorEventLoop → create_subprocess_exec 會拋「空字串的 NotImplementedError」
    # （v3.2.6 首發實測到此症狀）。subprocess.run 與 event loop 無關、放 executor
    # 不阻塞主迴圈（比照 /exec 的同步呼叫 + photo_handler 的 Gemini run_in_executor）。
    loop = asyncio.get_running_loop()

    def _run_resume():
        return subprocess.run(
            [cli, "-p", "--resume", info["sid"],
             "--model", model, "--output-format", "json", instruction],
            capture_output=True, cwd=info["cwd"], timeout=timeout,
            encoding="utf-8", errors="replace",
        )

    fut = loop.run_in_executor(None, _run_resume)
    # 進度心跳：resume 一條大 session 可能要數分鐘，靜默等待會被誤判成當機
    # （v3.2.7 首次成功 resume 這條 1.5MB live session 花了 ~3 分鐘、全程無回饋）。
    # 每 ~25s 更新一則訊息報告已耗時，讓手機端知道還在跑。
    started = time.time()
    prog = await update.message.reply_text("⏳ resume 進行中… 0s")
    while not fut.done():
        _, pending = await asyncio.wait({fut}, timeout=25)
        if not pending:
            break
        try:
            await prog.edit_text(
                f"⏳ resume 進行中… {int(time.time() - started)}s"
                f"（大 session 載入較久，上限 {timeout}s）"
            )
        except Exception:
            pass
    try:
        await prog.delete()
    except Exception:
        pass

    try:
        rp = fut.result()
    except subprocess.TimeoutExpired:
        await update.message.reply_text(
            f"⏱️ resume 超時（>{timeout}s）已中止。可調高 RESUME_TIMEOUT 或把指令拆小。"
        )
        return
    except FileNotFoundError:
        await update.message.reply_text("❌ 找不到 claude CLI，無法 resume。")
        return
    except Exception as e:
        await update.message.reply_text(f"❌ 啟動 resume 失敗：{type(e).__name__}: {e}")
        return

    stdout = (rp.stdout or "").strip()
    stderr = (rp.stderr or "").strip()

    if not stdout:
        low = stderr.lower()
        hint = ""
        if any(k in low for k in ("login", "auth", "unauthorized", "not logged")):
            hint = ("\n（疑似 CLI 未登入 — CLI 認證與桌面 App 分開，"
                    "請先在終端機跑一次 `claude` 登入）")
        await update.message.reply_text(f"❌ resume 無輸出。\nstderr:\n{stderr[:1500]}{hint}")
        return

    try:
        data = json.loads(stdout)
        result = data.get("result") or "(無文字結果)"
        is_err = data.get("is_error")
        cost = data.get("total_cost_usd")
        turns = data.get("num_turns")
        footer = f"\n\n— {'⚠️出錯 ' if is_err else ''}"
        footer += f"💰${cost:.3f}｜" if isinstance(cost, (int, float)) else ""
        footer += f"{turns} turns｜" if turns is not None else ""
        footer += f"session {info['sid'][:8]}"
    except Exception:
        result = stdout
        footer = f"\n\n— session {info['sid'][:8]}（非 JSON 輸出，原樣回傳）"

    # 記住這次 resume，避免下一次 /resume 把自己的新寫入誤判為「桌面還活著」
    _RESUME_STATE["last_sid"] = info["sid"]
    _RESUME_STATE["last_ts"] = time.time()

    body = result + footer
    if len(body) > 4000:
        chunks = [body[i:i+4000] for i in range(0, len(body), 4000)]
        for i, c in enumerate(chunks):
            await update.message.reply_text(f"[{i+1}/{len(chunks)}]\n\n{c}")
    else:
        await update.message.reply_text(f"✅ 桌面 CC 回應：\n\n{body}")


# === /handoff：把桌面 CC session 尾巴交棒給常駐 B（替身）（v3.3.0） ===
# 與 /resume 互補（見 config.py HANDOFF_* 說明）：resume 叫醒本尊、handoff 讓替身 B
# 接手。只「讀」桌面 transcript 尾巴 → 組 briefing → execute_claude 餵給常駐 B。
# 只讀不寫 → 不需要 /resume 的 LIVE_GUARD，桌面 A 還開著也能安全交棒。

def _squeeze(s: str, cap: int) -> str:
    """壓成單行並截斷 — briefing 要緊湊，換行/連續空白都壓成單一空格。"""
    s = " ".join((s or "").split())
    return s if len(s) <= cap else s[:cap - 1] + "…"


def _extract_records(jsonl_path: Path, snippet_cap: int):
    """單趟串流掃 transcript，回傳 (records, last_todos)。
    records = [(kind, text)]，kind ∈ {user, assistant, action}，只收「有意義」的行：
      - 人類訊息（user 且 content 是 str，或 list 內含 text block）
      - assistant 文字 block
      - assistant tool_use（TodoWrite 除外 → 抽成 last_todos）
    跳過：UI 狀態行（last-prompt/ai-title/mode/queue-operation/attachment/
    file-history-snapshot 等）、tool_result（工具輸出非人話）、thinking、
    isMeta（系統注入的 reminder）、isSidechain（subagent 雜訊）。"""
    records = []
    last_todos = None
    try:
        with open(jsonl_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type")
                if t not in ("user", "assistant"):
                    continue
                if d.get("isMeta") or d.get("isSidechain"):
                    continue
                msg = d.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")

                if t == "user":
                    if isinstance(content, str):
                        txt = content.strip()
                    elif isinstance(content, list):
                        parts = [b.get("text", "") for b in content
                                 if isinstance(b, dict) and b.get("type") == "text"]
                        txt = " ".join(parts).strip()  # 空 = 純 tool_result → 跳過
                    else:
                        txt = ""
                    if txt:
                        records.append(("user", _squeeze(txt, snippet_cap)))
                    continue

                # assistant
                if not isinstance(content, list):
                    continue
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text":
                        txt = (b.get("text") or "").strip()
                        if txt:
                            records.append(("assistant", _squeeze(txt, snippet_cap)))
                    elif bt == "tool_use":
                        name = b.get("name") or ""
                        inp = b.get("input") or {}
                        if name == "TodoWrite":
                            last_todos = inp.get("todos") or last_todos
                        else:
                            records.append(("action", _summarize_tool(name, inp)))
    except Exception as e:
        logger.warning(f"[handoff] 讀取 transcript 失敗: {e}")
        return None, None
    return records, last_todos


def _summarize_tool(name: str, inp: dict) -> str:
    """把 tool_use 濃縮成一個短標記給 briefing（比照 bridge_core._summarize_tool_use）。"""
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        p = inp.get("file_path") or inp.get("path") or ""
        return f"{name} {Path(p).name if p else ''}".strip()
    if name == "Bash":
        return f"Bash {(inp.get('command') or '')[:40]}".strip()
    if name in ("Glob", "Grep"):
        return f"{name} {inp.get('pattern','')}".strip()
    if name in ("WebFetch", "WebSearch"):
        return f"{name} {(inp.get('url') or inp.get('query') or '')[:40]}".strip()
    if name == "Task":
        return f"Task {inp.get('description','')}".strip()
    if name.startswith("mcp__"):
        return name.replace("mcp__", "").replace("__", ".")
    return name or "tool"


def _render_context(records, max_chars: int):
    """把 records 排成緊湊敘事（舊→新），連續 action 併成一行；超過 max_chars
    從最舊端裁掉並在頂端標注。回傳 (context_str, n_kept)。"""
    lines = []
    i = 0
    n = len(records)
    while i < n:
        kind, text = records[i]
        if kind == "action":
            acts = []
            while i < n and records[i][0] == "action":
                acts.append(records[i][1])
                i += 1
            shown = acts[:8]
            more = f" …(+{len(acts) - 8})" if len(acts) > 8 else ""
            lines.append("🔧 " + " · ".join(shown) + more)
        else:
            lines.append(("👤 " if kind == "user" else "🤖 ") + text)
            i += 1

    # 從最舊端裁到 max_chars 以內
    trimmed = False
    while lines and len("\n".join(lines)) > max_chars:
        lines.pop(0)
        trimmed = True
    if trimmed:
        lines.insert(0, "…（較舊脈絡已省略）")
    return "\n".join(lines), len(lines)


def _build_handoff_briefing(jsonl_path: Path, cwd: str, instruction: str):
    """組 handoff briefing。回傳 (briefing_str, meta) 或 (None, error_str)。
    meta = {n_records, n_todos}。"""
    snippet_cap = CONFIG.get("HANDOFF_SNIPPET_CHARS", 500)
    tail_n = CONFIG.get("HANDOFF_TAIL_RECORDS", 24)
    max_chars = CONFIG.get("HANDOFF_MAX_CHARS", 6000)

    records, todos = _extract_records(jsonl_path, snippet_cap)
    if records is None:
        return None, "transcript 無法讀取"
    if not records:
        return None, "此 session 沒有可節錄的對話內容"

    tail = records[-tail_n:]
    context_str, n_kept = _render_context(tail, max_chars)

    todo_block = ""
    n_todos = 0
    if todos:
        n_todos = len(todos)
        mark = {"completed": "✅", "in_progress": "🔄", "pending": "⬜"}
        rows = [f"{mark.get(td.get('status'), '•')} {_squeeze(td.get('content') or '', 120)}"
                for td in todos if isinstance(td, dict)]
        if rows:
            todo_block = "## 桌面 session 最後的 todos\n" + "\n".join(rows) + "\n\n"

    briefing = (
        "[HANDOFF] 你要接手一條「桌面 Claude Code」工作線。你是常駐替身 B，"
        "不是那條 session 的本尊——本尊的完整記憶你沒有，只有下面這份最近脈絡摘要"
        "與 todos。請據此接續使用者交辦的下一步。\n\n"
        "重要規則：\n"
        f"- 該專案在 `{cwd}`。你的預設工作目錄不在那裡；操作該專案檔案請用"
        "絕對路徑，或先在 Bash 裡 cd 進去。\n"
        "- 摘要是節錄、不完整。若不足以安全執行下一步（缺關鍵前文或檔案現況），"
        "先簡短說明缺什麼、你打算讀哪些檔補齊，再動手，不要臆測。\n"
        "- 完成後用繁體中文簡述你做了什麼。\n\n"
        f"{todo_block}"
        "## 最近脈絡（舊→新，節錄自桌面 session transcript）\n"
        f"{context_str}\n\n"
        "## 使用者現在交辦\n"
        f"{instruction}"
    )
    return briefing, {"n_records": n_kept, "n_todos": n_todos}


async def handoff_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bridge_core.bridge.is_authorized(update.effective_user.id):
        await update.message.reply_text("未授權")
        return

    instruction = " ".join(context.args).strip()
    if not instruction:
        await update.message.reply_text(
            "用法：/handoff <給替身 B 的下一步指令>\n"
            "讀桌面最近那條 Claude Code session 的尾巴做成 briefing，交給常駐 B 接手。\n"
            "與 /resume 不同：B 是替身（記憶不完整）但有串流、免 CLI 登入；只讀桌面"
            "transcript 不寫入，桌面那條還開著也能安全交棒。"
        )
        return

    if not bridge_core.bridge.sdk_client:
        await update.message.reply_text(
            "❌ 常駐 B（SDK）未連線，無法 handoff。可改用 /resume 叫醒本尊。"
        )
        return

    info, err = _find_resume_target()
    if err:
        await update.message.reply_text(f"⚠️ {err}")
        return

    jsonl = Path.home() / ".claude" / "projects" / info["slug"] / f"{info['sid']}.jsonl"
    loop = asyncio.get_running_loop()
    briefing, meta = await loop.run_in_executor(
        None, _build_handoff_briefing, jsonl, info["cwd"], instruction
    )
    if briefing is None:
        await update.message.reply_text(f"⚠️ 無法建立 briefing：{meta}")
        return

    todo_note = f"｜{meta['n_todos']} 個 todo" if meta.get("n_todos") else ""
    await update.message.reply_text(
        f"📨 交棒給常駐 B（替身）\n"
        f"📁 來源 {info['slug']}（{info['age_min']:.0f} 分前活動）\n"
        f"📂 {info['cwd']}\n"
        f"🧵 節錄 {meta['n_records']} 則脈絡{todo_note}\n"
        f"▶️ {instruction[:80]}"
    )

    processing = await update.message.reply_text("🤝 B 接手中…")
    progress_cb = make_progress_cb(processing, CONFIG["SDK_PROGRESS_EDIT_INTERVAL"])
    photo_cb = make_photo_cb(update.message)
    result = await bridge_core.bridge.execute_claude(
        briefing, progress_cb=progress_cb, photo_cb=photo_cb,
    )
    try:
        await processing.delete()
    except Exception:
        pass

    if len(result) > 4000:
        chunks = [result[i:i+4000] for i in range(0, len(result), 4000)]
        for i, c in enumerate(chunks):
            await update.message.reply_text(f"[{i+1}/{len(chunks)}]\n\n{c}")
    else:
        await update.message.reply_text(f"🤝 替身 B 回應：\n\n{result}")


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
        "• /clear /help /status /stats /interrupt"
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
