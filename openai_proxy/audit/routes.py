"""
审计查询接口 - /admin/usage 与 /admin/audit

鉴权：这些接口一律要求 GATEWAY_API_KEY；未配置密钥时返回 403（见 service.py 的中间件）。
所有 SQL 都在 query 层用占位符绑定，接口层只校验参数取值。
"""
import asyncio
import logging
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Response

from openai_proxy.audit.manager import AuditManager
from openai_proxy.audit.query import UsageQueryService, parse_time

logger = logging.getLogger(__name__)


def build_audit_router(audit_provider: Callable[[], AuditManager]) -> APIRouter:
    """
    构建审计路由

    Args:
        audit_provider: 返回当前 AuditManager 的函数（服务启动后才打开库，因此用 provider 而不是实例）
    """
    router = APIRouter()

    def _queries() -> UsageQueryService:
        manager = audit_provider()
        return manager.query_service

    @router.get("/admin/usage/summary")
    async def usage_summary(
        group_by: str = Query("model", description="model|platform|endpoint|selector|outcome|error|day|hour"),
        since: Optional[str] = Query(None, description="ISO 时间、日期或 7d/24h/30m"),
        until: Optional[str] = None,
        platform: Optional[str] = None,
        model: Optional[str] = None,
        endpoint: Optional[str] = None,
        limit: int = Query(100, ge=1, le=1000),
        sort: str = Query("requests", description="requests|p95|duration|ttft|throughput|failover|errors|tokens"),
    ) -> Dict[str, Any]:
        """按维度聚合用量与性能指标"""
        return await _run(_queries().summary, group_by, since, until, platform, model, endpoint, limit, sort)

    @router.get("/admin/usage/timeseries")
    async def usage_timeseries(
        bucket: str = Query("hour", description="hour|day"),
        since: Optional[str] = None,
        until: Optional[str] = None,
        platform: Optional[str] = None,
        model: Optional[str] = None,
        endpoint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """按小时/天分桶的时序数据"""
        return await _run(_queries().timeseries, bucket, since, until, platform, model, endpoint)

    @router.get("/admin/usage/models")
    async def usage_models(
        sort: str = Query("requests", description="requests|p95|duration|ttft|throughput|failover|errors|tokens"),
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = Query(20, ge=1, le=500),
    ) -> Dict[str, Any]:
        """模型对比榜：慢模型、不稳定模型、用量大户"""
        return await _run(_queries().model_ranking, sort, since, until, limit)

    @router.get("/admin/audit")
    async def audit_list(
        since: Optional[str] = None,
        until: Optional[str] = None,
        platform: Optional[str] = None,
        model: Optional[str] = None,
        outcome: Optional[str] = None,
        endpoint: Optional[str] = None,
        gateway_request_id: Optional[str] = None,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> Dict[str, Any]:
        """审计明细下钻"""
        try:
            return await asyncio.to_thread(
                _queries().list_attempts, since, until, platform, model, outcome, endpoint,
                gateway_request_id, limit, offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/admin/audit/chain/{gateway_request_id}")
    async def audit_chain(gateway_request_id: str) -> Dict[str, Any]:
        """还原一次请求的完整故障转移链"""
        return await asyncio.to_thread(_queries().chain, gateway_request_id)

    @router.get("/admin/audit/stats")
    async def audit_stats() -> Dict[str, Any]:
        """审计自身的健康状态：行数、库体积、丢弃计数、最近刷盘时间"""
        return audit_provider().health()

    @router.delete("/admin/audit")
    async def audit_prune(before: str = Query(..., description="ISO 时间、日期或 30d")) -> Dict[str, Any]:
        """删除指定时间之前的审计记录"""
        try:
            cutoff = parse_time(before)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        removed = await asyncio.to_thread(_queries().prune, cutoff)
        return {"deleted": removed, "before": cutoff}

    @router.get("/metrics")
    async def metrics() -> Response:
        """Prometheus 指标（来自审计落库时的同步打点）"""
        manager = audit_provider()
        if not manager.config.prometheus_enabled:
            raise HTTPException(status_code=404, detail="Prometheus 指标已关闭（audit.prometheus_enabled=false）")
        from openai_proxy.utils.metrics import metrics_collector

        return Response(
            content=metrics_collector.get_metrics(),
            media_type=metrics_collector.get_metrics_content_type(),
        )

    return router


async def _run(func: Callable[..., Dict[str, Any]], *args: Any) -> Dict[str, Any]:
    """在线程池执行 SQLite 查询，并把手动传入的非法参数转成 400"""
    try:
        return await asyncio.to_thread(func, *args)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
