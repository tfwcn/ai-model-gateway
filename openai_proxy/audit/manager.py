"""
审计管理器 - 组装配置、存储、采集器与查询服务，供服务层与 CLI 共用

对外只暴露三个动作：
    start()   打开库、建表、按保留期清理、启动后台刷盘
    close()   排空缓冲并关闭
    其余都是「拿上下文 / 查统计 / 记一条配置变更」
"""
import asyncio
import logging
import uuid
from typing import Any, Dict, Optional

from openai_proxy.audit.collector import AuditCollector
from openai_proxy.audit.config import AuditConfig
from openai_proxy.audit.context import AuditContext
from openai_proxy.audit.query import UsageQueryService
from openai_proxy.audit.record import AuditAttempt, OUTCOME_FAILURE, OUTCOME_SUCCESS, utc_now_iso
from openai_proxy.audit.store import AuditStore, NullAuditStore, SqliteAuditStore

logger = logging.getLogger(__name__)


class AuditManager:
    """审计子系统入口"""

    def __init__(self, config: Optional[AuditConfig] = None, metrics: Any = None):
        self.config = config or AuditConfig.from_env()
        self.store: AuditStore = NullAuditStore()
        self.collector: Optional[AuditCollector] = None
        self.query_service: UsageQueryService = UsageQueryService(self.store)
        self._metrics = metrics
        self._started = False

    # ---------- 生命周期 ----------

    def apply_yaml(self, section: Optional[Dict[str, Any]]) -> None:
        """应用配置文件里的 audit: 段（必须在 start 之前调用）"""
        self.config.apply_yaml(section)

    async def start(self) -> None:
        """打开存储并启动刷盘；失败时降级为关闭审计，不影响服务"""
        if self._started:
            return
        self._started = True

        if not self.config.enabled:
            logger.info("审计已关闭（AUDIT_ENABLED=false 或 audit.enabled=false）")
            return

        try:
            store = SqliteAuditStore(self.config.db_path)
            await asyncio.to_thread(store.init_schema)
            removed = await asyncio.to_thread(store.prune_old, self.config.retention_days)
            if removed:
                logger.info(f"审计记录按保留期清理: 删除 {removed} 条（{self.config.retention_days} 天前）")

            self.store = store
            self.query_service = UsageQueryService(store)
            self.collector = AuditCollector(
                self.config, store, metrics=self._metrics or load_metrics_collector()
            )
            self.collector.start()
        except Exception as exc:  # noqa: BLE001 - 审计故障不能阻止网关启动
            logger.error(f"审计初始化失败，已降级为关闭审计: {exc}", exc_info=True)
            self.config.enabled = False
            self.store = NullAuditStore()
            self.query_service = UsageQueryService(self.store)
            self.collector = None

    async def close(self) -> None:
        """排空缓冲并关闭库"""
        if self.collector is not None:
            await self.collector.close()
            self.collector = None
        try:
            await asyncio.to_thread(self.store.close)
        except Exception as exc:  # noqa: BLE001 - 关闭阶段不抛
            logger.debug(f"关闭审计存储失败: {exc}")
        self.store = NullAuditStore()
        self._started = False

    # ---------- 热路径入口 ----------

    def new_context(
        self,
        endpoint: str,
        selector: str = "",
        stream: bool = False,
        msg_count: Optional[int] = None,
    ) -> AuditContext:
        """创建一次请求的审计上下文（审计关闭时 collector 为 None，全部退化为空操作）"""
        return AuditContext(
            collector=self.collector,
            endpoint=endpoint,
            selector=selector,
            stream=stream,
            msg_count=msg_count if self.config.include_request_meta else None,
        )

    def record_config_change(
        self,
        op: str,
        detail: str = "",
        success: bool = True,
    ) -> bool:
        """把一次配置写入也记进审计（endpoint=config_write，可在 /admin/audit 查历史）"""
        if self.collector is None:
            return False
        request_id = uuid.uuid4().hex[:8]
        attempt = AuditAttempt(
            attempt_key=f"config-{request_id}",
            gateway_request_id=request_id,
            endpoint="config_write",
            stream=False,
            selector=op,
            platform="config",
            model_name=op,
            finalized=True,
            outcome=OUTCOME_SUCCESS if success else OUTCOME_FAILURE,
            error_code=None if success else "config_write_failed",
            ts=utc_now_iso(),
        )
        attempt.output_chars = len(detail) if detail else None
        attempt.upstream_id = detail[:200] if detail else None
        return self.collector.submit(attempt)

    # ---------- 状态 ----------

    def health(self) -> Dict[str, Any]:
        """审计自身健康状态（审计关闭时也要能返回，界面靠它判断状态）"""
        collector_stats = self.collector.stats() if self.collector else {"enabled": self.config.enabled}
        return {
            "config": self.config.to_dict(),
            "collector": collector_stats,
            "store": self.store.stats() if self.store else {"enabled": False, "rows": 0},
        }


def load_metrics_collector() -> Any:
    """安全加载 Prometheus 收集器（依赖缺失时返回 None，服务照常运行）"""
    try:
        from openai_proxy.utils.metrics import metrics_collector

        return metrics_collector
    except Exception as exc:  # noqa: BLE001 - prometheus_client 未安装时降级
        logger.debug(f"Prometheus 指标不可用: {exc}")
        return None
