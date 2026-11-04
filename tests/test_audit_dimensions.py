"""审计维度模型测试

覆盖：
- dimensions 是唯一写入源，标准字段由生成列派生
- 维度日汇总的累加与保留策略
- 按任意维度组合查询
- 非法维度名被拒绝
"""
import pytest

from openai_proxy.audit.query import UsageQueryService
from openai_proxy.audit.record import AuditAttempt
from openai_proxy.audit.store import SqliteAuditStore, USAGE_DIMENSION_TABLE


def _attempt(key: str, ts: str, *, endpoint="passthrough:example-service", user="user-a",
             model="model-a", size="1024x1024", outcome="success", retry_seq=1):
    attempt = AuditAttempt(
        attempt_key=key,
        gateway_request_id=key.rsplit("-", 1)[0],
        endpoint=endpoint,
        stream=False,
        api_user=user,
        attempt_seq=1,
        retry_seq=retry_seq,
        ts=ts,
        dimensions={
            "platform": "passthrough",
            "model": model,
            "model_id": model,
            "selector": "example-service",
            "size": size,
        },
        input_tokens=100,
        cached_tokens=10,
        output_tokens=20,
    )
    if outcome == "failure":
        attempt.mark_failure(http_status=429)
    elif outcome == "aborted":
        attempt.mark_aborted()
    else:
        attempt.mark_success()
    return attempt


@pytest.fixture
def store(tmp_path):
    instance = SqliteAuditStore(str(tmp_path / "audit.db"))
    instance.init_schema()
    yield instance
    instance.close()


class TestDimensionsStorage:
    """dimensions 与生成列"""

    @pytest.mark.unit
    def test_标准字段由生成列派生(self, store):
        store.insert_many([_attempt("r1-1", "2026-01-01T10:00:00.000+00:00").to_row()])

        row = store.query("SELECT platform, model_name, model_id, selector, dimensions FROM audit_attempts")[0]
        assert row["platform"] == "passthrough"
        assert row["model_name"] == "model-a"
        assert row["model_id"] == "model-a"
        assert row["selector"] == "example-service"
        assert '"size":"1024x1024"' in row["dimensions"]

    @pytest.mark.unit
    def test_维度日汇总按完整组合累加(self, store):
        store.insert_many([
            _attempt("r1-1", "2026-01-01T10:00:00.000+00:00", size="1024x1024").to_row(),
            _attempt("r2-1", "2026-01-01T10:01:00.000+00:00", size="1024x1024").to_row(),
            _attempt("r3-1", "2026-01-01T10:02:00.000+00:00", size="512x512").to_row(),
            _attempt("r4-1", "2026-01-01T10:03:00.000+00:00", size="1024x1024", outcome="failure").to_row(),
        ])
        store.aggregate_usage()

        rows = store.query(
            f"SELECT dimensions, requests, attempts, success, failure, "
            f"input_tokens, cached_tokens, output_tokens FROM {USAGE_DIMENSION_TABLE} ORDER BY dimensions"
        )
        assert len(rows) == 2
        by_size = {row["dimensions"]: row for row in rows}
        assert any('"size":"1024x1024"' in key for key in by_size)
        assert any('"size":"512x512"' in key for key in by_size)
        assert sum(row["requests"] for row in rows) == 4
        assert sum(row["failure"] for row in rows) == 1
        assert sum(row["input_tokens"] for row in rows) == 400
        assert sum(row["cached_tokens"] for row in rows) == 40
        assert sum(row["output_tokens"] for row in rows) == 80

    @pytest.mark.unit
    def test_清理明细不影响维度日汇总(self, store):
        store.insert_many([_attempt("r1-1", "2026-01-01T10:00:00.000+00:00").to_row()])
        store.aggregate_usage()
        store.prune("2026-02-01T00:00:00.000+00:00")

        assert store.stats()["rows"] == 0
        assert store.stats()["dimension_rows"] == 1


class TestDimensionQueries:
    """维度组合查询"""

    @pytest.fixture
    def queries(self, store):
        store.insert_many([
            _attempt("r1-1", "2026-01-01T10:00:00.000+00:00", user="user-a", model="model-a", size="1024x1024").to_row(),
            _attempt("r2-1", "2026-01-01T11:00:00.000+00:00", user="user-a", model="model-a", size="512x512").to_row(),
            _attempt("r3-1", "2026-01-01T12:00:00.000+00:00", user="user-b", model="model-b", size="1024x1024").to_row(),
            _attempt("r4-1", "2026-01-02T10:00:00.000+00:00", user="user-b", model="model-b", size="1024x1024").to_row(),
        ])
        store.aggregate_usage()
        return UsageQueryService(store)

    @pytest.mark.unit
    def test_按单个维度汇总(self, queries):
        result = queries.dimension_usage("model", since="2026-01-01")

        by_model = {item["values"]["model"]: item for item in result["items"]}
        assert by_model["model-a"]["requests"] == 2
        assert by_model["model-b"]["requests"] == 2

    @pytest.mark.unit
    def test_按多维度交叉汇总(self, queries):
        result = queries.dimension_usage("model,size", since="2026-01-01")

        assert result["count"] == 3
        combos = {(item["values"]["model"], item["values"]["size"]) for item in result["items"]}
        assert combos == {("model-a", "1024x1024"), ("model-a", "512x512"), ("model-b", "1024x1024")}

    @pytest.mark.unit
    def test_按月份汇总并返回tokens(self, queries):
        result = queries.dimension_usage("month,user", since="2026-01-01")

        by_user = {item["values"]["user"]: item for item in result["items"]}
        assert set(by_user) == {"user-a", "user-b"}
        assert all(item["values"]["month"] == "2026-01" for item in result["items"])
        assert by_user["user-a"]["input_tokens"] == 200
        assert by_user["user-a"]["output_tokens"] == 40
        assert by_user["user-b"]["input_tokens"] == 200

    @pytest.mark.unit
    def test_按用户与日期过滤(self, queries):
        result = queries.dimension_usage("day,user", since="2026-01-01", user="user-a")

        assert result["count"] == 1
        assert result["items"][0]["values"] == {"day": "2026-01-01", "user": "user-a"}

    @pytest.mark.unit
    def test_审计明细也可以按维度分组(self, queries):
        result = queries.summary("dim:model,size", since="2026-01-01")

        assert result["count"] == 3
        assert {item["bucket"] for item in result["items"]} == {
            "model-a | 1024x1024", "model-a | 512x512", "model-b | 1024x1024",
        }

    @pytest.mark.unit
    def test_非法维度名被拒绝(self, queries):
        with pytest.raises(ValueError):
            queries.dimension_usage("model,; DROP TABLE")
        with pytest.raises(ValueError):
            queries.summary("dim:model; DROP TABLE")

    @pytest.mark.unit
    def test_维度候选来自历史表(self, queries):
        assert {"model", "size", "platform", "selector"} <= set(queries.dimension_keys())
