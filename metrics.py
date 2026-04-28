"""
metrics.py — 基礎指標收集模組
==============================
追蹤 T-M-B 的關鍵操作指標：fetch 成功/失敗率、Claude 回應時間、每日使用頻率。
持久化至 stats.json，供 /stats 指令查看。
"""

import json
import time
import logging
from datetime import datetime
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)

DEFAULT_STATS_FILE = Path("stats.json")


class Metrics:
    def __init__(self, stats_file: Path = DEFAULT_STATS_FILE):
        self.stats_file = stats_file
        self._lock = Lock()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            if self.stats_file.exists():
                with open(self.stats_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"[metrics] 載入 stats.json 失敗: {e}")
        return self._empty()

    def _empty(self) -> dict:
        return {
            "since": datetime.now().strftime("%Y-%m-%d"),
            "totals": {
                "messages": 0,
                "claude_calls": 0,
                "claude_errors": 0,
                "fetch_success": 0,
                "fetch_fail": 0,
                "obsidian_saves": 0,
                "photos": 0,
                "reply_fetches": 0,
                "reply_fetch_success": 0,
                "reply_fetch_fail": 0,
            },
            "timing": {
                "claude_total_sec": 0.0,
                "claude_count": 0,
                "fetch_total_sec": 0.0,
                "fetch_count": 0,
            },
            "daily": {},  # "2026-04-15": {"messages": 3, "fetches": 2, ...}
            "recent_errors": [],  # last 20 errors
        }

    def _save(self):
        try:
            with open(self.stats_file, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[metrics] 儲存 stats.json 失敗: {e}")

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _ensure_daily(self, day: str):
        if day not in self._data["daily"]:
            self._data["daily"][day] = {
                "messages": 0, "claude_calls": 0, "fetches": 0, "errors": 0,
            }
        # 清理超過 30 天的 daily 條目
        keys = sorted(self._data["daily"].keys())
        while len(keys) > 30:
            del self._data["daily"][keys.pop(0)]

    def record_message(self):
        with self._lock:
            self._data["totals"]["messages"] += 1
            day = self._today()
            self._ensure_daily(day)
            self._data["daily"][day]["messages"] += 1
            self._save()

    def record_claude_call(self, duration_sec: float, success: bool):
        with self._lock:
            self._data["totals"]["claude_calls"] += 1
            if not success:
                self._data["totals"]["claude_errors"] += 1
                day = self._today()
                self._ensure_daily(day)
                self._data["daily"][day]["errors"] += 1
            day = self._today()
            self._ensure_daily(day)
            self._data["daily"][day]["claude_calls"] += 1
            self._data["timing"]["claude_total_sec"] += duration_sec
            self._data["timing"]["claude_count"] += 1
            self._save()

    def record_fetch(self, url: str, platform: str, method: str,
                     success: bool, duration_sec: float):
        with self._lock:
            if success:
                self._data["totals"]["fetch_success"] += 1
            else:
                self._data["totals"]["fetch_fail"] += 1
            day = self._today()
            self._ensure_daily(day)
            self._data["daily"][day]["fetches"] += 1
            if not success:
                self._data["daily"][day]["errors"] += 1
            self._data["timing"]["fetch_total_sec"] += duration_sec
            self._data["timing"]["fetch_count"] += 1
            self._save()

    def record_obsidian_save(self):
        with self._lock:
            self._data["totals"]["obsidian_saves"] += 1
            self._save()

    def record_photo(self):
        with self._lock:
            self._data["totals"]["photos"] += 1
            self._save()

    def record_reply_fetch(self, success: bool = True, error_code: str = ""):
        """
        記錄一次回覆抓取。success=False 時 error_code 會進 recent_errors，
        供 /stats 與根因分析使用（handoff 2026-04-17 修復：原本失敗被吞，
        reply_fetches 從未累加成功案例）。
        """
        with self._lock:
            self._data["totals"]["reply_fetches"] += 1
            if success:
                self._data["totals"].setdefault("reply_fetch_success", 0)
                self._data["totals"]["reply_fetch_success"] += 1
            else:
                self._data["totals"].setdefault("reply_fetch_fail", 0)
                self._data["totals"]["reply_fetch_fail"] += 1
                entry = {
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "context": "reply_fetch",
                    "error": (error_code or "unspecified")[:200],
                }
                self._data["recent_errors"].append(entry)
                self._data["recent_errors"] = self._data["recent_errors"][-20:]
            self._save()

    def record_error(self, context: str, error_msg: str):
        with self._lock:
            entry = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "context": context,
                "error": str(error_msg)[:200],
            }
            self._data["recent_errors"].append(entry)
            # 只保留最近 20 筆
            self._data["recent_errors"] = self._data["recent_errors"][-20:]
            self._save()

    def get_summary(self) -> str:
        d = self._data
        t = d["totals"]
        tm = d["timing"]

        # 平均回應時間
        avg_claude = (tm["claude_total_sec"] / tm["claude_count"]
                      if tm["claude_count"] > 0 else 0)
        avg_fetch = (tm["fetch_total_sec"] / tm["fetch_count"]
                     if tm["fetch_count"] > 0 else 0)

        # fetch 成功率
        total_fetches = t["fetch_success"] + t["fetch_fail"]
        fetch_rate = (t["fetch_success"] / total_fetches * 100
                      if total_fetches > 0 else 0)

        # 今日
        today = self._data["daily"].get(self._today(), {})

        # 最近 7 天趨勢
        days = sorted(self._data["daily"].keys())[-7:]
        trend_lines = []
        for day in days:
            dd = self._data["daily"][day]
            short_day = day[5:]  # MM-DD
            trend_lines.append(
                f"  {short_day}: {dd.get('messages', 0)} msg, "
                f"{dd.get('fetches', 0)} fetch, "
                f"{dd.get('errors', 0)} err"
            )
        trend = "\n".join(trend_lines) if trend_lines else "  (無資料)"

        # 最近錯誤
        recent_err = ""
        if d["recent_errors"]:
            last_3 = d["recent_errors"][-3:]
            err_lines = [f"  [{e['time']}] {e['context']}: {e['error']}" for e in last_3]
            recent_err = "\n".join(err_lines)
        else:
            recent_err = "  (無)"

        return f"""📊 T-M-B 指標 (自 {d['since']})

累計：
  訊息: {t['messages']} | Claude 呼叫: {t['claude_calls']} (錯誤 {t['claude_errors']})
  Fetch: {t['fetch_success']}✅ / {t['fetch_fail']}❌ (成功率 {fetch_rate:.0f}%)
  Obsidian 落地: {t['obsidian_saves']} | 照片: {t['photos']}
  回覆抓取: {t['reply_fetches']} ({t.get('reply_fetch_success', 0)}✅ / {t.get('reply_fetch_fail', 0)}❌)

平均耗時：
  Claude 回應: {avg_claude:.1f}s | URL 抓取: {avg_fetch:.1f}s

今日 ({self._today()})：
  訊息: {today.get('messages', 0)} | Claude: {today.get('claude_calls', 0)} | Fetch: {today.get('fetches', 0)} | 錯誤: {today.get('errors', 0)}

近 7 日趨勢：
{trend}

最近錯誤：
{recent_err}"""


class Timer:
    """簡易計時器，用於測量操作耗時。"""
    def __init__(self):
        self._start = None

    def start(self):
        self._start = time.monotonic()
        return self

    def elapsed(self) -> float:
        if self._start is None:
            return 0.0
        return time.monotonic() - self._start
