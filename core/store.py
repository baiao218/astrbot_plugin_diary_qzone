"""轻量状态存储。

- `state.json`：只记日期、开头片段和 tid，不保存日记全文（防重复用）。
- `pending.json`：写完但没发出去的那一篇，等下次补发；发出去就删。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from astrbot.api import logger

MAX_ENTRIES = 90
PENDING_MAX_AGE_HOURS = 36


class DiaryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.pending_path = path.with_name("pending.json")
        self.data: dict = {"entries": []}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("entries"), list):
                self.data = loaded
        except Exception as e:
            logger.warning(f"读取日记状态文件失败，将重新开始：{e}")
            self.data = {"entries": []}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"写入日记状态文件失败：{e}")

    # ---------- 查询 ----------

    def recent_openings(self, n: int) -> list[str]:
        n = max(0, int(n))
        if not n:
            return []
        entries = self.data.get("entries") or []
        return [str(e.get("head") or "") for e in entries[-n:] if e.get("head")]

    def last_entry(self) -> dict | None:
        entries = self.data.get("entries") or []
        return entries[-1] if entries else None

    def today_count(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        return sum(1 for e in (self.data.get("entries") or []) if e.get("date") == today)

    # ---------- 写入 ----------

    def add(self, text: str, *, tid: str = "", ok: bool = False, mode: str = "auto") -> None:
        entries = self.data.setdefault("entries", [])
        head = text.strip().replace("\n", " ")[:60]
        entries.append(
            {
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "date": datetime.now().strftime("%Y-%m-%d"),
                "chars": len(text.strip()),
                "head": head,
                "tid": tid,
                "ok": bool(ok),
                "mode": mode,
            }
        )
        if len(entries) > MAX_ENTRIES:
            del entries[: len(entries) - MAX_ENTRIES]
        self.save()

    def today_published(self) -> bool:
        today = datetime.now().strftime("%Y-%m-%d")
        return any(
            e.get("date") == today and e.get("ok")
            for e in (self.data.get("entries") or [])
        )

    # ---------- 待补发 ----------

    def save_pending(self, text: str, *, mode: str = "auto") -> None:
        """记下一篇没发出去的日记，等下次补发。"""
        try:
            self.pending_path.parent.mkdir(parents=True, exist_ok=True)
            self.pending_path.write_text(
                json.dumps(
                    {
                        "text": text,
                        "mode": mode,
                        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"写入待补发日记失败：{e}")

    def load_pending(self) -> dict | None:
        """读取待补发日记；超过 36 小时视为过期，直接丢弃。"""
        if not self.pending_path.exists():
            return None
        try:
            payload = json.loads(self.pending_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"读取待补发日记失败：{e}")
            self.clear_pending()
            return None

        if not isinstance(payload, dict) or not str(payload.get("text") or "").strip():
            self.clear_pending()
            return None

        try:
            at = datetime.strptime(str(payload.get("at")), "%Y-%m-%d %H:%M:%S")
        except Exception:
            return payload

        age_hours = (datetime.now() - at).total_seconds() / 3600
        if age_hours > PENDING_MAX_AGE_HOURS:
            logger.warning(
                f"[日记] 待补发日记已过期 {age_hours:.1f} 小时，丢弃（{len(str(payload.get('text'))) } 字）"
            )
            self.clear_pending()
            return None
        return payload

    def clear_pending(self) -> None:
        try:
            if self.pending_path.exists():
                self.pending_path.unlink()
        except Exception as e:
            logger.warning(f"删除待补发日记失败：{e}")
