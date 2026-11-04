"""
用量与性能查询 - 把审计表聚合成可直接展示的统计

口径约定（与 docs/AUDIT.md 一致）：
- requests = 去重后的 gateway_request_id 数量
- attempts = 上游尝试次数（含重试与故障转移）
- 成功率只统计 finalized=1 的行，未收尾的流不算成功
- P95 用 ROW_NUMBER 名次近似（SQLite 没有 percentile 函数）
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from openai_proxy.audit.record import FINAL_OUTCOMES
from openai_proxy.audit.store import TABLE, USAGE_DIMENSION_TABLE, USAGE_TABLE, AuditStore

# 维度名白名单：只允许字母、数字、下划线、连字符
_DIMENSION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# group_by 取值 → SQL 分组表达式（白名单，杜绝注入）
_GROUP_EXPRS: Dict[str, str] = {
    "model": "COALESCE(model_name, 'unknown')",
    "platform": "COALESCE(platform, 'unknown')",
    "user": "COALESCE(NULLIF(api_user, ''), 'admin')",
    "endpoint": "endpoint",
    "selector": "COALESCE(selector, '')",
    "outcome": "outcome",
    "error": "COALESCE(error_category, 'none')",
    "day": "substr(ts, 1, 10)",
    "hour": "substr(ts, 1, 13)",
}

# timeseries 的分桶粒度
_BUCKET_EXPRS: Dict[str, str] = {"hour": "substr(ts, 1, 13)", "day": "substr(ts, 1, 10)"}

# 排序取值 → (取哪个字段, 是否降序)。
# P95 这类派生列不在 SQL 里，聚合完再在 Python 端排序（分组数量等于模型数，代价可忽略）。
_SORT_KEYS: Dict[str, tuple] = {
    "requests": ("requests", True),
    "p95": ("p95_duration_ms", True),
    "duration": ("avg_duration_ms", True),
    "ttft": ("avg_ttft_ms", True),
    "throughput": ("avg_throughput", False),
    "failover": ("failover_count", True),
    "errors": ("failure", True),
    "tokens": ("output_tokens", True),
}

_RELATIVE_RE = re.compile(r"^(\d+)\s*([dhm])$", re.IGNORECASE)

_AGGREGATE_COLUMNS = """
    COUNT(*) AS attempts,
    COUNT(DISTINCT gateway_request_id) AS requests,
    SUM(CASE WHEN finalized = 1 THEN 1 ELSE 0 END) AS finalized,
    SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END) AS success,
    SUM(CASE WHEN outcome = 'failure' THEN 1 ELSE 0 END) AS failure,
    SUM(CASE WHEN outcome = 'aborted' THEN 1 ELSE 0 END) AS aborted,
    SUM(CASE WHEN retry_seq > 1 THEN 1 ELSE 0 END) AS retries,
    SUM(CASE WHEN disabled_model = 1 THEN 1 ELSE 0 END) AS disabled_models,
    AVG(CASE WHEN finalized = 1 THEN duration_ms END) AS avg_duration_ms,
    AVG(ttft_ms) AS avg_ttft_ms,
    AVG(CASE WHEN finalized = 1 THEN probe_ms END) AS avg_probe_ms,
    AVG(throughput) AS avg_throughput,
    SUM(input_tokens) AS input_tokens,
    SUM(cached_tokens) AS cached_tokens,
    SUM(output_tokens) AS output_tokens,
    SUM(output_chars) AS output_chars
"""


def parse_time(value: Optional[str], default: Optional[str] = None) -> Optional[str]:
    """把 ISO 时间或 7d / 24h / 30m 这类相对时间统一成 ISO（UTC）"""
    if value is None or str(value).strip() == "":
        return default
    text = str(value).strip()

    relative = _RELATIVE_RE.match(text)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2).lower()
        delta = {
            "d": timedelta(days=amount),
            "h": timedelta(hours=amount),
            "m": timedelta(minutes=amount),
        }[unit]
        return (datetime.now(timezone.utc) - delta).isoformat(timespec="milliseconds")

    # 允许只写日期
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return f"{text}T00:00:00.000+00:00"

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"时间格式无效: {value}（支持 ISO 时间、日期或 7d/24h/30m）") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")


class UsageQueryService:
    """审计统计查询服务（只读）"""

    def __init__(self, store: AuditStore):
        self.store = store

    # ---------- 聚合 ----------

    def summary(
        self,
        group_by: str = "model",
        since: Optional[str] = None,
        until: Optional[str] = None,
        platform: Optional[str] = None,
        model: Optional[str] = None,
        endpoint: Optional[str] = None,
        limit: int = 100,
        sort: str = "requests",
        user: Optional[str] = None,
    ) -> Dict[str, Any]:
        """按维度聚合用量与性能指标（时间维度按先后顺序返回，其余按 sort 排序）"""
        group_expr = self._group_expr(group_by)
        if sort not in _SORT_KEYS:
            raise ValueError(f"sort 只支持: {', '.join(sorted(_SORT_KEYS))}")
        where, params = self._filters(since, until, platform, model, endpoint, user)

        rows = self.store.query(
            f"""
            SELECT {group_expr} AS bucket, {_AGGREGATE_COLUMNS}
            FROM {TABLE} WHERE {' AND '.join(where)}
            GROUP BY bucket
            """,
            params,
        )

        percentiles = self._percentiles(group_expr, where, params)
        items = [self._decorate(row, percentiles.get(str(row.get("bucket")))) for row in rows]
        if group_by in ("day", "hour"):
            items.sort(key=lambda item: str(item.get("bucket")))
        else:
            items = _sorted(items, sort)
        items = items[: max(1, min(int(limit), 1000))]
        return {
            "group_by": group_by,
            "range": {"since": parse_time(since), "until": parse_time(until) or _now_iso()},
            "count": len(items),
            "items": items,
        }

    def daily_usage(
        self,
        since: Optional[str] = None,
        until: Optional[str] = None,
        user: Optional[str] = None,
        limit: int = 2000,
    ) -> Dict[str, Any]:
        """
        全量日用量：按 (日期, 用户) 返回，不受审计明细保留期影响

        数据来自 usage_daily：审计明细会被定期清理，这张表只累加、不删除，
        因此这里能查到比审计明细更早的用量。
        """
        where: List[str] = ["1 = 1"]
        params: List[Any] = []

        since_iso = parse_time(since)
        if since_iso:
            where.append("day >= ?")
            params.append(since_iso[:10])
        until_iso = parse_time(until)
        if until_iso:
            where.append("day < ?")
            params.append(until_iso[:10])
        if user:
            where.append("api_user = ?")
            params.append(user)

        clause = " AND ".join(where)
        rows = self.store.query(
            f"""
            SELECT day, api_user, requests, attempts, success, failure, aborted, retries,
                   input_tokens, cached_tokens, output_tokens, updated_at
            FROM {USAGE_TABLE} WHERE {clause}
            ORDER BY day ASC, api_user ASC
            LIMIT ?
            """,
            params + [max(1, min(int(limit), 5000))],
        )

        totals = self.store.query(
            f"""
            SELECT
                COALESCE(SUM(requests), 0) AS requests,
                COALESCE(SUM(attempts), 0) AS attempts,
                COALESCE(SUM(success), 0) AS success,
                COALESCE(SUM(failure), 0) AS failure,
                COALESCE(SUM(aborted), 0) AS aborted,
                COALESCE(SUM(retries), 0) AS retries,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens
            FROM {USAGE_TABLE} WHERE {clause}
            """,
            params,
        )
        summary = dict(totals[0]) if totals else {}
        finalized = int(summary.get("success") or 0) + int(summary.get("failure") or 0) + int(summary.get("aborted") or 0)
        summary["success_rate"] = (
            round(int(summary.get("success") or 0) / finalized, 4) if finalized else None
        )

        return {
            "range": {"since": parse_time(since), "until": parse_time(until) or _now_iso()},
            "count": len(rows),
            "totals": summary,
            "items": rows,
        }

    def timeseries(
        self,
        bucket: str = "hour",
        since: Optional[str] = None,
        until: Optional[str] = None,
        platform: Optional[str] = None,
        model: Optional[str] = None,
        endpoint: Optional[str] = None,
        user: Optional[str] = None,
    ) -> Dict[str, Any]:
        """按小时/天分桶的时序数据，供前端画折线"""
        bucket_expr = _BUCKET_EXPRS.get(bucket)
        if bucket_expr is None:
            raise ValueError(f"bucket 只支持: {', '.join(sorted(_BUCKET_EXPRS))}")
        where, params = self._filters(since, until, platform, model, endpoint, user)

        rows = self.store.query(
            f"""
            SELECT {bucket_expr} AS bucket, {_AGGREGATE_COLUMNS}
            FROM {TABLE} WHERE {' AND '.join(where)}
            GROUP BY bucket
            ORDER BY bucket ASC
            """,
            params,
        )
        percentiles = self._percentiles(bucket_expr, where, params)
        return {
            "bucket": bucket,
            "range": {"since": parse_time(since), "until": parse_time(until) or _now_iso()},
            "points": [self._decorate(row, percentiles.get(str(row.get("bucket")))) for row in rows],
        }

    def model_ranking(
        self,
        sort: str = "requests",
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 20,
        user: Optional[str] = None,
    ) -> Dict[str, Any]:
        """模型横向对比榜：慢模型、不稳定模型、用量大户"""
        if sort not in _SORT_KEYS:
            raise ValueError(f"sort 只支持: {', '.join(sorted(_SORT_KEYS))}")
        where, params = self._filters(since, until, user=user)
        group_expr = _GROUP_EXPRS["model"]

        # 分组数量等于模型数，先全量聚合再合并 P95，最后按任意维度排序
        rows = self.store.query(
            f"""
            SELECT {group_expr} AS bucket, {_AGGREGATE_COLUMNS}
            FROM {TABLE} WHERE {' AND '.join(where)} AND model_name IS NOT NULL
            GROUP BY bucket
            """,
            params,
        )
        percentiles = self._percentiles(group_expr, where, params)
        items = [self._decorate(row, percentiles.get(str(row.get("bucket")))) for row in rows]
        items = _sorted(items, sort)[: max(1, min(int(limit), 500))]
        return {
            "sort": sort,
            "range": {"since": parse_time(since), "until": parse_time(until) or _now_iso()},
            "items": items,
        }

    # ---------- 维度汇总 ----------

    def dimension_usage(
        self,
        group_by: str = "model",
        since: Optional[str] = None,
        until: Optional[str] = None,
        user: Optional[str] = None,
        endpoint: Optional[str] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """
        查询永久维度日汇总表（usage_dimension_daily）

        group_by 是逗号分隔的分组键，支持 day / user / endpoint 与任意维度名，
        例如 `model`、`model,size`、`day,user,model,size`。
        """
        names = _parse_dimension_group(group_by)
        exprs = [_dimension_group_expr(name) for name in names]
        aliases = [f"g{index}" for index in range(len(exprs))]
        select_group = ", ".join(f"{expr} AS {alias}" for expr, alias in zip(exprs, aliases))
        group_clause = ", ".join(aliases)
        bucket_expr = _bucket_from_exprs(exprs)

        where: List[str] = ["1 = 1"]
        params: List[Any] = []
        since_iso = parse_time(since)
        if since_iso:
            where.append("day >= ?")
            params.append(since_iso[:10])
        until_iso = parse_time(until)
        if until_iso:
            where.append("day < ?")
            params.append(until_iso[:10])
        if user:
            where.append("api_user = ?")
            params.append(user)
        if endpoint:
            where.append("endpoint = ?")
            params.append(endpoint)

        rows = self.store.query(
            f"""
            SELECT {select_group}, {bucket_expr} AS bucket,
                   COALESCE(SUM(requests), 0) AS requests,
                   COALESCE(SUM(attempts), 0) AS attempts,
                   COALESCE(SUM(success), 0) AS success,
                   COALESCE(SUM(failure), 0) AS failure,
                   COALESCE(SUM(aborted), 0) AS aborted,
                   COALESCE(SUM(retries), 0) AS retries,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens
            FROM {USAGE_DIMENSION_TABLE}
            WHERE {' AND '.join(where)}
            GROUP BY {group_clause}
            ORDER BY requests DESC, bucket ASC
            LIMIT ?
            """,
            params + [max(1, min(int(limit), 1000))],
        )

        items = []
        for row in rows:
            item = dict(row)
            item["values"] = {name: item.pop(alias) for name, alias in zip(names, aliases)}
            finalized = int(item.get("success") or 0) + int(item.get("failure") or 0) + int(item.get("aborted") or 0)
            item["success_rate"] = (
                round(int(item.get("success") or 0) / finalized, 4) if finalized else None
            )
            items.append(item)
        return {
            "group_by": names,
            "range": {"since": parse_time(since), "until": parse_time(until) or _now_iso()},
            "count": len(items),
            "items": items,
        }

    def dimension_keys(self) -> List[str]:
        """维度日汇总表里出现过的全部维度名（按名称排序）"""
        try:
            rows = self.store.query(
                f"""
                SELECT DISTINCT key
                FROM {USAGE_DIMENSION_TABLE}, json_each({USAGE_DIMENSION_TABLE}.dimensions)
                WHERE key IS NOT NULL AND key != ''
                ORDER BY key ASC
                """
            )
            return [str(row["key"]) for row in rows]
        except Exception:  # noqa: BLE001 - 极老 SQLite 没有 JSON1 时降级
            keys = set()
            for row in self.store.query(f"SELECT dimensions FROM {USAGE_DIMENSION_TABLE}"):
                try:
                    import json

                    payload = json.loads(row.get("dimensions") or "{}")
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict):
                    keys.update(str(key) for key in payload if str(key).strip())
            return sorted(keys)

    # ---------- 明细 ----------

    def list_attempts(
        self,
        since: Optional[str] = None,
        until: Optional[str] = None,
        platform: Optional[str] = None,
        model: Optional[str] = None,
        outcome: Optional[str] = None,
        endpoint: Optional[str] = None,
        gateway_request_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        user: Optional[str] = None,
    ) -> Dict[str, Any]:
        """审计明细下钻"""
        where, params = self._filters(since, until, platform, model, endpoint, user)
        if outcome:
            if outcome not in FINAL_OUTCOMES + ("stream_open",):
                raise ValueError(f"outcome 只支持: {', '.join(FINAL_OUTCOMES + ('stream_open',))}")
            where.append("outcome = ?")
            params.append(outcome)
        if gateway_request_id:
            where.append("gateway_request_id = ?")
            params.append(gateway_request_id)

        clause = " AND ".join(where)
        total_rows = self.store.query(f"SELECT COUNT(*) AS total FROM {TABLE} WHERE {clause}", params)
        total = int(total_rows[0]["total"]) if total_rows else 0

        items = self.store.query(
            f"""
            SELECT * FROM {TABLE} WHERE {clause}
            ORDER BY ts DESC, attempt_seq ASC
            LIMIT ? OFFSET ?
            """,
            params + [max(1, min(int(limit), 500)), max(0, int(offset))],
        )
        return {"total": total, "limit": limit, "offset": offset, "items": items}

    def chain(self, gateway_request_id: str) -> Dict[str, Any]:
        """还原一次请求的完整故障转移链"""
        items = self.store.query(
            f"SELECT * FROM {TABLE} WHERE gateway_request_id = ? ORDER BY attempt_seq ASC",
            (gateway_request_id,),
        )
        return {"gateway_request_id": gateway_request_id, "attempts": items}

    def prune(self, before: str) -> int:
        """删除指定时间之前的记录"""
        return self.store.prune(parse_time(before))

    # ---------- 内部 ----------

    def _group_expr(self, group_by: str) -> str:
        expr = _GROUP_EXPRS.get(group_by)
        if expr is not None:
            return expr
        if group_by.startswith("dim:"):
            names = _parse_dimension_names(group_by[4:])
            return _dimension_bucket_expr(names)
        raise ValueError(f"group_by 只支持: {', '.join(sorted(_GROUP_EXPRS))} 或 dim:<维度名>")

    @staticmethod
    def _filters(
        since: Optional[str] = None,
        until: Optional[str] = None,
        platform: Optional[str] = None,
        model: Optional[str] = None,
        endpoint: Optional[str] = None,
        user: Optional[str] = None,
    ) -> Tuple[List[str], List[Any]]:
        """组装 WHERE 条件（全部占位符绑定）"""
        where: List[str] = ["1 = 1"]
        params: List[Any] = []

        since_iso = parse_time(since)
        if since_iso:
            where.append("ts >= ?")
            params.append(since_iso)
        until_iso = parse_time(until)
        if until_iso:
            where.append("ts < ?")
            params.append(until_iso)
        if platform:
            where.append("platform = ?")
            params.append(platform)
        if model:
            where.append("(model_name = ? OR model_id = ?)")
            params.extend([model, model])
        if endpoint:
            where.append("endpoint = ?")
            params.append(endpoint)
        if user:
            # 与分组口径保持一致：历史行没有 api_user 时归到 admin
            where.append("COALESCE(NULLIF(api_user, ''), 'admin') = ?")
            params.append(user)
        return where, params

    def _percentiles(self, group_expr: str, where: Sequence[str], params: Sequence[Any]) -> Dict[str, Dict[str, Any]]:
        """
        按组算 P95（ROW_NUMBER 近似）

        SQLite 没有 percentile 函数，也没有 CEIL；用整数运算求 ceil(n * 0.95) 的名次。
        窗口函数不可用（SQLite < 3.25）时降级为不出 P95。
        """
        clause = " AND ".join(where)
        try:
            rows = self.store.query(
                f"""
                WITH ranked AS (
                    SELECT {group_expr} AS bucket, duration_ms, probe_ms, ttft_ms, throughput,
                           ROW_NUMBER() OVER (PARTITION BY {group_expr} ORDER BY duration_ms) AS rn,
                           COUNT(*) OVER (PARTITION BY {group_expr}) AS group_rows
                    FROM {TABLE}
                    WHERE {clause} AND finalized = 1 AND duration_ms IS NOT NULL
                )
                SELECT bucket,
                       MAX(CASE WHEN rn = (group_rows * 95 + 99) / 100 THEN duration_ms END) AS p95_duration_ms,
                       MAX(CASE WHEN rn = (group_rows * 50 + 99) / 100 THEN duration_ms END) AS p50_duration_ms,
                       MAX(duration_ms) AS max_duration_ms,
                       MIN(duration_ms) AS min_duration_ms
                FROM ranked
                GROUP BY bucket
                """,
                params,
            )
        except Exception:  # noqa: BLE001 - 窗口函数不可用时降级
            return {}
        return {str(row.get("bucket")): row for row in rows}

    @staticmethod
    def _decorate(row: Dict[str, Any], percentile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """补派生指标：成功率、平均尝试次数，并把 None 归一为 None 而不是 0"""
        attempts = int(row.get("attempts") or 0)
        requests = int(row.get("requests") or 0)
        finalized = int(row.get("finalized") or 0)
        success = int(row.get("success") or 0)

        item = dict(row)
        item["success_rate"] = round(success / finalized, 4) if finalized else None
        item["avg_attempts"] = round(attempts / requests, 2) if requests else None
        item["failover_count"] = max(0, attempts - requests)
        if percentile:
            item["p50_duration_ms"] = percentile.get("p50_duration_ms")
            item["p95_duration_ms"] = percentile.get("p95_duration_ms")
            item["min_duration_ms"] = percentile.get("min_duration_ms")
            item["max_duration_ms"] = percentile.get("max_duration_ms")
        for key in ("avg_duration_ms", "avg_ttft_ms", "avg_probe_ms", "avg_throughput"):
            value = item.get(key)
            if isinstance(value, (int, float)):
                item[key] = round(float(value), 2)
        return item


def _sorted(items: List[Dict[str, Any]], sort: str) -> List[Dict[str, Any]]:
    """
    按指定维度排序（P95 等派生列不在 SQL 里，只能合并后在 Python 端排）

    缺失值一律排在最后：没有数据的模型不该被当成「最慢」或「最慢的用量」。
    """
    column, descending = _SORT_KEYS[sort]

    def key(item: Dict[str, Any]) -> tuple:
        value = item.get(column)
        if not isinstance(value, (int, float)):
            return (1, 0.0)
        return (0, -float(value) if descending else float(value))

    return sorted(items, key=key)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------- 维度查询工具 ----------


def _parse_dimension_names(raw: str) -> List[str]:
    """解析 `model,size` 这类维度名列表，逐个校验白名单"""
    names = [item.strip() for item in str(raw).split(",") if item.strip()]
    if not names:
        raise ValueError("dim: 后面至少要有一个维度名，例如 dim:model,size")
    for name in names:
        _require_dimension_name(name)
    return names


def _parse_dimension_group(raw: str) -> List[str]:
    """维度汇总的分组键：允许 day/user/endpoint 与任意维度名"""
    names = [item.strip() for item in str(raw).split(",") if item.strip()]
    if not names:
        raise ValueError("group_by 至少要有一个分组键")
    for name in names:
        if name in ("day", "month", "user", "endpoint"):
            continue
        _require_dimension_name(name)
    return names


def _require_dimension_name(name: str) -> None:
    """维度名只允许安全字符，避免拼进 JSON 路径时被注入"""
    if not _DIMENSION_NAME_RE.match(name):
        raise ValueError(f"维度名不合法: {name}（只允许字母、数字、下划线、连字符）")


def _json_dimension(name: str) -> str:
    """从 dimensions JSON 里取一个维度值的 SQL 表达式"""
    return f"COALESCE(json_extract(COALESCE(dimensions, '{{}}'), '$.\"{name}\"'), '')"


def _dimension_bucket_expr(names: Sequence[str]) -> str:
    """审计明细按多个维度拼成一个可读 bucket"""
    exprs = [_json_dimension(name) for name in names]
    return " || ' | ' || ".join(exprs)


def _dimension_group_expr(name: str) -> str:
    """维度日汇总表的分组表达式：day/user/endpoint 走固定列，其余走 dimensions"""
    if name == "day":
        return "day"
    if name == "month":
        return "substr(day, 1, 7)"
    if name == "user":
        return "COALESCE(NULLIF(api_user, ''), 'admin')"
    if name == "endpoint":
        return "endpoint"
    return _json_dimension(name)


def _bucket_from_exprs(exprs: Sequence[str]) -> str:
    """把多个分组表达式直接拼成 bucket（SQLite 不允许同一层 SELECT 引用别名）"""
    if len(exprs) == 1:
        return exprs[0]
    return " || ' | ' || ".join(exprs)
