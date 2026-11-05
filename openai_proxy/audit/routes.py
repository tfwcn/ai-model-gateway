"""
审计查询接口 - /admin/usage 与 /admin/audit

鉴权：这些接口一律要求 GATEWAY_API_KEY；未配置密钥时返回 403（见 service.py 的中间件）。
所有 SQL 都在 query 层用占位符绑定，接口层只校验参数取值。
"""
import asyncio
import logging
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from openai_proxy.audit.csv_export import DimensionCsvExporter
from openai_proxy.audit.manager import AuditManager
from openai_proxy.audit.query import UsageQueryService, parse_time
from openai_proxy.core.api_errors import ApiError

logger = logging.getLogger(__name__)


class PruneRequest(BaseModel):
    """清理请求：POST 方式用 JSON body 传截止时间"""

    before: str = Field(..., description="ISO 时间、日期或 30d")


def build_audit_router(
    audit_provider: Callable[[], AuditManager],
    dimension_provider: Optional[Callable[[], list]] = None,
) -> APIRouter:
    """
    构建审计路由

    Args:
        audit_provider: 返回当前 AuditManager 的函数（服务启动后才打开库，因此用 provider 而不是实例）
        dimension_provider: 返回当前透传路由维度配置的函数，供用量页做候选
    """
    router = APIRouter()

    def _queries() -> UsageQueryService:
        manager = audit_provider()
        return manager.query_service

    def _routes() -> list:
        if dimension_provider is None:
            return []
        try:
            return dimension_provider() or []
        except Exception as exc:  # noqa: BLE001 - 候选拿不到不影响查询
            logger.debug(f"读取透传维度配置失败: {exc}")
            return []

    @router.post("/admin/usage/summary")
    async def usage_summary(
        group_by: str = Query("model", description="model|platform|user|endpoint|selector|outcome|error|day|hour"),
        since: Optional[str] = Query(None, description="ISO 时间、日期或 7d/24h/30m"),
        until: Optional[str] = None,
        platform: Optional[str] = None,
        model: Optional[str] = None,
        endpoint: Optional[str] = None,
        user: Optional[str] = Query(None, description="只看某个用户标签"),
        limit: int = Query(100, ge=1, le=1000),
        sort: str = Query("requests", description="requests|p95|duration|ttft|throughput|failover|errors|tokens"),
    ) -> Dict[str, Any]:
        """按维度聚合用量与性能指标"""
        return await _run(
            _queries().summary, group_by, since, until, platform, model, endpoint, limit, sort, user
        )

    @router.post("/admin/usage/daily")
    async def usage_daily(
        since: Optional[str] = Query(None, description="ISO 时间、日期或 7d/24h/30m"),
        until: Optional[str] = None,
        user: Optional[str] = Query(None, description="只看某个用户标签"),
        limit: int = Query(2000, ge=1, le=5000),
    ) -> Dict[str, Any]:
        """全量日用量（按日期 + 用户），不受审计明细保留期影响"""
        return await _run(_queries().daily_usage, since, until, user, limit)

    @router.post("/admin/usage/timeseries")
    async def usage_timeseries(
        bucket: str = Query("hour", description="hour|day"),
        since: Optional[str] = None,
        until: Optional[str] = None,
        platform: Optional[str] = None,
        model: Optional[str] = None,
        endpoint: Optional[str] = None,
        user: Optional[str] = Query(None, description="只看某个用户标签"),
    ) -> Dict[str, Any]:
        """按小时/天分桶的时序数据"""
        return await _run(_queries().timeseries, bucket, since, until, platform, model, endpoint, user)

    @router.post("/admin/usage/models")
    async def usage_models(
        sort: str = Query("requests", description="requests|p95|duration|ttft|throughput|failover|errors|tokens"),
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = Query(20, ge=1, le=500),
        user: Optional[str] = Query(None, description="只看某个用户标签"),
    ) -> Dict[str, Any]:
        """模型对比榜：慢模型、不稳定模型、用量大户"""
        return await _run(_queries().model_ranking, sort, since, until, limit, user)

    @router.post("/admin/usage/dimensions")
    async def usage_dimensions(
        group_by: str = Query("model", description="逗号分隔：model / model,size / day,user,model,size"),
        since: Optional[str] = None,
        until: Optional[str] = None,
        user: Optional[str] = Query(None, description="只看某个用户标签"),
        endpoint: Optional[str] = Query(None, description="只看某个入口，例如 passthrough:example-service"),
        limit: int = Query(200, ge=1, le=1000),
    ) -> Dict[str, Any]:
        """永久维度日汇总：按任意维度组合看调用量"""
        return await _run(_queries().dimension_usage, group_by, since, until, user, endpoint, limit)

    @router.post("/admin/usage/dimensions/export")
    async def usage_dimensions_export(
        group_by: str = Query("model", description="逗号分隔：model / model,size / day,user,model,size"),
        since: Optional[str] = None,
        until: Optional[str] = None,
        user: Optional[str] = Query(None, description="只看某个用户标签"),
        endpoint: Optional[str] = Query(None, description="只看某个入口，例如 passthrough:example-service"),
    ) -> Response:
        """永久维度日汇总导出 CSV（全部匹配行，不受页面分页限制）"""
        result = await _run(
            _queries().dimension_usage, group_by, since, until, user, endpoint, None
        )
        content = DimensionCsvExporter(result["group_by"], result["items"]).render()
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="dimensions.csv"'},
        )

    @router.post("/admin/usage/dimension-keys")
    async def usage_dimension_keys() -> Dict[str, Any]:
        """维度候选项：已配置的透传路由 + 历史表里出现过的维度名"""
        stored = await asyncio.to_thread(_queries().dimension_keys)
        routes = _routes()
        configured = []
        for route in routes:
            if not isinstance(route, dict):
                continue
            configured.extend(route.get("dimensions") or [])
        return {
            "routes": routes,
            "stored": stored,
            "keys": sorted({str(key) for key in configured + stored if str(key).strip()}),
        }

    @router.post("/admin/audit")
    async def audit_list(
        since: Optional[str] = None,
        until: Optional[str] = None,
        platform: Optional[str] = None,
        model: Optional[str] = None,
        outcome: Optional[str] = None,
        endpoint: Optional[str] = None,
        gateway_request_id: Optional[str] = None,
        user: Optional[str] = Query(None, description="只看某个用户标签"),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> Dict[str, Any]:
        """审计明细下钻"""
        try:
            return await asyncio.to_thread(
                _queries().list_attempts, since, until, platform, model, outcome, endpoint,
                gateway_request_id, limit, offset, user,
            )
        except ValueError as exc:
            raise ApiError(400, "invalid_parameter", str(exc)) from exc

    @router.post("/admin/audit/chain/{gateway_request_id}")
    async def audit_chain(gateway_request_id: str) -> Dict[str, Any]:
        """还原一次请求的完整故障转移链"""
        return await asyncio.to_thread(_queries().chain, gateway_request_id)

    @router.post("/admin/audit/stats")
    async def audit_stats() -> Dict[str, Any]:
        """审计自身的健康状态：行数、库体积、丢弃计数、最近刷盘时间"""
        return audit_provider().health()

    @router.post("/admin/audit/prune")
    async def audit_prune_post(payload: PruneRequest) -> Dict[str, Any]:
        """删除指定时间之前的审计记录（POST 版本，推荐）"""
        return await _prune(payload.before)

    async def audit_prune(before: str = Query(..., description="ISO 时间、日期或 30d")) -> Dict[str, Any]:
        """删除指定时间之前的审计记录（保留 DELETE 以兼容旧调用）"""
        return await _prune(before)

    async def _prune(before: str) -> Dict[str, Any]:
        """按时间清理审计明细（两种方法共用）"""
        try:
            cutoff = parse_time(before)
        except ValueError as exc:
            raise ApiError(400, "invalid_parameter", str(exc)) from exc
        removed = await asyncio.to_thread(_queries().prune, cutoff)
        return {"deleted": removed, "before": cutoff}

    @router.post("/metrics")
    @router.get("/metrics")
    async def metrics() -> Response:
        """Prometheus 指标（来自审计落库时的同步打点）"""
        manager = audit_provider()
        if not manager.config.prometheus_enabled:
            raise ApiError(404, "metrics_disabled", "Prometheus 指标已关闭（audit.prometheus_enabled=false）")
        from openai_proxy.utils.metrics import metrics_collector

        return Response(
            content=metrics_collector.get_metrics(),
            media_type=metrics_collector.get_metrics_content_type(),
        )

    return router


async def _run(func: Callable[..., Dict[str, Any]], *args: Any) -> Dict[str, Any]:
    """在线程池执行 SQLite 查询，并把手动传入的非法参数转成统一错误"""
    try:
        return await asyncio.to_thread(func, *args)
    except ValueError as exc:
        raise ApiError(400, "invalid_parameter", str(exc)) from exc
