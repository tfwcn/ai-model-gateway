"""
定时任务调度器

使用APScheduler实现爬虫任务的定时执行，支持服务启动时立即执行和周期性更新。
"""

import asyncio
import logging
from datetime import datetime
from typing import Callable, Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

logger = logging.getLogger(__name__)


class ScheduledScraper:
    """
    定时爬虫任务调度器

    管理爬虫任务的定时执行，包括：
    - 服务启动时立即执行一次
    - 每天凌晨定时更新
    - 任务状态跟踪和错误记录
    - 优雅关闭

    使用示例:
        async def scrape_task():
            # 爬虫逻辑
            pass

        scheduler = ScheduledScraper(
            scrape_func=scrape_task,
            cron_expression="0 2 * * *"  # 每天凌晨2点
        )
        await scheduler.start()
    """

    def __init__(
        self,
        scrape_func: Callable,
        cron_expression: str = "0 2 * * *",
        run_on_start: bool = True,
        timezone: str = "Asia/Shanghai"
    ):
        """
        初始化调度器

        Args:
            scrape_func: 爬虫任务函数（async）
            cron_expression: cron表达式，默认每天凌晨2点
            run_on_start: 是否在启动时立即执行一次，默认True
            timezone: 时区，默认Asia/Shanghai
        """
        self.scrape_func = scrape_func
        self.cron_expression = cron_expression
        self.run_on_start = run_on_start
        self.timezone = timezone

        # 调度器实例
        self.scheduler: Optional[AsyncIOScheduler] = None

        # 任务状态
        self.last_run_time: Optional[datetime] = None
        self.last_success_time: Optional[datetime] = None
        self.consecutive_failures: int = 0
        self.total_runs: int = 0
        self.total_successes: int = 0

    async def start(self, wait_for_initial: bool = False) -> None:
        """
        启动调度器

        Args:
            wait_for_initial: 是否等待初始爬虫任务完成，默认False
                             如果为True，会阻塞直到首次爬虫完成或失败

        如果配置了run_on_start，会立即执行一次爬虫任务，
        然后添加定时任务。
        """
        logger.info("启动定时爬虫调度器...")

        # 创建调度器
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)

        # 如果配置了启动时执行
        if self.run_on_start:
            logger.info("配置了启动时执行，将立即运行爬虫任务")
            # 添加一个立即执行的任务
            self.scheduler.add_job(
                self._execute_scrape,
                trigger=DateTrigger(),  # 立即执行
                id="initial_scrape",
                name="Initial scrape on startup",
                misfire_grace_time=60
            )

        # 添加定时任务
        try:
            # 解析cron表达式
            minute, hour, day, month, day_of_week = self.cron_expression.split()

            self.scheduler.add_job(
                self._execute_scrape,
                trigger=CronTrigger(
                    minute=minute,
                    hour=hour,
                    day=day,
                    month=month,
                    day_of_week=day_of_week,
                    timezone=self.timezone
                ),
                id="scheduled_scrape",
                name="Scheduled model list update",
                misfire_grace_time=300,  # 允许5分钟的误差
                replace_existing=True
            )

            logger.info(f"定时任务已配置: cron='{self.cron_expression}' ({self.timezone})")

        except Exception as e:
            logger.error(f"配置定时任务失败: {e}")
            raise

        # 启动调度器
        self.scheduler.start()
        logger.info("✓ 调度器已启动")

        # 如果需要等待初始任务完成
        if wait_for_initial and self.run_on_start:
            await self._wait_for_initial_scrape()

    async def stop(self, wait: bool = True, timeout: int = 5) -> None:
        """
        停止调度器

        Args:
            wait: 是否等待当前任务完成，默认True
            timeout: 等待超时时间（秒），默认5秒
        """
        if not self.scheduler:
            logger.warning("调度器未启动")
            return

        logger.info("停止定时爬虫调度器...")

        try:
            if wait:
                # 等待当前任务完成
                logger.info(f"等待当前任务完成（最多{timeout}秒）...")
                self.scheduler.shutdown(wait=True, timeout=timeout)
            else:
                # 立即关闭
                self.scheduler.shutdown(wait=False)

            logger.info("✓ 调度器已停止")

        except Exception as e:
            logger.error(f"停止调度器失败: {e}", exc_info=True)

    async def _execute_scrape(self) -> None:
        """
        执行爬虫任务（内部方法）

        包含错误处理、状态跟踪和告警逻辑。
        """
        self.total_runs += 1
        self.last_run_time = datetime.now()

        task_name = "启动时爬虫" if self.total_runs == 1 and self.run_on_start else "定时爬虫"
        logger.info(f"▶ 开始执行{task_name}任务 (第{self.total_runs}次)")

        try:
            # 执行爬虫任务
            start_time = datetime.now()
            await self.scrape_func()
            duration = (datetime.now() - start_time).total_seconds()

            # 成功
            self.last_success_time = datetime.now()
            self.total_successes += 1
            self.consecutive_failures = 0

            logger.info(f"✓ {task_name}任务成功完成，耗时 {duration:.2f}秒")

        except Exception as e:
            # 失败
            self.consecutive_failures += 1

            error_msg = f"{task_name}任务失败: {type(e).__name__}: {e}"
            logger.error(error_msg, exc_info=True)

            # 连续失败告警
            if self.consecutive_failures >= 3:
                logger.warning(
                    f"⚠ 警告：爬虫任务已连续失败 {self.consecutive_failures} 次！"
                )

            # 重新抛出异常，让调度器记录
            raise

    def get_status(self) -> dict:
        """
        获取调度器状态

        Returns:
            包含调度器状态的字典
        """
        return {
            "running": self.scheduler is not None and self.scheduler.running,
            "last_run_time": self.last_run_time.isoformat() if self.last_run_time else None,
            "last_success_time": self.last_success_time.isoformat() if self.last_success_time else None,
            "consecutive_failures": self.consecutive_failures,
            "total_runs": self.total_runs,
            "total_successes": self.total_successes,
            "success_rate": (
                self.total_successes / self.total_runs
                if self.total_runs > 0
                else 0
            ),
            "cron_expression": self.cron_expression,
            "timezone": self.timezone
        }

    async def _wait_for_initial_scrape(self, timeout: int = 60) -> None:
        """
        等待初始爬虫任务完成

        Args:
            timeout: 超时时间（秒），默认60秒
        """
        logger.info(f"等待初始爬虫任务完成（最多{timeout}秒）...")

        start_time = asyncio.get_event_loop().time()

        while True:
            # 检查是否已经执行过
            if self.last_run_time is not None:
                # 任务已经开始执行，检查是否完成
                if self.last_success_time is not None or self.consecutive_failures > 0:
                    if self.last_success_time:
                        logger.info("✓ 初始爬虫任务成功完成")
                    else:
                        logger.warning("⚠ 初始爬虫任务失败，但将继续启动服务")
                    break

            # 检查超时
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                logger.warning(f"⚠ 等待初始爬虫任务超时（{timeout}秒），将继续启动服务")
                break

            # 等待一小段时间再检查
            await asyncio.sleep(0.5)

    def trigger_manual_run(self) -> None:
        """
        手动触发一次爬虫任务

        用于测试或紧急更新。
        """
        if not self.scheduler:
            logger.error("调度器未启动，无法手动触发")
            return

        logger.info("手动触发爬虫任务...")

        # 添加一个立即执行的任务
        self.scheduler.add_job(
            self._execute_scrape,
            trigger=DateTrigger(),
            id=f"manual_scrape_{int(datetime.now().timestamp())}",
            name="Manual scrape trigger",
            replace_existing=True
        )
