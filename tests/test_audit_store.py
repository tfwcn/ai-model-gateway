"""审计存储单元测试

覆盖：
- 建表与索引（幂等）
- 批量写入与主键覆盖（stream_open 行被完整行覆盖）
- finalize 只允许更新白名单列
- 按保留期清理与手工清理
- 关闭审计时的空实现
"""

import sqlite3

import pytest

from openai_proxy.audit.record import AuditAttempt, OUTCOME_STREAM_OPEN, OUTCOME_SUCCESS
from openai_proxy.audit.store import NullAuditStore, SqliteAuditStore


def _row(key: str, ts: str = "2026-01-01T10:00:00.000+00:00", **overrides) -> dict:
    """构造一条落库行"""
    attempt = AuditAttempt(
        attempt_key=key,
        gateway_request_id=key.rsplit("-", 1)[0],
        endpoint="chat_completions",
        stream=True,
        platform="example-platform",
        model_name="example-platform|model-a",
        ts=ts,
        **overrides,
    )
    if overrides.get("outcome") != OUTCOME_STREAM_OPEN:
        attempt.mark_failure()
    return attempt.to_row()


@pytest.fixture
def store(tmp_path):
    """临时库文件上的存储实例"""
    instance = SqliteAuditStore(str(tmp_path / "audit.db"))
    instance.init_schema()
    yield instance
    instance.close()


class TestSchema:
    """建表与索引"""

    @pytest.mark.unit
    def test_建表是幂等的(self, tmp_path):
        first = SqliteAuditStore(str(tmp_path / "audit.db"))
        first.init_schema()
        first.insert_many([_row("req1-1")])
        first.close()

        reopened = SqliteAuditStore(str(tmp_path / "audit.db"))
        reopened.init_schema()

        assert reopened.stats()["rows"] == 1
        reopened.close()

    @pytest.mark.unit
    def test_常用过滤维度都建了索引(self, store):
        rows = store.query("PRAGMA index_list(audit_attempts)")

        indexed = {row["name"] for row in rows}
        assert "idx_audit_ts" in indexed
        assert "idx_audit_greq" in indexed
        assert "idx_audit_outcome" in indexed

    @pytest.mark.unit
    def test_库文件目录不存在时自动创建(self, tmp_path):
        store = SqliteAuditStore(str(tmp_path / "nested" / "deep" / "audit.db"))

        store.init_schema()
        assert store.stats()["rows"] == 0
        store.close()


class TestSchemaMigration:
    """老库升级：按 user_version 补列并给历史行一个说得通的取值"""

    @staticmethod
    def _legacy_db(tmp_path) -> str:
        """先按新结构写几行，再退回「还没有 done_sent 列」的老结构（连结构版本一起退回 0）"""
        path = str(tmp_path / "audit.db")
        store = SqliteAuditStore(path)
        store.init_schema()
        store.insert_many([
            _row("req1-1"),
            _row("req2-1", outcome=OUTCOME_STREAM_OPEN),
            dict(_row("req3-1"), outcome=OUTCOME_SUCCESS, finalized=1, done_sent=1),
        ])
        store._conn.execute("ALTER TABLE audit_attempts DROP COLUMN done_sent")
        store._conn.execute("PRAGMA user_version = 0")
        store._conn.commit()
        store.close()
        return path

    @pytest.mark.unit
    def test_老库缺done_sent列时会自动补列(self, tmp_path):
        upgraded = SqliteAuditStore(self._legacy_db(tmp_path))
        upgraded.init_schema()

        columns = {row["name"] for row in upgraded._conn.execute("PRAGMA table_info(audit_attempts)")}
        assert "done_sent" in columns
        upgraded.close()

    @pytest.mark.unit
    def test_补列时给历史成功行回填已发完但不改结局(self, tmp_path):
        upgraded = SqliteAuditStore(self._legacy_db(tmp_path))
        upgraded.init_schema()

        rows = upgraded.query("SELECT attempt_key, outcome, stream, done_sent FROM audit_attempts ORDER BY attempt_key")
        # 旧结局一条都不重算
        assert [(row["attempt_key"], row["outcome"]) for row in rows] == [
            ("req1-1", "failure"), ("req2-1", "stream_open"), ("req3-1", "success"),
        ]
        # 旧实现只有把流读到底才记 success，所以那批行其实都已经把终止标记发出去了
        assert {row["attempt_key"]: row["done_sent"] for row in rows} == {
            "req1-1": 0, "req2-1": 0, "req3-1": 1,
        }
        upgraded.close()

    @pytest.mark.unit
    def test_老库缺api_user列时会自动补列(self, tmp_path):
        path = str(tmp_path / "audit.db")
        legacy = SqliteAuditStore(path)
        legacy.init_schema()
        legacy.insert_many([_row("req1-1")])
        legacy._conn.execute("DROP INDEX idx_audit_user")
        legacy._conn.execute("ALTER TABLE audit_attempts DROP COLUMN api_user")
        legacy._conn.execute("PRAGMA user_version = 1")
        legacy._conn.commit()
        legacy.close()

        upgraded = SqliteAuditStore(path)
        upgraded.init_schema()

        columns = {row["name"] for row in upgraded._conn.execute("PRAGMA table_info(audit_attempts)")}
        rows = upgraded.query("SELECT api_user FROM audit_attempts")
        # 历史行无法还原调用方：迁移后统一归到 admin
        assert "api_user" in columns
        assert rows[0]["api_user"] == "admin"
        upgraded.close()

    @pytest.mark.unit
    def test_老库的anonymous日汇总并入admin(self, tmp_path):
        path = str(tmp_path / "audit.db")
        legacy = SqliteAuditStore(path)
        legacy.init_schema()
        legacy._conn.executemany(
            "INSERT INTO usage_daily (day, api_user, requests, attempts, success, failure, aborted, retries, "
            "input_tokens, cached_tokens, output_tokens, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("2026-01-01", "anonymous", 2, 2, 2, 0, 0, 0, 100, 10, 200, "2026-01-01T10:00:00.000+00:00"),
                ("2026-01-01", "admin", 1, 1, 1, 0, 0, 0, 50, 5, 80, "2026-01-01T11:00:00.000+00:00"),
            ],
        )
        legacy._conn.execute("PRAGMA user_version = 3")
        legacy._conn.commit()
        legacy.close()

        upgraded = SqliteAuditStore(path)
        upgraded.init_schema()

        rows = upgraded.query("SELECT * FROM usage_daily")
        assert len(rows) == 1
        assert rows[0]["api_user"] == "admin"
        assert rows[0]["requests"] == 3
        assert rows[0]["input_tokens"] == 150
        assert rows[0]["output_tokens"] == 280
        upgraded.close()

    @pytest.mark.unit
    def test_迁移只会执行一次(self, tmp_path):
        upgraded = SqliteAuditStore(self._legacy_db(tmp_path))
        upgraded.init_schema()
        version = upgraded._conn.execute("PRAGMA user_version").fetchone()[0]

        # 再把列删掉：版本已经到位就不会重跑迁移，列也不会被凭空加回来
        upgraded._conn.execute("ALTER TABLE audit_attempts DROP COLUMN done_sent")
        upgraded.init_schema()

        columns = {row["name"] for row in upgraded._conn.execute("PRAGMA table_info(audit_attempts)")}
        assert version == 5
        assert "done_sent" not in columns
        upgraded.close()

    @pytest.mark.unit
    def test_新库不需要迁移(self, tmp_path):
        store = SqliteAuditStore(str(tmp_path / "audit.db"))
        store.init_schema()
        store.init_schema()

        rows = store.query("SELECT attempt_key FROM audit_attempts")
        assert rows == []
        store.close()


class TestWrites:
    """写入与覆盖"""

    @pytest.mark.unit
    def test_批量写入后行可查询(self, store):
        store.insert_many([_row("req1-1"), _row("req1-2"), _row("req2-1")])

        rows = store.query("SELECT attempt_key FROM audit_attempts ORDER BY attempt_key")
        assert [row["attempt_key"] for row in rows] == ["req1-1", "req1-2", "req2-1"]

    @pytest.mark.unit
    def test_同一主键重复提交会覆盖(self, store):
        store.insert_many([_row("req1-1", outcome=OUTCOME_STREAM_OPEN)])
        # 流结束时用完整行覆盖
        complete = _row("req1-1")
        complete["outcome"] = "success"
        complete["finalized"] = 1
        complete["output_tokens"] = 350
        store.insert_many([complete])

        rows = store.query("SELECT * FROM audit_attempts")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "success"
        assert rows[0]["output_tokens"] == 350

    @pytest.mark.unit
    def test_空批量写入不会报错(self, store):
        assert store.insert_many([]) == 0

    @pytest.mark.unit
    def test_finalize只更新白名单列(self, store):
        store.insert_many([_row("req1-1", outcome=OUTCOME_STREAM_OPEN)])

        changed = store.finalize_row("req1-1", {"outcome": "aborted", "finalized": True, "output_tokens": 12,
                                                "done_sent": True})

        rows = store.query("SELECT * FROM audit_attempts")
        assert changed == 1
        assert rows[0]["outcome"] == "aborted"
        assert rows[0]["finalized"] == 1
        assert rows[0]["output_tokens"] == 12
        assert rows[0]["done_sent"] == 1

    @pytest.mark.unit
    def test_finalize不允许改写主键与时间(self, store):
        store.insert_many([_row("req1-1")])

        store.finalize_row("req1-1", {"attempt_key": "hacked", "ts": "1999-01-01T00:00:00.000+00:00"})

        rows = store.query("SELECT attempt_key, ts FROM audit_attempts")
        assert rows[0]["attempt_key"] == "req1-1"
        assert rows[0]["ts"].startswith("2026-01-01")

    @pytest.mark.unit
    def test_finalize空字段集不执行更新(self, store):
        store.insert_many([_row("req1-1")])

        assert store.finalize_row("req1-1", {}) == 0


class TestPrune:
    """清理"""

    @pytest.mark.unit
    def test_按时间清理只删除更早的记录(self, store):
        store.insert_many([
            _row("old-1", ts="2026-01-01T00:00:00.000+00:00"),
            _row("new-1", ts="2026-02-01T00:00:00.000+00:00"),
        ])

        removed = store.prune("2026-01-15T00:00:00.000+00:00")

        assert removed == 1
        assert store.stats()["rows"] == 1

    @pytest.mark.unit
    def test_保留天数为零时不清理(self, store):
        store.insert_many([_row("req1-1")])

        assert store.prune_old(0) == 0
        assert store.stats()["rows"] == 1


class TestStats:
    """库状态"""

    @pytest.mark.unit
    def test_stats暴露行数与体积(self, store):
        store.insert_many([_row("req1-1", ts="2026-01-01T00:00:00.000+00:00")])

        stats = store.stats()

        assert stats["rows"] == 1
        assert stats["oldest"] == stats["newest"]
        assert stats["db_bytes"] > 0


class TestUsageAggregate:
    """全量日汇总：usage_daily 只累加、不随明细清理"""

    @staticmethod
    def _success(key: str, ts: str, api_user: str, input_tokens: int = 100, cached: int = 30, output: int = 200) -> dict:
        attempt = AuditAttempt(
            attempt_key=key,
            gateway_request_id=key.rsplit("-", 1)[0],
            endpoint="chat_completions",
            stream=False,
            platform="example-platform",
            model_name="example-platform|model-a",
            ts=ts,
            api_user=api_user,
        )
        attempt.mark_success({
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output,
                "total_tokens": input_tokens + output,
                "prompt_tokens_details": {"cached_tokens": cached},
            }
        })
        return attempt.to_row()

    @pytest.mark.unit
    def test_已收尾明细按天与用户累加(self, store):
        store.insert_many([
            self._success("r1-1", "2026-01-01T10:00:00.000+00:00", "user-a"),
            dict(_row("r2-1", ts="2026-01-01T10:01:00.000+00:00", api_user="user-a")),
            dict(_row("r3-1", ts="2026-01-02T10:00:00.000+00:00", api_user="user-b")),
        ])

        assert store.aggregate_usage() == 3

        rows = store.query("SELECT * FROM usage_daily ORDER BY day, api_user")
        first = rows[0]
        assert (first["day"], first["api_user"]) == ("2026-01-01", "user-a")
        assert first["requests"] == 2 and first["attempts"] == 2
        assert first["success"] == 1 and first["failure"] == 1
        assert first["input_tokens"] == 100 and first["cached_tokens"] == 30
        assert first["output_tokens"] == 200
        assert (rows[1]["day"], rows[1]["api_user"]) == ("2026-01-02", "user-b")

    @pytest.mark.unit
    def test_重复汇总不会重复计数(self, store):
        store.insert_many([self._success("r1-1", "2026-01-01T10:00:00.000+00:00", "user-a")])

        assert store.aggregate_usage() == 1
        assert store.aggregate_usage() == 0

        row = store.query("SELECT * FROM usage_daily")[0]
        assert row["attempts"] == 1 and row["output_tokens"] == 200

    @pytest.mark.unit
    def test_未收尾的流不计入日汇总(self, store):
        attempt = AuditAttempt(
            attempt_key="r1-1",
            gateway_request_id="r1",
            endpoint="chat_completions",
            stream=True,
            platform="example-platform",
            model_name="example-platform|model-a",
            ts="2026-01-01T10:00:00.000+00:00",
            api_user="user-a",
        )
        attempt.mark_stream_open()
        store.insert_many([attempt.to_row()])

        assert store.aggregate_usage() == 0
        assert store.query("SELECT * FROM usage_daily") == []

    @pytest.mark.unit
    def test_清理明细不会删除日汇总(self, store):
        store.insert_many([self._success("r1-1", "2026-01-01T10:00:00.000+00:00", "user-a")])
        store.aggregate_usage()

        store.prune("2026-02-01T00:00:00.000+00:00")

        assert store.query("SELECT * FROM audit_attempts") == []
        assert store.query("SELECT * FROM usage_daily")[0]["output_tokens"] == 200

    @pytest.mark.unit
    def test_配置变更不进入用量(self, store):
        config_row = dict(
            _row("config-1", ts="2026-01-01T10:00:00.000+00:00"),
            endpoint="config_write",
            outcome=OUTCOME_SUCCESS,
            finalized=1,
        )
        store.insert_many([config_row])

        assert store.aggregate_usage() == 1
        assert store.query("SELECT * FROM usage_daily") == []


class TestNullStore:
    """关闭审计时的空实现"""

    @pytest.mark.unit
    def test_空实现全部返回零(self):
        store = NullAuditStore()

        store.init_schema()
        assert store.insert_many([_row("req1-1")]) == 0
        assert store.finalize_row("req1-1", {"outcome": "success"}) == 0
        assert store.prune("2026-01-01") == 0
        assert store.prune_old(30) == 0
        assert store.query("SELECT 1") == []
        assert store.stats()["rows"] == 0
        store.close()
