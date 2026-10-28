"""日记生成：读人格 → 拼提示词 → 调模型 → 清洗正文。"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.core.provider.provider import Provider
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

# 「观测生活」插件每天刷新的摘要，这里只读不写
OBSERVE_PLUGIN = "astrbot_plugin_observe_life"
BRIEF_NAME = "today_brief.txt"
BRIEF_MAX_AGE = 12 * 3600  # 超过 12 小时的文件当昨天的处理
NO_ACTIVITY = "（今天没有观测到他在电脑上的活动）"


class DiaryError(RuntimeError):
    """日记生成失败。"""


class DiaryWriter:
    def __init__(self, context: Any, settings: dict) -> None:
        self.context = context
        self.settings = settings

    # ---------- 人格 ----------

    async def resolve_persona(self) -> tuple[str, str]:
        """返回 (persona_id, system_prompt)。取不到时返回 ("", "")。"""
        mgr = getattr(self.context, "persona_manager", None)
        if mgr is None:
            logger.warning("找不到 persona_manager，日记将不带人格")
            return "", ""

        wanted = str(self.settings.get("persona_id") or "").strip()
        persona = None
        persona_id = wanted

        if wanted:
            try:
                persona = mgr.get_persona_v3_by_id(wanted)
            except Exception as e:  # pragma: no cover
                logger.warning(f"按 ID 取人格失败: {e}")
            if not persona:
                logger.warning(f"未找到 persona_id={wanted} 的人格，回退到当前默认人格")

        if not persona:
            try:
                persona = await mgr.get_default_persona_v3()
            except Exception as e:
                logger.warning(f"取默认人格失败: {e}")
                return "", ""

        if not persona:
            return "", ""

        persona_id = str(persona.get("name") or persona_id or "")
        prompt = str(persona.get("prompt") or "").strip()
        return persona_id, prompt

    # ---------- 观测素材 ----------

    def read_activity_brief(self) -> str:
        """读「观测生活」写的今日摘要。读不到就返回占位文本，绝不让日记失败。"""
        if not bool(self.settings.get("activity_feed_enable", True)):
            return NO_ACTIVITY
        try:
            path = Path(get_astrbot_plugin_data_path()) / OBSERVE_PLUGIN / BRIEF_NAME
        except Exception as e:
            logger.warning(f"定位观测数据目录失败：{e}")
            return NO_ACTIVITY

        if not path.exists():
            return NO_ACTIVITY
        try:
            # 昨天留下的旧文件不能当成今天
            if time.time() - path.stat().st_mtime > BRIEF_MAX_AGE:
                return NO_ACTIVITY
            text = path.read_text(encoding="utf-8").strip()
        except OSError as e:
            logger.warning(f"读取观测摘要失败：{e}")
            return NO_ACTIVITY
        return text or NO_ACTIVITY

    # ---------- 模型 ----------

    def _get_provider(self) -> Provider:
        provider = None
        wanted = str(self.settings.get("provider_id") or "").strip()
        if wanted:
            provider = self.context.get_provider_by_id(wanted)
        if not isinstance(provider, Provider):
            provider = self.context.get_using_provider()
        if not isinstance(provider, Provider):
            raise DiaryError("没有可用的 LLM 提供商，请先在 AstrBot 里配置模型")
        return provider

    # ---------- 清洗 ----------

    @staticmethod
    def clean(raw: str) -> str:
        text = (raw or "").strip()

        # 去掉 ```...``` 代码块（模型偶尔爱包一层）
        fence = re.search(r"```[a-zA-Z]*\s*(.*?)```", text, re.S)
        if fence:
            text = fence.group(1).strip()

        # 只有整段都被三引号包住时才剥壳，否则只删掉引号本身，
        # 避免把引号外面的正文一起丢掉
        text = text.strip()
        if text.startswith('"""') and text.endswith('"""') and len(text) > 6:
            text = text[3:-3].strip()
        elif text.startswith("'''") and text.endswith("'''") and len(text) > 6:
            text = text[3:-3].strip()
        else:
            text = text.replace('"""', "").replace("'''", "").strip()

        lines = [ln.rstrip() for ln in text.splitlines()]

        heading = re.compile(r"^#{1,6}\s*.*$")
        bold_only = re.compile(r"^\*\*.*\*\*$")
        date_line = re.compile(r"^\d{4}\s*[年\-/.]\s*\d{1,2}\s*[月\-/.]\s*\d{1,2}\s*日?$")
        rule_line = re.compile(r"^[-*_]{3,}$")
        label_only = re.compile(r"^日记\s*[:：]?$")
        label_prefix = re.compile(r"^日记\s*[:：]\s*(.+)$")

        # 只清理开头最多 6 行，正文中间的井号/星号一律不动
        dropped = 0
        while lines and dropped < 6:
            head = lines[0].strip()
            if not head:
                lines.pop(0)
                continue
            if (
                heading.match(head)
                or bold_only.match(head)
                or date_line.match(head)
                or rule_line.match(head)
                or label_only.match(head)
            ):
                lines.pop(0)
                dropped += 1
                continue
            match = label_prefix.match(head)
            if match:
                lines[0] = match.group(1)
            break

        text = "\n".join(lines).strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _trim(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        window = text[:max_chars]
        cut = max(
            window.rfind("。"),
            window.rfind("！"),
            window.rfind("？"),
            window.rfind("…"),
            window.rfind("\n"),
        )
        if cut >= max_chars * 0.6:
            return text[: cut + 1].strip()
        return window.strip()

    # ---------- 主流程 ----------

    async def write(self, recent_openings: list[str]) -> tuple[str, str, str]:
        """生成一篇日记，返回 (正文, persona_id, persona_prompt)。"""
        persona_id, persona_prompt = await self.resolve_persona()
        if not persona_prompt:
            logger.warning("人格提示词为空，日记会使用通用口吻")

        min_chars = int(self.settings.get("min_chars") or 150)
        max_chars = int(self.settings.get("max_chars") or 400)
        if max_chars < min_chars:
            max_chars = min_chars

        task = str(self.settings.get("diary_prompt") or "").strip()
        if not task:
            raise DiaryError("diary_prompt 配置为空")

        recent = "\n".join(f"- {s}" for s in recent_openings if s) or "- （还没有历史日记，这是第一篇）"

        task = (
            task.replace("{min_chars}", str(min_chars))
            .replace("{max_chars}", str(max_chars))
            .replace("{recent}", recent)
            .replace("{activity}", self.read_activity_brief())
            .replace("{today}", datetime.now().strftime("%Y年%m月%d日"))
        )

        system_prompt = f"{persona_prompt}\n\n{task}" if persona_prompt else task

        provider = self._get_provider()
        try:
            resp = await provider.text_chat(
                system_prompt=system_prompt,
                prompt="写吧。只输出日记正文。",
            )
        except Exception as e:
            raise DiaryError(f"调用模型失败：{e}") from e

        text = self.clean(getattr(resp, "completion_text", "") or "")
        if not text:
            raise DiaryError("模型返回的日记是空的")

        trimmed = self._trim(text, max_chars)
        if len(trimmed) < min_chars:
            logger.warning(f"日记只有 {len(trimmed)} 字，低于设定的 {min_chars} 字")
        if len(text) > max_chars:
            logger.info(f"日记长度 {len(text)} 字超过上限 {max_chars}，已按句号截断")

        return trimmed, persona_id, persona_prompt
