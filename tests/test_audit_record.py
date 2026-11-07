"""审计记录单元测试

覆盖：
- outcome / finalized 的口径（成功率统计的地基）
- 从 ClassifiedError 提取错误分类、错误码、HTTP 状态码
- 从 usage 统计合并 token 与性能字段
- 落库行的列对齐与布尔转换
- 隐私约束：审计行不允许出现消息内容字段
"""

import pytest

from openai_proxy.audit.record import (
    COLUMNS,
    FINAL_OUTCOMES,
    OUTCOME_ABORTED,
    OUTCOME_FAILURE,
    OUTCOME_STREAM_OPEN,
    OUTCOME_SUCCESS,
    AuditAttempt,
    build_attempt_key,
    to_ms,
)
from openai_proxy.model.error_classifier import ErrorClassifier


def _attempt(**overrides) -> AuditAttempt:
    """构造一条基础审计行"""
    params = dict(
        attempt_key="req1-1",
        gateway_request_id="req1",
        endpoint="chat_completions",
        stream=True,
        selector="all",
        platform="example-platform",
        model_name="example-platform|model-a",
        model_id="model-a",
        attempt_seq=1,
    )
    params.update(overrides)
    return AuditAttempt(**params)


class TestOutcome:
    """结局与收尾标记"""

    @pytest.mark.unit
    def test_流交给上层时先落未收尾的结论(self):
        attempt = _attempt()
        attempt.mark_stream_open()

        assert attempt.outcome == OUTCOME_STREAM_OPEN
        # 未收尾的流不能计入成功率
        assert attempt.finalized is False
        assert attempt.probe_ms is not None

    @pytest.mark.unit
    def test_非流式成功直接收尾(self):
        attempt = _attempt(stream=False)
        attempt.mark_success()

        assert attempt.outcome == OUTCOME_SUCCESS
        assert attempt.finalized is True
        assert attempt.probe_ms is None

    @pytest.mark.unit
    def test_失败与中断都是已收尾结局(self):
        failure = _attempt()
        failure.mark_failure()
        aborted = _attempt()
        aborted.mark_aborted()

        assert failure.outcome == OUTCOME_FAILURE and failure.finalized is True
        assert aborted.outcome == OUTCOME_ABORTED and aborted.finalized is True
        assert OUTCOME_STREAM_OPEN not in FINAL_OUTCOMES


class TestErrorFields:
    """错误字段提取（鸭子类型，不依赖具体分类器实现）"""

    @pytest.mark.unit
    def test_从分类错误提取分类与状态码(self):
        classified = ErrorClassifier.classify_http_error(429, "rate limited", "example-platform|model-a")
        attempt = _attempt()
        attempt.mark_failure(classified)

        assert attempt.error_category == classified.category.value
        assert attempt.http_status == 429
        assert attempt.error_code
        assert attempt.disabled_model == bool(classified.should_disable_model)

    @pytest.mark.unit
    def test_未知错误也不会抛异常(self):
        attempt = _attempt()
        attempt.mark_failure(ValueError("上游异常"))

        assert attempt.outcome == OUTCOME_FAILURE
        # 没有分类信息时用异常类型名兜底
        assert attempt.error_code == "ValueError"

    @pytest.mark.unit
    def test_字符串错误不会污染错误码(self):
        attempt = _attempt()
        attempt.mark_failure("plain error text")

        assert attempt.error_category is None
        assert attempt.error_code is None


class TestUsageStats:
    """token 与性能字段"""

    @pytest.mark.unit
    def test_有usage时写入token用量(self):
        attempt = _attempt()
        attempt.apply_stats({
            "has_usage": True,
            "ttft": 1.234,
            "total": 10.0,
            "input_tokens": 120,
            "cached_tokens": 64,
            "output_tokens": 350,
            "output_chars": 820,
            "throughput": 35.0,
            "upstream_id": "chatcmpl-abc",
            "keepalive_count": 3,
        })

        assert attempt.has_usage is True
        assert attempt.ttft_ms == 1234
        assert attempt.duration_ms == 10000
        assert attempt.input_tokens == 120
        assert attempt.cached_tokens == 64
        assert attempt.output_tokens == 350
        assert attempt.throughput == 35.0
        assert attempt.upstream_id == "chatcmpl-abc"
        assert attempt.keepalive_count == 3

    @pytest.mark.unit
    def test_上游没有usage时保持未知而不是0(self):
        attempt = _attempt()
        attempt.apply_stats({"has_usage": False, "input_tokens": 0, "output_tokens": 0, "total": 5.0})

        assert attempt.has_usage is False
        assert attempt.input_tokens is None
        assert attempt.output_tokens is None

    @pytest.mark.unit
    def test_非字典统计不会抛异常(self):
        attempt = _attempt()
        attempt.apply_stats(None)
        attempt.apply_stats("oops")

        assert attempt.ttft_ms is None


class TestRow:
    """落库行"""

    @pytest.mark.unit
    def test_落库列与建表列完全对齐(self):
        row = _attempt().to_row()

        assert set(row.keys()) == set(COLUMNS)

    @pytest.mark.unit
    def test_布尔转换成整数以适配sqlite(self):
        row = _attempt(disabled_model=True, finalized=True).to_row()

        assert row["disabled_model"] == 1
        assert row["finalized"] == 1

    @pytest.mark.unit
    def test_审计行不包含任何消息内容字段(self):
        forbidden = {"messages", "content", "prompt", "api_key", "authorization", "body", "response"}

        assert forbidden.isdisjoint(set(COLUMNS))

    @pytest.mark.unit
    def test_主键由请求号与尝试序号组成(self):
        assert build_attempt_key("req1", 2) == "req1-2"

    @pytest.mark.unit
    def test_秒转毫秒保留无效值(self):
        assert to_ms(1.234) == 1234
        assert to_ms(1.2349) == 1235
        assert to_ms(None) is None
        assert to_ms("bad") is None
