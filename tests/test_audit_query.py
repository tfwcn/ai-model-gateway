"""审计查询与聚合单元测试

覆盖：
- 成功率只统计已收尾的行
- 按模型/平台/小时分桶
- P95 近似值
- 模型榜排序
- 明细过滤与故障转移链
- 非法参数抛 ValueError
- 相对时间解析（7d / 24h / 30m）
"""

import pytest

from openai_proxy.audit.query import UsageQueryService, parse_time
from openai_proxy.audit.record import AuditAttempt
from openai_proxy.audit.store import SqliteAuditStore


@pytest.fixture
def queries(tmp_path):
    """带样本数据的查询服务"""
    store = SqliteAuditStore(str(tmp_path / "audit.db"))
    store.init_schema()
    _seed(store)
    yield UsageQueryService(store)
    store.close()


def _insert(store: SqliteAuditStore, key: str, ts: str, model: str, outcome: str, **extra) -> None:
    """写入一条样本审计行"""
    platform = model.split("|", 1)[0]
    attempt = AuditAttempt(
        attempt_key=key,
        gateway_request_id=key.rsplit("-", 1)[0],
        endpoint="chat_completions",
        stream=True,
        selector=model,
        platform=platform,
        model_name=model,
        model_id=model.split("|", 1)[-1],
        ts=ts,
    )
    if outcome == "failure":
        attempt.mark_failure()
    elif outcome == "aborted":
        attempt.mark_stream_open()
        attempt.mark_aborted()
    elif outcome == "stream_open":
        attempt.mark_stream_open()
    else:
        attempt.mark_stream_open()
        attempt.apply_stats({
            "has_usage": True,
            "ttft": extra.get("ttft", 1.0),
            "total": (extra.get("duration_ms", 1000) or 1000) / 1000.0,
            "input_tokens": extra.get("input_tokens", 100),
            "cached_tokens": extra.get("cached_tokens", 0),
            "output_tokens": extra.get("output_tokens", 200),
            "output_chars": 500,
            "throughput": extra.get("throughput", 20.0),
        })
        attempt.outcome = "success"
        attempt.finalized = True

    store.insert_many([attempt.to_row()])


def _seed(store: SqliteAuditStore) -> None:
    """样本：两个模型、两种结局、跨两个小时"""
    _insert(store, "r1-1", "2026-01-01T10:05:00.000+00:00", "example-platform|model-a", "success",
            duration_ms=1000, ttft=1.0, output_tokens=200, throughput=20.0)
    _insert(store, "r2-1", "2026-01-01T10:06:00.000+00:00", "example-platform|model-a", "success",
            duration_ms=3000, ttft=2.0, output_tokens=300, throughput=10.0)
    _insert(store, "r3-1", "2026-01-01T10:07:00.000+00:00", "example-platform|model-a", "failure")
    _insert(store, "r4-1", "2026-01-01T11:01:00.000+00:00", "example-platform|model-b", "aborted")
    # 未收尾的流：不能计入成功率
    _insert(store, "r5-1", "2026-01-01T11:02:00.000+00:00", "example-platform|model-b", "stream_open")


class TestParseTime:
    """时间参数解析"""

    @pytest.mark.unit
    def test_相对时间换算成utc(self):
        assert parse_time("7d").startswith("20")
        assert parse_time("24h") is not None
        assert parse_time("30m") is not None

    @pytest.mark.unit
    def test_日期补全成时刻(self):
        assert parse_time("2026-01-01") == "2026-01-01T00:00:00.000+00:00"

    @pytest.mark.unit
    def test_非法时间抛value_error(self):
        with pytest.raises(ValueError):
            parse_time("not-a-time")

    @pytest.mark.unit
    def test_空值返回默认(self):
        assert parse_time(None, default="fallback") == "fallback"
        assert parse_time("", default="fallback") == "fallback"


class TestSummary:
    """聚合口径"""

    @pytest.mark.unit
    def test_按模型聚合的计数与成功率(self, queries):
        result = queries.summary(group_by="model", since="2026-01-01")

        by_model = {item["bucket"]: item for item in result["items"]}
        model_a = by_model["example-platform|model-a"]
        assert model_a["attempts"] == 3
        assert model_a["requests"] == 3
        assert model_a["success"] == 2
        assert model_a["failure"] == 1
        # 成功率分母只含已收尾的行
        assert model_a["success_rate"] == pytest.approx(2 / 3, abs=1e-3)

    @pytest.mark.unit
    def test_未收尾的流不计入成功率(self, queries):
        result = queries.summary(group_by="model", since="2026-01-01")

        model_b = {item["bucket"]: item for item in result["items"]}["example-platform|model-b"]
        assert model_b["attempts"] == 2
        assert model_b["finalized"] == 1
        assert model_b["aborted"] == 1
        assert model_b["success_rate"] == 0.0

    @pytest.mark.unit
    def test_token用量按组求和(self, queries):
        result = queries.summary(group_by="model", since="2026-01-01")

        model_a = {item["bucket"]: item for item in result["items"]}["example-platform|model-a"]
        assert model_a["input_tokens"] == 200
        assert model_a["output_tokens"] == 500

    @pytest.mark.unit
    def test_p95近似值存在(self, queries):
        result = queries.summary(group_by="model", since="2026-01-01")

        model_a = {item["bucket"]: item for item in result["items"]}["example-platform|model-a"]
        # 样本里最长的一次成功调用是 3000ms
        assert model_a["p95_duration_ms"] == 3000

    @pytest.mark.unit
    def test_按小时分桶(self, queries):
        result = queries.summary(group_by="hour", since="2026-01-01")

        buckets = sorted(item["bucket"] for item in result["items"])
        assert buckets == ["2026-01-01T10", "2026-01-01T11"]

    @pytest.mark.unit
    def test_非法分组抛value_error(self, queries):
        with pytest.raises(ValueError):
            queries.summary(group_by="DROP TABLE")


class TestTimeseries:
    """时序"""

    @pytest.mark.unit
    def test_按小时出点(self, queries):
        result = queries.timeseries(bucket="hour", since="2026-01-01")

        assert [point["bucket"] for point in result["points"]] == ["2026-01-01T10", "2026-01-01T11"]

    @pytest.mark.unit
    def test_非法粒度抛value_error(self, queries):
        with pytest.raises(ValueError):
            queries.timeseries(bucket="minute")


class TestRanking:
    """模型榜"""

    @pytest.mark.unit
    def test_按p95排序把慢模型排在前面(self, queries):
        result = queries.model_ranking(sort="p95", since="2026-01-01")

        assert result["items"][0]["bucket"] == "example-platform|model-a"

    @pytest.mark.unit
    def test_按throughput排序把慢速模型排在前面(self, queries):
        result = queries.model_ranking(sort="throughput", since="2026-01-01")

        assert result["items"][0]["bucket"] == "example-platform|model-a"

    @pytest.mark.unit
    def test_非法排序抛value_error(self, queries):
        with pytest.raises(ValueError):
            queries.model_ranking(sort="random")


class TestDetails:
    """明细与链"""

    @pytest.mark.unit
    def test_按结局过滤(self, queries):
        result = queries.list_attempts(outcome="failure", since="2026-01-01")

        assert result["total"] == 1
        assert result["items"][0]["outcome"] == "failure"

    @pytest.mark.unit
    def test_非法结局抛value_error(self, queries):
        with pytest.raises(ValueError):
            queries.list_attempts(outcome="unknown")

    @pytest.mark.unit
    def test_故障转移链按尝试序号还原(self, queries):
        result = queries.chain("r1")

        assert len(result["attempts"]) == 1
        assert result["attempts"][0]["model_name"] == "example-platform|model-a"

    @pytest.mark.unit
    def test_分页参数生效(self, queries):
        first = queries.list_attempts(limit=2, offset=0, since="2026-01-01")
        second = queries.list_attempts(limit=2, offset=2, since="2026-01-01")

        assert first["total"] == 5
        assert len(first["items"]) == 2
        assert len(second["items"]) == 2

    @pytest.mark.unit
    def test_清理只删除指定时间之前(self, queries):
        removed = queries.prune("2026-01-01T11:00:00.000+00:00")

        assert removed == 3
        assert queries.list_attempts()["total"] == 2


class TestDailyUsage:
    """全量日用量：按天 + 用户，不受明细清理影响"""

    @pytest.fixture
    def queries(self, tmp_path):
        store = SqliteAuditStore(str(tmp_path / "audit.db"))
        store.init_schema()
        store.insert_many([
            _usage_row("r1-1", "2026-01-01T10:00:00.000+00:00", "user-a", 100, 10, 200),
            _usage_row("r2-1", "2026-01-02T10:00:00.000+00:00", "user-a", 50, 5, 80),
            _usage_row("r3-1", "2026-01-02T11:00:00.000+00:00", "user-b", 30, 0, 40),
        ])
        store.aggregate_usage()
        yield UsageQueryService(store)
        store.close()

    @pytest.mark.unit
    def test_按天与用户返回token(self, queries):
        result = queries.daily_usage()

        assert result["count"] == 3
        assert [(row["day"], row["api_user"]) for row in result["items"]] == [
            ("2026-01-01", "user-a"),
            ("2026-01-02", "user-a"),
            ("2026-01-02", "user-b"),
        ]
        assert result["items"][0]["input_tokens"] == 100
        assert result["items"][0]["cached_tokens"] == 10
        assert result["items"][0]["output_tokens"] == 200

    @pytest.mark.unit
    def test_汇总合计与用户过滤(self, queries):
        all_rows = queries.daily_usage()
        assert all_rows["totals"]["output_tokens"] == 320
        assert all_rows["totals"]["input_tokens"] == 180

        only_user_b = queries.daily_usage(user="user-b")
        assert only_user_b["count"] == 1
        assert only_user_b["totals"]["output_tokens"] == 40

    @pytest.mark.unit
    def test_按日期过滤(self, queries):
        result = queries.daily_usage(since="2026-01-02", until="2026-01-03")

        assert result["count"] == 2
        assert {row["day"] for row in result["items"]} == {"2026-01-02"}


def _usage_row(key: str, ts: str, api_user: str, input_tokens: int, cached: int, output: int) -> dict:
    """构造一条带 token 用量的成功审计行"""
    attempt = AuditAttempt(
        attempt_key=key,
        gateway_request_id=key.rsplit("-", 1)[0],
        endpoint="chat_completions",
        stream=False,
        platform="example-platform",
        model_name="example-platform|model-a",
        model_id="model-a",
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
