"""
审计存储 - SQLite 落库与查询通道

设计要点：
- 单写者模型：配合 AuditCollector 的后台刷盘任务，请求热路径不碰磁盘
- WAL + busy_timeout：多进程读取安全，写入串行
- 所有方法都是同步的（sqlite 本身很快），由上层用 asyncio.to_thread 包一层避免卡事件循环
"""
import logging
import os
import sqlite3
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from openai_proxy.audit.record import COLUMNS

logger = logging.getLogger(__name__)

TABLE = "audit_attempts"
# 全量日汇总：审计明细按保留期清理，这张表永久保留，用于「用量不丢历史」
USAGE_TABLE = "usage_daily"
# 维度日汇总：按「完整维度组合」累加，同样永久保留
USAGE_DIMENSION_TABLE = "usage_dimension_daily"

# 结构版本：版本 5 起标准维度改为 dimensions + 生成列；版本 6 给维度日汇总加 token；
# 版本 7 修正历史行里按「提取响应体耗时」算出的假 tokens/s
DIMENSIONS_VERSION = 5
TOKEN_COLUMNS_VERSION = 6
SCHEMA_VERSION = 7

# 主键 + 常用过滤维度的索引：按时间、按模型、按结局、按请求链
_INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS idx_audit_ts ON {TABLE}(ts)",
    f"CREATE INDEX IF NOT EXISTS idx_audit_model ON {TABLE}(platform, model_name, ts)",
    f"CREATE INDEX IF NOT EXISTS idx_audit_outcome ON {TABLE}(outcome, ts)",
    f"CREATE INDEX IF NOT EXISTS idx_audit_greq ON {TABLE}(gateway_request_id)",
    f"CREATE INDEX IF NOT EXISTS idx_audit_endpoint ON {TABLE}(endpoint, ts)",
    f"CREATE INDEX IF NOT EXISTS idx_audit_user ON {TABLE}(api_user, ts)",
    # 只有「已收尾但还没汇总」的行会被扫描，索引很小
    f"CREATE INDEX IF NOT EXISTS idx_audit_pending_agg ON {TABLE}(finalized, aggregated) "
    f"WHERE finalized = 1 AND aggregated = 0",
)

_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    attempt_key         TEXT PRIMARY KEY,
    gateway_request_id  TEXT NOT NULL,
    api_user            TEXT,
    request_id          TEXT,
    endpoint            TEXT NOT NULL,
    stream              INTEGER NOT NULL DEFAULT 0,
    attempt_seq         INTEGER,
    retry_seq           INTEGER,
    ts                  TEXT NOT NULL,
    outcome             TEXT NOT NULL,
    finalized           INTEGER NOT NULL DEFAULT 0,
    http_status         INTEGER,
    error_category      TEXT,
    error_code          TEXT,
    disabled_model      INTEGER NOT NULL DEFAULT 0,
    duration_ms         INTEGER,
    probe_ms            INTEGER,
    ttft_ms             INTEGER,
    has_usage           INTEGER NOT NULL DEFAULT 0,
    upstream_id         TEXT,
    input_tokens        INTEGER,
    cached_tokens       INTEGER,
    output_tokens       INTEGER,
    output_chars        INTEGER,
    throughput          REAL,
    tool_call_converted INTEGER,
    keepalive_count     INTEGER,
    msg_count           INTEGER,
    done_sent           INTEGER NOT NULL DEFAULT 0,
    dimensions          TEXT NOT NULL DEFAULT '{{}}',
    aggregated          INTEGER NOT NULL DEFAULT 0,
    -- 以下四列是 dimensions 的标准键投影，只读；写入只写 dimensions 一份
    platform            TEXT GENERATED ALWAYS AS (json_extract(dimensions, '$.platform')) VIRTUAL,
    model_name          TEXT GENERATED ALWAYS AS (json_extract(dimensions, '$.model')) VIRTUAL,
    model_id            TEXT GENERATED ALWAYS AS (json_extract(dimensions, '$.model_id')) VIRTUAL,
    selector            TEXT GENERATED ALWAYS AS (json_extract(dimensions, '$.selector')) VIRTUAL
)
"""

# 全量日汇总：按 (日期, 用户) 累加，不随审计明细的保留期清理
_USAGE_DDL = f"""
CREATE TABLE IF NOT EXISTS {USAGE_TABLE} (
    day             TEXT NOT NULL,
    api_user        TEXT NOT NULL,
    requests        INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    success         INTEGER NOT NULL DEFAULT 0,
    failure         INTEGER NOT NULL DEFAULT 0,
    aborted         INTEGER NOT NULL DEFAULT 0,
    retries         INTEGER NOT NULL DEFAULT 0,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    cached_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (day, api_user)
)
"""

# 维度日汇总：一天 + 一个入口 + 一个用户 + 一组完整维度 = 一行
_USAGE_DIMENSION_DDL = f"""
CREATE TABLE IF NOT EXISTS {USAGE_DIMENSION_TABLE} (
    day             TEXT NOT NULL,
    endpoint        TEXT NOT NULL,
    api_user        TEXT NOT NULL,
    dimensions      TEXT NOT NULL,
    requests        INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    success         INTEGER NOT NULL DEFAULT 0,
    failure         INTEGER NOT NULL DEFAULT 0,
    aborted         INTEGER NOT NULL DEFAULT 0,
    retries         INTEGER NOT NULL DEFAULT 0,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    cached_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (day, endpoint, api_user, dimensions)
)
"""

# 首发之后新增的列与历史行修正：按版本号组织，老库用 ALTER TABLE 追加，不改已有列、不重建表
#
# 版本 1（done_sent）：旧实现只有把上游流读到底才会记 success，所以历史上记成 success 的流式行
# 其实都已经把终止标记转发出去了，这里补一个说得通的取值；
# 已经记成 aborted 的行保持原样 —— 旧结局一律不重算。
_MIGRATIONS: Dict[int, Dict[str, Any]] = {
    1: {
        "columns": {"done_sent": "INTEGER NOT NULL DEFAULT 0"},
        "statements": (
            f"UPDATE {TABLE} SET done_sent = 1 WHERE outcome = 'success' AND stream = 1",
        ),
    },
    # 版本 2（api_user）：用户级密钥引入的调用方标签。历史行无法还原调用方，保持 NULL，
    # 查询侧按 admin 展示，避免把未知来源硬塞给某个真实用户。
    2: {
        "columns": {"api_user": "TEXT"},
        "statements": (),
    },
    # 版本 3（aggregated）：标记审计行是否已经进入 usage_daily 全量日汇总。
    # 历史已收尾行默认 0，升级后的第一次汇总会把它们补进日汇总表。
    3: {
        "columns": {"aggregated": "INTEGER NOT NULL DEFAULT 0"},
        "statements": (),
    },
    # 版本 4：不再区分 anonymous，历史与未鉴权流量统一归到 admin。
    # usage_daily 的 anonymous 行先并入当天的 admin 行，再删掉；审计明细同步回填。
    4: {
        "columns": {},
        "statements": (
            f"""
            INSERT INTO {USAGE_TABLE} (
                day, api_user, requests, attempts, success, failure, aborted, retries,
                input_tokens, cached_tokens, output_tokens, updated_at
            )
            SELECT
                day, 'admin', SUM(requests), SUM(attempts), SUM(success), SUM(failure),
                SUM(aborted), SUM(retries), SUM(input_tokens), SUM(cached_tokens),
                SUM(output_tokens), MAX(updated_at)
            FROM {USAGE_TABLE}
            WHERE api_user = 'anonymous'
            GROUP BY day
            ON CONFLICT(day, api_user) DO UPDATE SET
                requests = requests + excluded.requests,
                attempts = attempts + excluded.attempts,
                success = success + excluded.success,
                failure = failure + excluded.failure,
                aborted = aborted + excluded.aborted,
                retries = retries + excluded.retries,
                input_tokens = input_tokens + excluded.input_tokens,
                cached_tokens = cached_tokens + excluded.cached_tokens,
                output_tokens = output_tokens + excluded.output_tokens,
                updated_at = excluded.updated_at
            """,
            f"DELETE FROM {USAGE_TABLE} WHERE api_user = 'anonymous'",
            f"UPDATE {TABLE} SET api_user = 'admin' WHERE api_user IS NULL OR api_user = ''",
        ),
    },
}


class AuditStore(ABC):
    """审计存储抽象：便于关闭审计时替换为空实现"""

    @abstractmethod
    def init_schema(self) -> None:
        ...

    @abstractmethod
    def insert_many(self, rows: Iterable[Dict[str, Any]]) -> int:
        ...

    @abstractmethod
    def finalize_row(self, attempt_key: str, fields: Dict[str, Any]) -> int:
        ...

    @abstractmethod
    def prune(self, before_iso: str) -> int:
        ...

    @abstractmethod
    def aggregate_usage(self) -> int:
        ...

    @abstractmethod
    def prune_old(self, retention_days: int) -> int:
        ...

    @abstractmethod
    def stats(self) -> Dict[str, Any]:
        ...

    @abstractmethod
    def query(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def close(self) -> None:
        ...


class SqliteAuditStore(AuditStore):
    """SQLite 审计存储"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)

        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")

    def init_schema(self) -> None:
        """建表建索引，并按版本迁移老库结构（幂等）"""
        with self._lock, self._conn:
            self._conn.execute(_CREATE_DDL)
            self._conn.execute(_USAGE_DDL)
            self._conn.execute(_USAGE_DIMENSION_DDL)
            self._migrate_schema()
            for ddl in _INDEX_DDL:
                self._conn.execute(ddl)

    def _migrate_schema(self) -> None:
        """
        按 `PRAGMA user_version` 依次执行结构迁移

        版本 1~4 只补列与回填；版本 5 把标准维度收拢到 dimensions 一份真相：
        老库会重建 audit_attempts，把 platform/model_name/model_id/selector 写进 dimensions，
        再用虚拟生成列保持原有查询与界面字段不变。
        """
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0] or 0)
        existing = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({TABLE})")}

        for target in sorted(_MIGRATIONS):
            if version >= target:
                continue
            spec = _MIGRATIONS[target]
            added = []
            for name, column_type in spec["columns"].items():
                if name in existing:
                    continue
                self._conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN {name} {column_type}")
                existing.add(name)
                added.append(name)
            touched = 0
            for statement in spec["statements"]:
                touched = max(touched, self._conn.execute(statement).rowcount)
            self._conn.execute(f"PRAGMA user_version = {target}")
            logger.info(
                f"审计库结构升级: user_version {version} → {target}"
                f"（新增列 {', '.join(added) or '无'}，回填 {touched} 行历史取值）"
            )
            version = target

        if version < DIMENSIONS_VERSION:
            self._migrate_to_dimensions()
            version = DIMENSIONS_VERSION

        if version < TOKEN_COLUMNS_VERSION:
            self._migrate_dimension_tokens()
            version = TOKEN_COLUMNS_VERSION

        if version < SCHEMA_VERSION:
            self._migrate_throughput()

    def _migrate_to_dimensions(self) -> None:
        """把老结构收拢为 dimensions + 生成列，并回填维度日汇总"""
        columns = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({TABLE})")}
        generated = {
            row["name"] for row in self._conn.execute(f"PRAGMA table_xinfo({TABLE})")
            if int(row["hidden"] or 0) > 0
        }
        if "dimensions" not in columns or "platform" not in generated:
            legacy = f"{TABLE}_legacy"
            self._conn.execute(f"ALTER TABLE {TABLE} RENAME TO {legacy}")
            self._conn.execute(_CREATE_DDL)
            core_columns = [column for column in COLUMNS if column != "dimensions"]
            column_list = ", ".join(core_columns + ["dimensions"])
            select_list = ", ".join(
                f"COALESCE({column}, '')" if column in ("api_user", "request_id") else column
                for column in core_columns
            )
            self._conn.execute(
                f"""
                INSERT INTO {TABLE} ({column_list})
                SELECT {select_list},
                       json_object(
                           'model', COALESCE(model_name, ''),
                           'model_id', COALESCE(model_id, ''),
                           'platform', COALESCE(platform, ''),
                           'selector', COALESCE(selector, '')
                       )
                FROM {legacy}
                """
            )
            self._conn.execute(f"DROP TABLE {legacy}")
            logger.info("审计库结构升级: 标准维度已收拢到 dimensions（老行已回填）")
        self._conn.execute(f"PRAGMA user_version = {DIMENSIONS_VERSION}")
        self._backfill_dimension_usage()

    def _migrate_dimension_tokens(self) -> None:
        """版本 6：给维度日汇总补 token 列，并用仍保留的审计明细回填一次"""
        columns = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({USAGE_DIMENSION_TABLE})")}
        for name in ("input_tokens", "cached_tokens", "output_tokens"):
            if name not in columns:
                self._conn.execute(
                    f"ALTER TABLE {USAGE_DIMENSION_TABLE} ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
                )
        self._conn.execute(
            f"""
            UPDATE {USAGE_DIMENSION_TABLE} AS u
            SET input_tokens = COALESCE((
                    SELECT SUM(a.input_tokens) FROM {TABLE} a
                    WHERE substr(a.ts, 1, 10) = u.day
                      AND a.endpoint = u.endpoint
                      AND COALESCE(NULLIF(a.api_user, ''), 'admin') = u.api_user
                      AND a.dimensions = u.dimensions
                      AND a.finalized = 1 AND a.endpoint != 'config_write'
                ), 0),
                cached_tokens = COALESCE((
                    SELECT SUM(a.cached_tokens) FROM {TABLE} a
                    WHERE substr(a.ts, 1, 10) = u.day
                      AND a.endpoint = u.endpoint
                      AND COALESCE(NULLIF(a.api_user, ''), 'admin') = u.api_user
                      AND a.dimensions = u.dimensions
                      AND a.finalized = 1 AND a.endpoint != 'config_write'
                ), 0),
                output_tokens = COALESCE((
                    SELECT SUM(a.output_tokens) FROM {TABLE} a
                    WHERE substr(a.ts, 1, 10) = u.day
                      AND a.endpoint = u.endpoint
                      AND COALESCE(NULLIF(a.api_user, ''), 'admin') = u.api_user
                      AND a.dimensions = u.dimensions
                      AND a.finalized = 1 AND a.endpoint != 'config_write'
                ), 0)
            """
        )
        self._conn.execute(f"PRAGMA user_version = {TOKEN_COLUMNS_VERSION}")
        logger.info("审计库结构升级: 维度日汇总已补充 token 列")

    def _migrate_throughput(self) -> None:
        """版本 7：按真实 duration_ms 重算历史 tokens/s，耗时无效的行置空"""
        updated = self._conn.execute(
            f"""
            UPDATE {TABLE}
            SET throughput = CASE
                WHEN duration_ms > 0 AND output_tokens > 0
                THEN output_tokens * 1000.0 / duration_ms
                ELSE NULL
            END
            WHERE throughput IS NOT NULL
              AND (
                  duration_ms IS NULL OR duration_ms <= 0
                  OR output_tokens IS NULL OR output_tokens <= 0
                  OR ABS(throughput - output_tokens * 1000.0 / duration_ms) > 0.01
              )
            """
        ).rowcount
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        logger.info(f"审计库结构升级: 已按真实耗时重算 tokens/s（{updated} 行）")

    def _backfill_dimension_usage(self) -> None:
        """维度日汇总表首次创建时，用全部已收尾明细回填一次（不碰 usage_daily）"""
        self._conn.execute(
            f"""
            INSERT INTO {USAGE_DIMENSION_TABLE} (
                day, endpoint, api_user, dimensions, requests, attempts,
                success, failure, aborted, retries,
                input_tokens, cached_tokens, output_tokens, updated_at
            )
            SELECT
                substr(ts, 1, 10) AS day,
                endpoint,
                COALESCE(NULLIF(api_user, ''), 'admin') AS api_user,
                dimensions,
                COUNT(DISTINCT gateway_request_id) AS requests,
                COUNT(*) AS attempts,
                SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END) AS success,
                SUM(CASE WHEN outcome = 'failure' THEN 1 ELSE 0 END) AS failure,
                SUM(CASE WHEN outcome = 'aborted' THEN 1 ELSE 0 END) AS aborted,
                SUM(CASE WHEN retry_seq > 1 THEN 1 ELSE 0 END) AS retries,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                ? AS updated_at
            FROM {TABLE}
            WHERE finalized = 1 AND endpoint != 'config_write'
              AND dimensions IS NOT NULL AND dimensions != '' AND dimensions != '{{}}'
            GROUP BY day, endpoint, api_user, dimensions
            ON CONFLICT(day, endpoint, api_user, dimensions) DO UPDATE SET
                requests = requests + excluded.requests,
                attempts = attempts + excluded.attempts,
                success = success + excluded.success,
                failure = failure + excluded.failure,
                aborted = aborted + excluded.aborted,
                retries = retries + excluded.retries,
                input_tokens = input_tokens + excluded.input_tokens,
                cached_tokens = cached_tokens + excluded.cached_tokens,
                output_tokens = output_tokens + excluded.output_tokens,
                updated_at = excluded.updated_at
            """,
            (_now_iso(),),
        )
        logger.info("审计维度日汇总已回填")

    def insert_many(self, rows: Iterable[Dict[str, Any]]) -> int:
        """批量写入：INSERT OR REPLACE，允许 finalize 前后的重复提交覆盖同一行"""
        materialized = list(rows)
        if not materialized:
            return 0
        placeholders = ", ".join("?" for _ in COLUMNS)
        column_list = ", ".join(COLUMNS)
        sql = f"INSERT OR REPLACE INTO {TABLE} ({column_list}) VALUES ({placeholders})"
        values = [tuple(row.get(column) for column in COLUMNS) for row in materialized]
        with self._lock, self._conn:
            self._conn.executemany(sql, values)
        return len(materialized)

    def finalize_row(self, attempt_key: str, fields: Dict[str, Any]) -> int:
        """补齐用量与最终结局（流结束时调用）"""
        allowed = {
            "outcome", "finalized", "duration_ms", "probe_ms", "ttft_ms", "has_usage",
            "upstream_id", "input_tokens", "cached_tokens", "output_tokens", "output_chars",
            "throughput", "keepalive_count", "tool_call_converted", "done_sent",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return 0
        assignments = ", ".join(f"{key} = ?" for key in updates)
        params: List[Any] = [_as_sql_value(value) for value in updates.values()]
        params.append(attempt_key)
        with self._lock, self._conn:
            cursor = self._conn.execute(f"UPDATE {TABLE} SET {assignments} WHERE attempt_key = ?", params)
            return cursor.rowcount

    def aggregate_usage(self) -> int:
        """
        把「已收尾但还没汇总」的审计行累加进全量日汇总表

        与写审计行分开执行：先落明细，再一次性汇总；两步都在同一个事务里，
        中途崩溃不会留下「算了一半」的日汇总。配置变更审计（config_write）不进入用量。
        返回本次标记为已汇总的明细行数。
        """
        with self._lock, self._conn:
            self._conn.execute(
                f"""
                INSERT INTO {USAGE_TABLE} (
                    day, api_user, requests, attempts, success, failure, aborted, retries,
                    input_tokens, cached_tokens, output_tokens, updated_at
                )
                SELECT
                    substr(ts, 1, 10) AS day,
                    COALESCE(NULLIF(api_user, ''), 'admin') AS api_user,
                    COUNT(DISTINCT gateway_request_id) AS requests,
                    COUNT(*) AS attempts,
                    SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END) AS success,
                    SUM(CASE WHEN outcome = 'failure' THEN 1 ELSE 0 END) AS failure,
                    SUM(CASE WHEN outcome = 'aborted' THEN 1 ELSE 0 END) AS aborted,
                    SUM(CASE WHEN retry_seq > 1 THEN 1 ELSE 0 END) AS retries,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    ? AS updated_at
                FROM {TABLE}
                WHERE finalized = 1 AND aggregated = 0 AND endpoint != 'config_write'
                GROUP BY day, api_user
                ON CONFLICT(day, api_user) DO UPDATE SET
                    requests = requests + excluded.requests,
                    attempts = attempts + excluded.attempts,
                    success = success + excluded.success,
                    failure = failure + excluded.failure,
                    aborted = aborted + excluded.aborted,
                    retries = retries + excluded.retries,
                    input_tokens = input_tokens + excluded.input_tokens,
                    cached_tokens = cached_tokens + excluded.cached_tokens,
                    output_tokens = output_tokens + excluded.output_tokens,
                    updated_at = excluded.updated_at
                """,
                (_now_iso(),),
            )
            # 维度日汇总与 usage_daily 同一事务：按完整维度组合累加
            self._conn.execute(
                f"""
                INSERT INTO {USAGE_DIMENSION_TABLE} (
                    day, endpoint, api_user, dimensions, requests, attempts,
                    success, failure, aborted, retries,
                    input_tokens, cached_tokens, output_tokens, updated_at
                )
                SELECT
                    substr(ts, 1, 10) AS day,
                    endpoint,
                    COALESCE(NULLIF(api_user, ''), 'admin') AS api_user,
                    dimensions,
                    COUNT(DISTINCT gateway_request_id) AS requests,
                    COUNT(*) AS attempts,
                    SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END) AS success,
                    SUM(CASE WHEN outcome = 'failure' THEN 1 ELSE 0 END) AS failure,
                    SUM(CASE WHEN outcome = 'aborted' THEN 1 ELSE 0 END) AS aborted,
                    SUM(CASE WHEN retry_seq > 1 THEN 1 ELSE 0 END) AS retries,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    ? AS updated_at
                FROM {TABLE}
                WHERE finalized = 1 AND aggregated = 0 AND endpoint != 'config_write'
                  AND dimensions IS NOT NULL AND dimensions != '' AND dimensions != '{{}}'
                GROUP BY day, endpoint, api_user, dimensions
                ON CONFLICT(day, endpoint, api_user, dimensions) DO UPDATE SET
                    requests = requests + excluded.requests,
                    attempts = attempts + excluded.attempts,
                    success = success + excluded.success,
                    failure = failure + excluded.failure,
                    aborted = aborted + excluded.aborted,
                    retries = retries + excluded.retries,
                    input_tokens = input_tokens + excluded.input_tokens,
                    cached_tokens = cached_tokens + excluded.cached_tokens,
                    output_tokens = output_tokens + excluded.output_tokens,
                    updated_at = excluded.updated_at
                """,
                (_now_iso(),),
            )
            cursor = self._conn.execute(
                f"UPDATE {TABLE} SET aggregated = 1 WHERE finalized = 1 AND aggregated = 0"
            )
            return cursor.rowcount

    def prune(self, before_iso: str) -> int:
        """删除指定时间之前的记录"""
        with self._lock, self._conn:
            cursor = self._conn.execute(f"DELETE FROM {TABLE} WHERE ts < ?", (before_iso,))
            return cursor.rowcount

    def prune_old(self, retention_days: int) -> int:
        """按保留天数清理（启动时调用）"""
        if retention_days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        return self.prune(cutoff.isoformat(timespec="milliseconds"))

    def stats(self) -> Dict[str, Any]:
        """库体积与记录范围，供 /admin/audit/stats 使用"""
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS total, MIN(ts) AS oldest, MAX(ts) AS newest FROM {TABLE}"
            ).fetchone()
            dimension_row = self._conn.execute(
                f"SELECT COUNT(*) AS total FROM {USAGE_DIMENSION_TABLE}"
            ).fetchone()
        size_bytes = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        return {
            "rows": int(row["total"] or 0),
            "dimension_rows": int(dimension_row["total"] or 0) if dimension_row else 0,
            "oldest": row["oldest"],
            "newest": row["newest"],
            "db_path": self.db_path,
            "db_bytes": size_bytes,
        }

    def query(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        """只读查询通道（query 层拼 SQL，参数一律占位符绑定）"""
        with self._lock:
            cursor = self._conn.execute(sql, tuple(params))
            return [dict(item) for item in cursor.fetchall()]

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error as exc:  # noqa: BLE001 - 关闭失败只告警
                logger.debug(f"关闭审计数据库失败: {exc}")


class NullAuditStore(AuditStore):
    """审计关闭时的空实现：保证上层代码不需要分支判断"""

    def init_schema(self) -> None:
        return None

    def insert_many(self, rows: Iterable[Dict[str, Any]]) -> int:
        return 0

    def finalize_row(self, attempt_key: str, fields: Dict[str, Any]) -> int:
        return 0

    def prune(self, before_iso: str) -> int:
        return 0

    def aggregate_usage(self) -> int:
        return 0

    def prune_old(self, retention_days: int) -> int:
        return 0

    def stats(self) -> Dict[str, Any]:
        return {
            "rows": 0,
            "dimension_rows": 0,
            "oldest": None,
            "newest": None,
            "db_path": None,
            "db_bytes": 0,
        }

    def query(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        return []

    def close(self) -> None:
        return None


def _as_sql_value(value: Any) -> Any:
    """bool → 0/1，其余原样交给 sqlite"""
    if isinstance(value, bool):
        return 1 if value else 0
    return value


def _now_iso() -> str:
    """当前 UTC 时间（毫秒精度），写入日汇总的 updated_at"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
