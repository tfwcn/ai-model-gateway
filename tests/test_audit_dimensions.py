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
    def test_可以按维度值排序(self, queries):
        result = queries.dimension_usage(
            "model,size", since="2026-01-01", sort="size", order="asc"
        )

        assert result["sort"] == "size"
        assert result["order"] == "asc"
        assert [item["values"]["size"] for item in result["items"]] == [
            "1024x1024", "1024x1024", "512x512",
        ]

    @pytest.mark.unit
    def test_维度排序默认升序(self, queries):
        result = queries.dimension_usage("model", since="2026-01-01", sort="model")

        assert result["order"] == "asc"
        assert [item["values"]["model"] for item in result["items"]] == ["model-a", "model-b"]

    @pytest.mark.unit
    def test_可以按指标升序排序(self, queries):
        result = queries.dimension_usage(
            "model,size", since="2026-01-01", sort="requests", order="asc"
        )

        assert [item["requests"] for item in result["items"]] == [1, 1, 2]

    @pytest.mark.unit
    def test_可以按维度值过滤(self, queries):
        result = queries.dimension_usage(
            "model,size", since="2026-01-01",
            filters='{"model": ["model-a"], "size": ["1024x1024"]}',
        )

        assert result["count"] == 1
        assert result["items"][0]["values"] == {
            "model": "model-a", "size": "1024x1024",
        }

    @pytest.mark.unit
    def test_维度过滤支持固定分组键(self, queries):
        result = queries.dimension_usage(
            "day,user", since="2026-01-01", filters='{"user": "user-b"}',
        )

        assert result["count"] == 2
        assert {item["values"]["user"] for item in result["items"]} == {"user-b"}

    @pytest.mark.unit
    def test_非法维度过滤被拒绝(self, queries):
        with pytest.raises(ValueError):
            queries.dimension_usage("model", filters="not-json")
        with pytest.raises(ValueError):
            queries.dimension_usage("model", filters='{"model;DROP": "x"}')

    @pytest.mark.unit
    def test_可以按成功率排序(self, queries):
        result = queries.dimension_usage(
            "model", since="2026-01-01", sort="success_rate", order="desc"
        )

        assert result["sort"] == "success_rate"
        assert all(item["success_rate"] == 1.0 for item in result["items"])

    @pytest.mark.unit
    def test_返回耗时与吞吐指标(self, queries):
        rows = []
        for key, model, duration, ttft, output in (
            ("r9-1", "model-c", 100, 20, 50),
            ("r10-1", "model-c", 200, 30, 50),
            ("r11-1", "model-d", 1000, 100, 100),
        ):
            attempt = _attempt(
                key, "2026-01-03T10:00:00.000+00:00",
                user="user-c", model=model, size="1024x1024",
            )
            attempt.duration_ms = duration
            attempt.ttft_ms = ttft
            attempt.has_usage = True
            attempt.output_tokens = output
            rows.append(attempt.to_row())
        queries.store.insert_many(rows)
        queries.store.aggregate_usage()

        result = queries.dimension_usage(
            "model", since="2026-01-03", user="user-c", sort="p95", order="desc"
        )

        assert result["items"][0]["values"]["model"] == "model-d"
        by_model = {item["values"]["model"]: item for item in result["items"]}
        assert by_model["model-c"]["avg_duration_ms"] == pytest.approx(150.0)
        assert by_model["model-c"]["avg_ttft_ms"] == pytest.approx(25.0)
        assert by_model["model-c"]["avg_throughput"] == pytest.approx(100 * 1000 / 300)
        assert by_model["model-c"]["p95_duration_ms"] == 200
        assert by_model["model-d"]["p95_duration_ms"] == 1000

    @pytest.mark.unit
    def test_非法排序键被拒绝(self, queries):
        with pytest.raises(ValueError):
            queries.dimension_usage("model", sort="size")
        with pytest.raises(ValueError):
            queries.dimension_usage("model", order="sideways")

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
