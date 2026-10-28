"""每日定时调度。

思路：Cron 表达式只用来算「基准时间」，实际触发点 = 基准时间 + 随机偏移。
这样每天发帖的秒数都不一样，不会露出机器人的痕迹。
调度逻辑参考 Zhalslar/astrbot_plugin_qzone（MIT）。
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from astrbot.api import logger


class DiaryScheduler:
    def __init__(
        self,
        cron_expr: str,
        offset_seconds: int,
        timezone: ZoneInfo,
        job: Callable[[], Awaitable[None]],
    ) -> None:
        self.cron_expr = (cron_expr or "").strip()
        self.offset_seconds = max(0, int(offset_seconds))
        self.timezone = timezone
        self.job = job
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.trigger: CronTrigger | None = None
        self._last_base: datetime | None = None
        self._terminated = False
        self._next_run: datetime | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cron_expr)

    def start(self) -> None:
        if not self.enabled:
            logger.info("[日记] 未配置 publish_cron，定时任务关闭，只能手动触发")
            return
        try:
            self.trigger = CronTrigger.from_crontab(self.cron_expr, self.timezone)
        except Exception as e:
            logger.error(f"[日记] Cron 表达式不合法（{self.cron_expr}）：{e}")
            return
        self.scheduler.start()
        self._schedule_next()
        logger.info(
            f"[日记] 定时任务已启动：{self.cron_expr}，随机浮动 ±{self.offset_seconds} 秒，"
            f"下次执行 {self._next_run}"
        )

    def _schedule_next(self) -> None:
        if self._terminated or self.trigger is None:
            return

        now = datetime.now(self.timezone)
        base = self.trigger.get_next_fire_time(self._last_base, now)
        if not base:
            logger.error("[日记] 无法计算下一次触发时间")
            return
        self._last_base = base

        delay = random.randint(-self.offset_seconds, self.offset_seconds) if self.offset_seconds else 0
        target = base + timedelta(seconds=delay)
        if target <= now:
            target = now + timedelta(seconds=5)
            logger.warning(f"[日记] 偏移后时间已过，改为 5 秒后补偿执行：{target}")

        self._next_run = target
        self.scheduler.add_job(
            func=self._run,
            trigger=DateTrigger(run_date=target, timezone=self.timezone),
            name=f"diary_{int(base.timestamp())}",
            max_instances=1,
            misfire_grace_time=3600,
        )

    async def _run(self) -> None:
        logger.info("[日记] 定时任务开始执行")
        try:
            await self.job()
        except Exception as e:
            logger.exception(f"[日记] 定时任务执行失败：{e}")
        finally:
            if not self._terminated:
                self._schedule_next()

    def next_run_time(self) -> datetime | None:
        return self._next_run

    def shutdown(self) -> None:
        if self._terminated:
            return
        self._terminated = True
        try:
            self.scheduler.remove_all_jobs()
            self.scheduler.shutdown(wait=False)
        except Exception as e:
            logger.debug(f"[日记] 关闭调度器时忽略异常：{e}")
