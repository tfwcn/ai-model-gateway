"""
用量与审计命令 - 直接读审计库，不依赖网关进程

审计配置来源与运行时一致：默认值 -> AUDIT_* 环境变量 -> models.yaml 顶层 audit: 段。
"""
import logging
import os
from typing import Any, Dict, List, Optional

from openai_proxy.audit.config import AuditConfig
from openai_proxy.audit.query import UsageQueryService, parse_time
from openai_proxy.audit.store import SqliteAuditStore
from openai_proxy.cli.codes import EXIT_ERROR, EXIT_OK
from openai_proxy.cli.output import Console
from openai_proxy.config_store.document import ConfigDocument

logger = logging.getLogger(__name__)


class AuditUnavailable(Exception):
    """审计库不存在或审计已关闭"""


class AuditAccess:
    """按运行时同样的规则还原审计配置，并打开只读查询连接"""

    def __init__(self, config_file: str):
        self.config_file = config_file
        self.audit_config = AuditConfig.from_env()
        self.audit_config.apply_yaml(self._yaml_section())
        self._store: Optional[SqliteAuditStore] = None

    def queries(self) -> UsageQueryService:
        """返回查询服务；审计库文件不存在时直接报错，避免误建空库"""
        if self._store is None:
            if not self.audit_config.enabled:
                raise AuditUnavailable("审计已关闭（AUDIT_ENABLED=false 或 audit.enabled=false）")
            if not os.path.exists(self.audit_config.db_path):
                raise AuditUnavailable(f"审计库不存在: {self.audit_config.db_path}（网关还没产生审计记录？）")
            self._store = SqliteAuditStore(self.audit_config.db_path)
            self._store.init_schema()
        return UsageQueryService(self._store)

    def store(self) -> SqliteAuditStore:
        self.queries()
        assert self._store is not None
        return self._store

    def stats(self) -> Dict[str, Any]:
        try:
            return self.store().stats()
        except AuditUnavailable as exc:
            return {"error": str(exc)}

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    def _yaml_section(self) -> Dict[str, Any]:
        if not os.path.exists(self.config_file):
            return {}
        try:
            return ConfigDocument.load(self.config_file).audit or {}
        except Exception as exc:  # noqa: BLE001 - 配置文件读不动时退回环境变量
            logger.debug(f"读取 audit: 段失败: {exc}")
            return {}


class UsageCommands:
    """usage 各动作"""

    def __init__(self, access: AuditAccess, console: Console):
        self.access = access
        self.console = console

    def usage_summary(self, args) -> int:
        result = self._call(lambda queries: queries.summary(
            args.group_by, args.since, args.until, args.platform, args.model, args.endpoint, args.limit, args.sort,
            args.user,
        ))
        if result is None:
            return EXIT_ERROR
        if self.console.as_json:
            self.console.emit(result)
            return EXIT_OK

        self.console.line(f"按 {result['group_by']} 聚合，共 {result['count']} 组")
        self.console.table(_display_rows(result["items"]), _aggregate_columns("bucket"))
        return EXIT_OK

    def usage_models(self, args) -> int:
        result = self._call(lambda queries: queries.model_ranking(
            args.sort, args.since, args.until, args.limit, args.user
        ))
        if result is None:
            return EXIT_ERROR
        if self.console.as_json:
            self.console.emit(result)
            return EXIT_OK

        self.console.line(f"模型榜（按 {result['sort']} 排序）")
        self.console.table(_display_rows(result["items"]), _aggregate_columns("bucket"))
        return EXIT_OK

    def usage_timeseries(self, args) -> int:
        result = self._call(lambda queries: queries.timeseries(
            args.bucket, args.since, args.until, args.platform, args.model, args.endpoint, args.user
        ))
        if result is None:
            return EXIT_ERROR
        if self.console.as_json:
            self.console.emit(result)
            return EXIT_OK

        self.console.line(f"按 {result['bucket']} 分桶")
        self.console.table(_display_rows(result["points"]), _aggregate_columns("bucket"))
        return EXIT_OK

    def usage_daily(self, args) -> int:
        result = self._call(lambda queries: queries.daily_usage(
            args.since, args.until, args.user, args.limit
        ))
        if result is None:
            return EXIT_ERROR
        if self.console.as_json:
            self.console.emit(result)
            return EXIT_OK

        totals = result["totals"]
        self.console.line(
            f"全量日用量（{result['count']} 行）: "
            f"输入 {totals.get('input_tokens', 0)} / 缓存 {totals.get('cached_tokens', 0)} / "
            f"输出 {totals.get('output_tokens', 0)} tokens"
        )
        self.console.table(result["items"], [
            ("day", "日期"),
            ("api_user", "用户"),
            ("requests", "请求"),
            ("attempts", "尝试"),
            ("input_tokens", "输入tokens"),
            ("cached_tokens", "缓存tokens"),
            ("output_tokens", "输出tokens"),
            ("success", "成功"),
            ("failure", "失败"),
        ])
        return EXIT_OK

    def usage_dimensions(self, args) -> int:
        """永久维度日汇总：按任意维度组合看调用量"""
        result = self._call(lambda queries: queries.dimension_usage(
            args.group_by, args.since, args.until, args.user, args.endpoint, args.limit
        ))
        if result is None:
            return EXIT_ERROR
        if self.console.as_json:
            self.console.emit(result)
            return EXIT_OK

        columns = [(name, name) for name in result["group_by"]]
        self.console.line(f"维度汇总（按 {args.group_by}，共 {result['count']} 组）")
        rows = [
            {**item, **(item.get("values") or {})}
            for item in result["items"]
        ]
        self.console.table(_display_rows(rows), columns + [
            ("requests", "请求"),
            ("attempts", "尝试"),
            ("success", "成功"),
            ("failure", "失败"),
            ("aborted", "中断"),
            ("success_rate", "成功率"),
        ])
        return EXIT_OK

    def usage_attempts(self, args) -> int:
        result = self._call(lambda queries: queries.list_attempts(
            args.since, args.until, args.platform, args.model, args.outcome, args.endpoint,
            None, args.limit, args.offset, args.user
        ))
        if result is None:
            return EXIT_ERROR
        if self.console.as_json:
            self.console.emit(result)
            return EXIT_OK

        self.console.line(f"审计明细（共 {result['total']} 条，显示 {result['limit']} 条）")
        self.console.table(_attempt_rows(result["items"]), [
            ("ts", "时间"),
            ("gateway_request_id", "请求"),
            ("api_user", "用户"),
            ("attempt_seq", "第几次"),
            ("model_name", "模型"),
            ("outcome", "结果"),
            ("done_label", "[DONE]"),
            ("duration_ms", "耗时ms"),
            ("ttft_ms", "首块ms"),
            ("http_status", "状态码"),
            ("error_category", "错误分类"),
            ("error_code", "错误码"),
        ])
        self.console.line("看整条故障转移链: manage.py usage chain <请求ID>")
        return EXIT_OK

    def usage_chain(self, args) -> int:
        result = self._call(lambda queries: queries.chain(args.gateway_request_id))
        if result is None:
            return EXIT_ERROR
        if self.console.as_json:
            self.console.emit(result)
            return EXIT_OK

        attempts = result["attempts"]
        self.console.line(f"请求 {result['gateway_request_id']} 共 {len(attempts)} 次尝试")
        if not attempts:
            self.console.line("（没有这个请求的审计记录）")
            return EXIT_OK

        for attempt in attempts:
            self.console.line(
                f"  #{attempt.get('attempt_seq')} {attempt.get('model_name')} "
                f"{attempt.get('outcome')} 耗时 {attempt.get('duration_ms')}ms "
                f"首块 {attempt.get('ttft_ms')}ms {attempt.get('error_code') or ''}".rstrip()
            )
            self.console.line(
                f"     错误分类: {attempt.get('error_category') or '-'} / {attempt.get('error_code') or '-'} "
                f"状态码 {attempt.get('http_status') or '-'} 禁用模型: {'是' if attempt.get('disabled_model') else '否'} "
                f"[DONE]: {_done_label(attempt)}"
            )
        return EXIT_OK

    def usage_stats(self, args) -> int:
        stats = self.access.stats()

        if self.console.as_json:
            self.console.emit({"config": self.access.audit_config.to_dict(), "store": stats})
            return EXIT_OK

        self.console.block("审计配置", [f"{key}: {value}" for key, value in self.access.audit_config.to_dict().items()])
        self.console.block("审计库", [f"{key}: {value}" for key, value in stats.items()])
        return EXIT_OK

    def usage_prune(self, args) -> int:
        try:
            cutoff = parse_time(args.before)
        except ValueError as exc:
            self.console.line(f"时间格式不对: {exc}", to_err=True)
            return EXIT_ERROR

        def run(queries: UsageQueryService) -> Dict[str, Any]:
            return {"deleted": queries.prune(cutoff), "before": cutoff}

        result = self._call(run)
        if result is None:
            return EXIT_ERROR

        if self.console.as_json:
            self.console.emit(result)
        else:
            self.console.line(f"已删除 {result['deleted']} 条（{result['before']} 之前）")
        return EXIT_OK

    # ---------- 内部 ----------

    def _call(self, action):
        """统一处理「审计不可用」与参数错误"""
        try:
            return action(self.access.queries())
        except AuditUnavailable as exc:
            self.console.line(str(exc), to_err=True)
            return None
        except ValueError as exc:
            self.console.line(f"参数不对: {exc}", to_err=True)
            return None


def _display_row(item: Dict[str, Any]) -> Dict[str, Any]:
    """成功率显示成百分数（--json 输出保持原始数值）"""
    row = dict(item)
    rate = row.get("success_rate")
    if isinstance(rate, (int, float)):
        row["success_rate"] = f"{rate * 100:.1f}%"
    return row


def _display_rows(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [_display_row(item) for item in items]


def _done_label(item: Dict[str, Any]) -> str:
    """终止标记 [DONE] 有没有转发出去：aborted 只会出现在「未发完」的行上"""
    if not item.get("stream"):
        return "-"  # 非流式调用没有 [DONE] 概念
    if "done_sent" not in item:
        return "未知"  # 老库还没补上这一列（网关启动时会自动 ALTER TABLE 补列）
    return "已发完" if item.get("done_sent") else "未发完"


def _attempt_rows(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """审计明细的终端视图：给 0/1 的 done_sent 配一个人话标签"""
    return [dict(item, done_label=_done_label(item)) for item in items]


def _aggregate_columns(bucket_key: str) -> List[tuple]:
    """聚合结果里最值得在终端看的几列"""
    return [
        (bucket_key, "分组"),
        ("requests", "请求"),
        ("attempts", "尝试"),
        ("success_rate", "成功率"),
        ("failover_count", "切换"),
        ("avg_ttft_ms", "首块ms"),
        ("p95_duration_ms", "P95ms"),
        ("avg_duration_ms", "平均ms"),
        ("avg_throughput", "tokens/s"),
        ("output_tokens", "输出tokens"),
    ]
