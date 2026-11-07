"""输出速度埋点（stream_metrics）单元测试"""

import json
import time

import pytest

from openai_proxy.utils.stream_metrics import (
    StreamPerformanceMonitor,
    extract_cached_tokens,
    extract_usage,
    report_non_stream_usage,
)


@pytest.mark.unit
def test_extract_usage_兼容两种协议命名():
    """直接 usage 与 responses 嵌套 usage 都要能取到"""
    assert extract_usage({"usage": {"completion_tokens": 3}}) == {"completion_tokens": 3}
    assert extract_usage({"response": {"usage": {"output_tokens": 5}}}) == {"output_tokens": 5}
    assert extract_usage({"choices": []}) is None
    assert extract_usage(None) is None


@pytest.mark.unit
def test_extract_cached_tokens_兼容各家字段命名():
    """OpenAI/Azure 的 details 嵌套、DeepSeek 的扁平字段都要能取到"""
    assert extract_cached_tokens({"prompt_tokens_details": {"cached_tokens": 128}}) == 128
    assert extract_cached_tokens({"input_tokens_details": {"cached_tokens": 64}}) == 64
    assert extract_cached_tokens({"prompt_cache_hit_tokens": 32}) == 32
    assert extract_cached_tokens({"cache_read_input_tokens": 16}) == 16
    # 没有缓存字段时是 0（上游未上报 = 无命中）
    assert extract_cached_tokens({"prompt_tokens": 10}) == 0


@pytest.mark.unit
def test_流式事件累积token与字符数():
    """流式场景：逐事件喂入，末块 usage 生效，增量文本累加"""
    monitor = StreamPerformanceMonitor(model_name="m1", request_id="abc", started_at=time.time() - 4)
    payloads = [
        {"id": "chatcmpl-abc", "choices": [{"delta": {"content": "你"}}]},
        {"id": "chatcmpl-abc", "choices": [{"delta": {"content": "好"}}]},
        {"id": "chatcmpl-abc", "choices": [{"delta": {"tool_calls": [{"function": {"arguments": "{}"}}]}}]},
        {"id": "chatcmpl-abc", "choices": [], "usage": {
            "prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17,
            "prompt_tokens_details": {"cached_tokens": 6},
        }},
    ]
    for payload in payloads:
        monitor.observe(f"data: {json.dumps(payload, ensure_ascii=False)}\n\ndata: [DONE]")
    stats = monitor.finish()

    assert stats["upstream_id"] == "chatcmpl-abc"
    assert stats["has_usage"] is True
    assert stats["input_tokens"] == 10
    assert stats["cached_tokens"] == 6
    assert stats["output_tokens"] == 7
    # "你" + "好" + "{}" = 4 个字符
    assert stats["output_chars"] == 4
    assert stats["total"] > 0
    assert stats["throughput"] is not None


@pytest.mark.unit
def test_上游id兼容缺失或异常取值():
    """上游不返回 id、id 为 null 或非字符串时都不能报错，且不覆盖已有的合法 id"""
    monitor = StreamPerformanceMonitor(model_name="m", started_at=time.time() - 1)
    monitor.observe('data: {"choices":[{"delta":{"content":"a"}}]}')
    assert monitor.finish()["upstream_id"] == ""

    monitor.observe('data: {"id": null, "choices":[{"delta":{"content":"b"}}]}')
    monitor.observe('data: {"id": 12345, "choices":[{"delta":{"content":"c"}}]}')
    monitor.observe('data: {"id": {"nested": 1}, "choices":[{"delta":{"content":"d"}}]}')
    assert monitor.finish()["upstream_id"] == ""

    monitor.observe('data: {"id": "resp_1", "choices":[{"delta":{"content":"e"}}]}')
    monitor.observe('data: {"id": "", "choices":[{"delta":{"content":"f"}}]}')
    assert monitor.finish()["upstream_id"] == "resp_1"


@pytest.mark.unit
def test_畸形事件不抛异常():
    """非 JSON、截断 JSON、非字典 JSON、空事件都要被安全跳过"""
    monitor = StreamPerformanceMonitor(model_name="m", started_at=time.time() - 1)
    for event in ["", "   ", "event: ping", "data: [DONE]", "data: {bad json", "data: [1,2,3]", "data: \"str\""]:
        monitor.observe(event)
    stats = monitor.finish()

    assert stats["output_tokens"] == 0
    assert stats["output_chars"] == 0
    assert stats["throughput"] is None


@pytest.mark.unit
def test_无usage时速度为空():
    """上游不给 usage 时不编造速度，token 值标记为未知（日志显示 -）"""
    monitor = StreamPerformanceMonitor(model_name="m2", started_at=time.time() - 2)
    monitor.observe('data: {"choices":[{"delta":{"content":"hello"}}]}')
    stats = monitor.finish()

    assert stats["has_usage"] is False
    assert stats["output_tokens"] == 0
    assert stats["throughput"] is None
    assert stats["output_chars"] == 5
    # 未知的 token 字段在日志里用 - 占位，而不是 0
    assert "输出tokens=-" in StreamPerformanceMonitor._format(stats)


@pytest.mark.unit
def test_非流式一键埋点():
    """非流式响应体直接统计，无首块耗时"""
    response = {
        "id": "chatcmpl-test",
        "choices": [{"message": {"content": "ok"}}],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "total_tokens": 3,
            "prompt_tokens_details": {"cached_tokens": 1},
        },
    }
    stats = report_non_stream_usage("m3", time.time() - 1, response)

    assert stats["upstream_id"] == "chatcmpl-test"
    assert stats["ttft"] is None
    assert stats["input_tokens"] == 1
    assert stats["cached_tokens"] == 1
    assert stats["output_tokens"] == 2
    assert stats["output_chars"] == 2
    assert stats["throughput"] is not None


@pytest.mark.unit
def test_从流对象读取埋点元信息():
    """failover 层挂在流对象上的元信息应被读取到"""

    class FakeStream:
        model_name = "tokenhub|demo"
        request_id = "deadbeef"
        started_at = 123.0

    monitor = StreamPerformanceMonitor.from_stream(FakeStream())
    assert monitor.model_name == "tokenhub|demo"
    assert monitor.request_id == "deadbeef"
    assert monitor.started_at == 123.0

    # 属性缺失时使用默认值，不抛异常
    fallback = StreamPerformanceMonitor.from_stream(object())
    assert fallback.model_name == "unknown"
    assert fallback.request_id == ""
