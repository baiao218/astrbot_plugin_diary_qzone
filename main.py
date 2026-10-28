"""日记本 · QQ空间 —— 对她说「晚安」时写一篇日记并发布到 QQ 空间。

链路：
    私聊里出现「晚安」
        → 读当前生效的人格提示词
        → 让模型写一篇独白体日记
        → 存一份到 pending.json（防止关机丢稿）
        → 通过 OneBot get_cookies 取 Cookie
        → emotion_cgi_publish_v6 发说说
        → 成功则清掉 pending

定时任务只作为兜底，默认关闭（适合会定时关机的机器）。
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Plain
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .core.qzone import QzoneAuthError, QzonePublisher
from .core.scheduler import DiaryScheduler
from .core.store import DiaryStore
from .core.writer import DiaryError, DiaryWriter

PLUGIN_NAME = "astrbot_plugin_diary_qzone"

UGC_LABEL = {"1": "所有人可见", "4": "好友可见", "64": "仅自己可见"}

# 装饰器里的正则必须在类定义时就确定，所以写得宽一些；
# 真正的判定在 on_goodnight 里按配置的触发词做。
GOODNIGHT_DECORATOR_RE = r"(晚安|睡了|睡觉|睡啦|安啦|去睡|要睡|睡觉了)"

DEFAULT_KEYWORDS = "晚安,睡了,睡觉,睡啦"


class DiaryQzonePlugin(Star):
    """对她说「晚安」时写日记 → QQ空间说说。

    命令：
        /写日记    立刻写一篇并发布
        /日记预览  只写不发
        /日记状态  查看触发方式与登录状态
    """

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.settings = config

        self.data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.store = DiaryStore(self.data_dir / "state.json")
        self.writer = DiaryWriter(context, config)
        self.publisher = QzonePublisher(
            manual_cookie=str(config.get("manual_cookie") or ""),
            cookie_ttl=int(config.get("cookie_ttl") or 600),
            timeout=int(config.get("timeout") or 20),
        )

        tz_name = self.context.get_config().get("timezone") or "Asia/Shanghai"
        try:
            self.tz = ZoneInfo(str(tz_name))
        except Exception:
            self.tz = ZoneInfo("Asia/Shanghai")

        self.scheduler = DiaryScheduler(
            str(config.get("publish_cron") or ""),
            int(config.get("publish_offset") or 0),
            self.tz,
            self.daily_job,
        )

        self._busy = asyncio.Lock()
        self._bg_tasks: set[asyncio.Task] = set()

    # ---------- 触发词 ----------

    def goodnight_keywords(self) -> list[str]:
        raw = str(self.settings.get("goodnight_keywords") or DEFAULT_KEYWORDS)
        words = [w.strip() for w in re.split(r"[,，;；\s]+", raw) if w.strip()]
        # 长的排前面，避免「睡了」先于「我睡了」命中
        return sorted(set(words), key=len, reverse=True)

    def _is_goodnight(self, text: str) -> bool:
        flat = re.sub(r"[\s~～!！。、,，…]+", "", text or "")
        return any(w in flat for w in self.goodnight_keywords())

    def owner_ids(self) -> set[str]:
        """哪些 QQ 说「晚安」才算数。

        留空时回退到 AstrBot 的 admins_id，这样别人对她说晚安不会占掉
        你今天唯一的名额、也不会把日记发到你的空间。填 * 表示不限制。
        """
        raw = str(self.settings.get("owner_ids") or "").strip()
        if raw == "*":
            return set()
        if raw:
            return {x.strip() for x in re.split(r"[,，\s]+", raw) if x.strip()}

        try:
            admins = self.context.get_config().get("admins_id") or []
        except Exception:
            admins = []
        return {str(a) for a in admins if str(a).isdigit()}

    def _spawn(self, coro) -> None:
        """后台跑，防止关机前阻塞对话。"""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # ---------- 生命周期 ----------

    async def initialize(self) -> None:
        self.publisher.attach_bot(self._get_bot())
        self.scheduler.start()
        self._spawn(self._delayed_retry())
        logger.info(
            f"[日记] 插件已就绪，数据目录 {self.data_dir}；"
            f"触发词 {self.goodnight_keywords()}"
        )

    async def terminate(self) -> None:
        for task in list(self._bg_tasks):
            task.cancel()
        self.scheduler.shutdown()
        await self.publisher.close()
        logger.info("[日记] 插件已停止")

    # ---------- 依赖 ----------

    def _get_bot(self):
        platform = None
        try:
            platform = self.context.get_platform("aiocqhttp")
        except Exception:
            platform = None
        if platform is None:
            try:
                for cand in self.context.platform_manager.platform_insts:
                    meta = cand.meta()
                    if getattr(meta, "name", "") == "aiocqhttp":
                        platform = cand
                        break
            except Exception:
                platform = None
        return getattr(platform, "bot", None) if platform is not None else None

    def _ugc_right(self) -> int:
        raw = str(self.settings.get("ugc_right") or "1").strip()
        return int(raw) if raw in UGC_LABEL else 1

    # ---------- 核心动作 ----------

    async def daily_job(self) -> None:
        dry = bool(self.settings.get("dry_run", True))
        await self.produce(mode="auto", dry_run=dry)

    async def produce(self, *, mode: str, dry_run: bool, notify: bool = True) -> str:
        """生成并（可选）发布，返回一段人可读的报告。

        notify=False 时成功不发通知（晚安触发走这条路，避免破坏气氛），
        失败仍然一定通知。
        """
        if self._busy.locked():
            msg = "上一篇还在写，这次跳过"
            logger.warning(f"[日记] {msg}")
            return msg

        async with self._busy:
            self.publisher.attach_bot(self._get_bot())

            recent = self.store.recent_openings(int(self.settings.get("avoid_repeat_count") or 3))
            try:
                text, persona_id, _ = await self.writer.write(recent)
            except DiaryError as e:
                logger.error(f"[日记] 生成失败：{e}")
                await self._notify(f"日记生成失败：{e}")
                return f"生成失败：{e}"

            logger.info(f"[日记] 生成完成（人格={persona_id or '无'}，{len(text)} 字）\n{text}")

            if dry_run:
                self.store.add(text, ok=False, mode="dry_run")
                if notify:
                    await self._notify(f"【日记·空跑未发布】\n\n{text}")
                return f"【空跑模式，未发布】\n\n{text}"

            ugc = self._ugc_right()
            try:
                result = await self.publisher.publish(text, ugc)
            except QzoneAuthError as e:
                logger.error(f"[日记] Cookie 不可用：{e}")
                self.store.add(text, ok=False, mode=mode)
                self.store.save_pending(text, mode=mode)
                await self._notify(f"日记已写好但发布失败（{e}），已存档等下次补发：\n\n{text}")
                return f"发布失败：{e}\n\n日记正文已生成：\n{text}"
            except Exception as e:
                logger.exception(f"[日记] 发布异常：{e}")
                self.store.add(text, ok=False, mode=mode)
                self.store.save_pending(text, mode=mode)
                await self._notify(f"日记发布异常（{e}），已存档等下次补发：\n\n{text}")
                return f"发布异常：{e}\n\n日记正文已生成：\n{text}"

            self.store.add(text, tid=result.get("tid", ""), ok=result["ok"], mode=mode)

            if result["ok"]:
                self.store.clear_pending()
                report = (
                    f"已发布到 QQ 空间（{UGC_LABEL[str(ugc)]}）\n"
                    f"tid：{result['tid']}\n\n{text}"
                )
                logger.info(f"[日记] 发布成功 tid={result['tid']}")
                if notify:
                    await self._notify(f"【今日日记已发布】\n\n{text}")
            else:
                self.store.save_pending(text, mode=mode)
                report = (
                    f"发布失败：code={result['code']} {result['message']}\n\n{text}"
                )
                logger.error(f"[日记] 发布失败：{result}")
                await self._notify(f"日记发布失败（{result['message']}）：\n\n{text}")
            return report

    async def _delayed_retry(self) -> None:
        """等平台连上再补发，插件初始化时 OneBot 客户端还没就绪。"""
        for _ in range(12):  # 最多等 2 分钟
            await asyncio.sleep(10)
            if self._get_bot() is not None:
                break
        await self._retry_pending()

    async def _retry_pending(self) -> None:
        """开机时补发上次没发出去的日记。"""
        pending = self.store.load_pending()
        if not pending:
            return
        text = str(pending.get("text") or "").strip()
        if not text:
            return
        logger.info(f"[日记] 发现待补发日记（写于 {pending.get('at')}，{len(text)} 字），开始补发")
        if self._busy.locked():
            logger.warning("[日记] 正在写新的，跳过补发")
            return
        async with self._busy:
            self.publisher.attach_bot(self._get_bot())
            try:
                result = await self.publisher.publish(text, self._ugc_right())
            except Exception as e:
                logger.error(f"[日记] 补发失败：{e}")
                return
            if result.get("ok"):
                self.store.add(
                    text, tid=result.get("tid", ""), ok=True, mode="retry"
                )
                self.store.clear_pending()
                logger.info(f"[日记] 补发成功 tid={result.get('tid')}")
                await self._notify(f"【昨晚那篇日记补发成功】\n\n{text}")
            else:
                logger.error(f"[日记] 补发失败：{result}")

    async def _notify(self, text: str) -> None:
        umo = str(self.settings.get("notify_umo") or "").strip()
        if not umo:
            return
        try:
            await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception as e:
            logger.warning(f"[日记] 同步到会话失败：{e}")

    # ---------- 晚安触发 ----------

    @filter.regex(GOODNIGHT_DECORATOR_RE)
    async def on_goodnight(self, event: AstrMessageEvent):
        """听到「晚安」就写日记并发空间。

        注意：这里**不** stop event，她还会照常回一句晚安。
        """
        if not bool(self.settings.get("goodnight_enable", True)):
            return
        if not event.is_private_chat():
            return
        owners = self.owner_ids()
        if owners and str(event.get_sender_id()) not in owners:
            logger.info(
                f"[日记] {event.get_sender_id()} 不是配置的主人，晚安不触发写日记"
            )
            return
        if not self._is_goodnight(event.get_message_str()):
            return
        if self.store.today_published():
            logger.info("[日记] 今天已经发过一篇，晚安触发跳过")
            return

        logger.info(f"[日记] 检测到晚安触发：{event.get_message_str()[:40]!r}")
        self._spawn(self._goodnight_job())

    async def _goodnight_job(self) -> None:
        # 先看有没有上次没发出去的，有就补发，不重复生成
        pending = self.store.load_pending()
        if pending:
            await self._retry_pending()
            if self.store.today_published():
                return

        notify = bool(self.settings.get("goodnight_notify", False))
        await self.produce(mode="goodnight", dry_run=False, notify=notify)

    # ---------- 命令 ----------

    @filter.command("写日记")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_write(self, event: AstrMessageEvent):
        """立刻写一篇日记并发布到 QQ 空间。"""
        yield event.plain_result("正在写，稍等……")
        report = await self.produce(mode="manual", dry_run=False)
        yield event.plain_result(report)

    @filter.command("日记预览")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_preview(self, event: AstrMessageEvent):
        """只生成不发布，用来检查口吻和长度。"""
        yield event.plain_result("正在写，稍等……")
        report = await self.produce(mode="preview", dry_run=True)
        yield event.plain_result(report)

    @filter.command("日记状态")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_status(self, event: AstrMessageEvent):
        """查看调度、Cookie 与人格状态。"""
        lines: list[str] = ["【日记插件状态】"]
        owners = self.owner_ids()

        if bool(self.settings.get("goodnight_enable", True)):
            kws = "、".join(self.goodnight_keywords())
            state = "今天已发过" if self.store.today_published() else "今天还没发"
            lines.append(f"晚安触发：开（{kws}）｜{state}")
        else:
            lines.append("晚安触发：关")
        lines.append(
            "响应 QQ：" + ("、".join(sorted(owners)) if owners else "不限制（任何好友说晚安都会写）")
        )

        cron = str(self.settings.get("publish_cron") or "").strip()
        if not self.scheduler.enabled:
            lines.append("定时兜底：关闭（只靠晚安触发）")
        else:
            nxt = self.scheduler.next_run_time()
            lines.append(f"定时兜底：{cron} ±{self.settings.get('publish_offset')} 秒")
            lines.append(f"下次执行：{nxt.strftime('%Y-%m-%d %H:%M:%S') if nxt else '未排期'}")

        lines.append(f"空跑模式：{'开（定时只生成不发布）' if self.settings.get('dry_run') else '关'}")
        ugc = self._ugc_right()
        lines.append(f"可见范围：{UGC_LABEL[str(ugc)]}")

        pending = self.store.load_pending()
        if pending:
            lines.append(f"待补发：有（写于 {pending.get('at')}）")
        else:
            lines.append("待补发：无")

        try:
            persona_id, persona_prompt = await self.writer.resolve_persona()
            lines.append(
                f"使用人格：{persona_id or '无'}（提示词 {len(persona_prompt)} 字符）"
            )
        except Exception as e:
            lines.append(f"使用人格：读取失败 {e}")

        last = self.store.last_entry()
        if last:
            lines.append(
                f"最近一篇：{last.get('at')}，{last.get('chars')} 字，"
                f"{'成功' if last.get('ok') else '未发布'}"
            )
        else:
            lines.append("最近一篇：还没有")

        bot = self._get_bot()
        self.publisher.attach_bot(bot)
        if bot is None:
            lines.append("登录状态：未找到 OneBot 客户端")
        else:
            try:
                ctx = await self.publisher.get_ctx(force=True)
                lines.append(f"登录状态：正常，uin={ctx.uin}")
            except Exception as e:
                lines.append(f"登录状态：异常（{e}）")

        yield event.plain_result("\n".join(lines))
