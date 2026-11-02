"""
审计采集器 - 把热路径上的审计行缓冲到内存队列，由后台任务批量写库

热路径约束（最重要的一条）：
    submit() 只做一次内存入队，不 await、不碰磁盘、不抛异常。
    队列满 → 丢弃最旧记录并计数；写库失败 → 保留缓冲等下次重试并计数。
    任何审计问题都不能变成转发失败。
"""
import asyncio
import logging
import threading
from collections import deque
from typing import Any, Dict, Optional

from openai_proxy.audit.config import AuditConfig
from openai_proxy.audit.record import AuditAttempt, OUTCOME_FAILURE
from openai_proxy.audit.store import AuditStore

logger = logging.getLogger(__name__)


class AuditCollector:
    """审计行缓冲与批量落库"""

    def __init__(self, config: AuditConfig, store: AuditStore, metrics: Any = None):
        self.config = config
        self.store = store
        # metrics: MetricsCollector 实例或 None（关闭 Prometheus / 依赖缺失时）
        self.metrics = metrics if config.prometheus_enabled else None

        self._pending: Dict[str, AuditAttempt] = {}
        self._order: deque = deque()
        self._state_lock = threading.Lock()
        self._task: Optional[asyncio.Task] = None
        self._closing = False

        # 运行计数器：审计自身的健康指标，通过 /admin/audit/stats 暴露
        self.submitted_total = 0
        self.finalized_total = 0
        self.skipped_total = 0
        self.dropped_total = 0
        self.write_errors_total = 0
        self.failover_total = 0
        self.last_flush_at: Optional[str] = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """启动后台刷盘任务（必须在事件循环内调用）"""
        if self._task is not None:
            return
        self._closing = False
        self._task = asyncio.create_task(self._flush_loop())
        logger.info(f"审计采集已启动: 库={self.config.db_path} 保留={self.config.retention_days}天")

    async def close(self) -> None:
        """停止刷盘任务并排空缓冲"""
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 关闭阶段不抛
                pass
            self._task = None
        try:
            await self._flush_once()
        except Exception as exc:  # noqa: BLE001 - 关闭阶段不抛
            logger.debug(f"关闭前刷盘失败: {exc}")

    # ---------- 热路径入口 ----------

    def submit(self, attempt: AuditAttempt) -> bool:
        """提交一条审计行（同步、非阻塞、不外抛异常）"""
        try:
            if self.config.record_failed_only and attempt.outcome != OUTCOME_FAILURE:
                self.skipped_total += 1
                return False

            with self._state_lock:
                key = attempt.attempt_key
                if key not in self._pending:
                    # 队列满：丢最旧的一条，保证最新数据优先可见
                    if len(self._pending) >= self.config.queue_size and self._order:
                        oldest = self._order.popleft()
                        self._pending.pop(oldest, None)
                        self.dropped_total += 1
                        self._order.append(key)
                    else:
                        self._order.append(key)
                self._pending[key] = attempt
                self.submitted_total += 1
            return True
        except Exception as exc:  # noqa: BLE001 - 审计不得影响主流程
            logger.debug(f"审计入队失败: {exc}")
            return False

    def finalize(self, attempt: AuditAttempt) -> bool:
        """补齐用量与最终结局后提交（同一 attempt 对象，落库时覆盖 stream_open 行）"""
        submitted = self.submit(attempt)
        if submitted and attempt.finalized:
            self.finalized_total += 1
        return submitted

    def record_failover(self, from_model: str, to_model: str) -> None:
        """记录一次模型切换（故障转移）事件"""
        self.failover_total += 1
        if self.metrics is None:
            return
        try:
            self.metrics.record_failover(from_platform=from_model, to_platform=to_model)
        except Exception as exc:  # noqa: BLE001 - 指标失败不影响转发
            logger.debug(f"记录故障转移指标失败: {exc}")

    def stats(self) -> Dict[str, Any]:
        """采集器自身状态"""
        from openai_proxy.audit.record import utc_now_iso

        with self._state_lock:
            pending = len(self._pending)
        return {
            "enabled": self.config.enabled,
            "pending_rows": pending,
            "submitted_total": self.submitted_total,
            "finalized_total": self.finalized_total,
            "skipped_total": self.skipped_total,
            "dropped_total": self.dropped_total,
            "write_errors_total": self.write_errors_total,
            "failover_total": self.failover_total,
            "last_flush_at": self.last_flush_at,
            "batch_size": self.config.batch_size,
            "flush_interval": self.config.flush_interval,
            "queue_size": self.config.queue_size,
            "utc_now": utc_now_iso(),
        }

    # ---------- 后台刷盘 ----------

    async def _flush_loop(self) -> None:
        """定时批量落库；任何异常只计数，任务不退出"""
        while not self._closing:
            try:
                await asyncio.sleep(self.config.flush_interval)
            except asyncio.CancelledError:
                return
            try:
                await self._flush_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001 - 刷盘异常不能让后台任务退出
                self.write_errors_total += 1
                logger.debug(f"审计刷盘异常: {exc}")

    async def _flush_once(self) -> int:
        """把缓冲行写入库；失败时保留缓冲"""
        with self._state_lock:
            if not self._order:
                return 0
            keys = list(self._order)
            rows = [self._pending[key].to_row() for key in keys if key in self._pending]

        try:
            for start in range(0, len(rows), self.config.batch_size):
                chunk = rows[start:start + self.config.batch_size]
                await asyncio.to_thread(self.store.insert_many, chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 写库失败保留缓冲，等下次重试
            self.write_errors_total += 1
            logger.debug(f"审计写库失败，保留 {len(rows)} 条待重试: {exc}")
            return 0

        # 写成功后才清空缓冲并喂指标，保证每行只喂一次
        with self._state_lock:
            for key in keys:
                attempt = self._pending.pop(key, None)
                if attempt is not None and attempt.finalized:
                    self._emit_metrics(attempt)
            self._order.clear()

        from openai_proxy.audit.record import utc_now_iso

        self.last_flush_at = utc_now_iso()
        return len(rows)

    def _emit_metrics(self, attempt: AuditAttempt) -> None:
        """行落库后喂一份给 Prometheus（每行只喂一次，口径与 SQLite 一致）"""
        if self.metrics is None:
            return
        try:
            duration_seconds = (attempt.duration_ms / 1000.0) if attempt.duration_ms else 0.0
            self.metrics.record_request(
                platform=attempt.platform or "unknown",
                model=attempt.model_name or "unknown",
                status="success" if attempt.outcome == "success" else "failure",
                duration=duration_seconds,
                error_type=attempt.error_category,
            )
            self.metrics.observe_stream_stats(attempt)
        except Exception as exc:  # noqa: BLE001 - 指标失败不影响转发
            logger.debug(f"记录请求指标失败: {exc}")
