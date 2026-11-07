"""流式审计结局单元测试

盯着一个真实踩点：按 SSE 协议读流的客户端拿到 [DONE]/finish_reason 就关连接去执行工具，
此时上游可能还没结束 chunked body。旧实现把这种情况记成「客户端中断」，
成功率被系统性压低。

覆盖：
- 转发正常走完：success + done_sent=1
- 客户端在 [DONE] 之前断开：aborted + done_sent=0
- 客户端在 [DONE] 之后关闭：success + done_sent=1（这是工具调用的正常节奏）
- 上游在 [DONE] 之后挂起：不等上游结束也能收尾，且跟在后面的 usage 尾片仍被统计
"""

import asyncio

import pytest

from openai_proxy.audit.config import AuditConfig
from openai_proxy.audit.manager import AuditManager
from openai_proxy.adapter.responses import ResponsesAdapter
from openai_proxy.models import ModelConfig
from openai_proxy.service import _convert_responses_sse, _forward_normalized_sse
from openai_proxy.utils import stream_terminator

_DELTA = b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
_DONE = b"data: [DONE]\n\n"
_USAGE_TAIL = b'data: {"usage":{"prompt_tokens":11,"completion_tokens":7}}\n\n'


def _model_config() -> ModelConfig:
    """构造测试用模型配置（示例平台/示例模型，不使用任何真实上游）"""
    return ModelConfig(
        name="example-platform|model-a",
        api_key="EXAMPLE_PLATFORM_API_KEY",
        base_url="https://api.example.com/v1",
        model="model-a",
        timeout=30,
        retry_count=1,
    )


class _FakeUpstream:
    """按脚本产出 SSE 块的假上游流，可模拟「[DONE] 之后还挂着不结束」"""

    def __init__(self, chunks, hang_at_end=False):
        self.chunks = list(chunks)
        self.hang_at_end = hang_at_end
        self.interrupted = False

    async def __aiter__(self):
        try:
            for chunk in self.chunks:
                if chunk:
                    yield chunk
            if self.hang_at_end:
                await asyncio.Event().wait()
        except (asyncio.CancelledError, GeneratorExit):
            self.interrupted = True
            raise


async def _audit(tmp_path, **overrides) -> AuditManager:
    """启动一个写入临时库的审计管理器（测试结束自动关闭）"""
    config = AuditConfig(
        db_path=str(tmp_path / "audit.db"),
        flush_interval=30,
        prometheus_enabled=False,
        **overrides,
    )
    manager = AuditManager(config)
    await manager.start()
    return manager


async def _rows(manager: AuditManager):
    """强制刷盘后读回全部审计行"""
    await manager.collector._flush_once()
    return manager.store.query("SELECT * FROM audit_attempts ORDER BY attempt_seq")


async def _open_stream(audit: AuditManager, upstream: _FakeUpstream):
    """走一次真实的 stream_open 收尾链路，返回审计上下文"""
    ctx = audit.new_context(endpoint="chat_completions", selector="all", stream=True)

    async def _fake_call():
        return True, upstream

    _success, _stream = await ctx.run_attempt(_model_config(), _fake_call())
    return ctx


class TestStreamOutcome:
    """转发结局与审计结局的一致性"""

    @pytest.mark.asyncio
    async def test_转发正常走完时记success且done_sent为1(self, tmp_path, monkeypatch):
        monkeypatch.setattr(stream_terminator, "DEFAULT_DRAIN_TIMEOUT", 0.2)
        audit = await _audit(tmp_path)
        try:
            upstream = _FakeUpstream([_DELTA, _DONE, _USAGE_TAIL])
            ctx = await _open_stream(audit, upstream)
            forwarder = _forward_normalized_sse(upstream, ctx)
            async for _ in forwarder:
                pass

            rows = await _rows(audit)
            assert rows[0]["outcome"] == "success"
            assert rows[0]["done_sent"] == 1
            assert rows[0]["finalized"] == 1
            assert upstream.interrupted is False
        finally:
            await audit.close()

    @pytest.mark.asyncio
    async def test_客户端在DONE之前断开时记aborted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(stream_terminator, "DEFAULT_DRAIN_TIMEOUT", 0.2)
        audit = await _audit(tmp_path)
        try:
            upstream = _FakeUpstream([_DELTA], hang_at_end=True)
            ctx = await _open_stream(audit, upstream)
            forwarder = _forward_normalized_sse(upstream, ctx)

            async for _ in forwarder:
                break  # 只收一块就停止迭代：等价于客户端提前断开
            await forwarder.aclose()

            rows = await _rows(audit)
            assert rows[0]["outcome"] == "aborted"
            assert rows[0]["done_sent"] == 0
            assert rows[0]["finalized"] == 1
        finally:
            await audit.close()

    @pytest.mark.asyncio
    async def test_客户端在DONE之后关闭仍记success(self, tmp_path, monkeypatch):
        """回归用例：工具调用场景里客户端读到 [DONE] 就断开，不是流被中断"""
        monkeypatch.setattr(stream_terminator, "DEFAULT_DRAIN_TIMEOUT", 0.2)
        audit = await _audit(tmp_path)
        try:
            # usage 在 [DONE] 之前：客户端断开时用量已经收到
            upstream = _FakeUpstream([_DELTA, _USAGE_TAIL, _DONE], hang_at_end=True)
            ctx = await _open_stream(audit, upstream)
            forwarder = _forward_normalized_sse(upstream, ctx)

            async for chunk in forwarder:
                if b"[DONE]" in chunk:
                    break  # 客户端拿到终止标记就关连接
            await forwarder.aclose()

            rows = await _rows(audit)
            assert rows[0]["outcome"] == "success"
            assert rows[0]["done_sent"] == 1
            assert rows[0]["has_usage"] == 1
            assert rows[0]["output_tokens"] == 7
        finally:
            await audit.close()

    @pytest.mark.asyncio
    async def test_上游在DONE之后挂起时不再死等(self, tmp_path, monkeypatch):
        """上游迟迟不结束 chunked body 时，转发也要在收尾窗口后正常收尾"""
        monkeypatch.setattr(stream_terminator, "DEFAULT_DRAIN_TIMEOUT", 0.3)
        audit = await _audit(tmp_path)
        try:
            upstream = _FakeUpstream([_DELTA, _DONE, _USAGE_TAIL], hang_at_end=True)
            ctx = await _open_stream(audit, upstream)
            forwarder = _forward_normalized_sse(upstream, ctx)

            received = []
            loop = asyncio.get_event_loop()
            started = loop.time()
            async for chunk in forwarder:
                received.append(chunk)
                assert loop.time() - started < 5  # 不会拖到上游超时

            rows = await _rows(audit)
            assert rows[0]["outcome"] == "success"
            assert rows[0]["done_sent"] == 1
            # 跟在 [DONE] 之后的 usage 尾片在收尾窗口里被读到并统计
            assert rows[0]["has_usage"] == 1
            assert rows[0]["output_tokens"] == 7
            assert upstream.interrupted is True
            assert b"[DONE]" in b"".join(received)
        finally:
            await audit.close()

    @pytest.mark.asyncio
    async def test_上游没发DONE时网关补发并按完成记账(self, tmp_path, monkeypatch):
        monkeypatch.setattr(stream_terminator, "DEFAULT_DRAIN_TIMEOUT", 0.2)
        audit = await _audit(tmp_path)
        try:
            upstream = _FakeUpstream([_DELTA])
            ctx = await _open_stream(audit, upstream)
            forwarder = _forward_normalized_sse(upstream, ctx)

            received = []
            async for chunk in forwarder:
                received.append(chunk)

            assert received[-1] == b"data: [DONE]\n\n"
            rows = await _rows(audit)
            assert rows[0]["outcome"] == "success"
            assert rows[0]["done_sent"] == 1
        finally:
            await audit.close()


class TestResponsesPathOutcome:
    """/v1/responses 的转发链路：终止事件由适配器补齐"""

    @pytest.mark.asyncio
    async def test_协议转换正常收尾时记done_sent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(stream_terminator, "DEFAULT_DRAIN_TIMEOUT", 0.2)
        audit = await _audit(tmp_path)
        try:
            adapter = ResponsesAdapter()
            upstream = _FakeUpstream([_DELTA, _DONE])
            ctx = await _open_stream(audit, upstream)
            forwarder = _convert_responses_sse(upstream, adapter, adapter.context, ctx)

            received = []
            async for chunk in forwarder:
                received.append(chunk)

            rows = await _rows(audit)
            assert rows[0]["outcome"] == "success"
            assert rows[0]["done_sent"] == 1
            # 完成事件与终止标记都在这里交给客户端
            body = b"".join(received)
            assert b"response.completed" in body
            assert b"[DONE]" in body
        finally:
            await audit.close()

    @pytest.mark.asyncio
    async def test_协议转换链路被中途打断时记aborted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(stream_terminator, "DEFAULT_DRAIN_TIMEOUT", 0.2)
        audit = await _audit(tmp_path)
        try:
            adapter = ResponsesAdapter()
            upstream = _FakeUpstream([_DELTA], hang_at_end=True)
            ctx = await _open_stream(audit, upstream)
            forwarder = _convert_responses_sse(upstream, adapter, adapter.context, ctx)

            async for _ in forwarder:
                break
            await forwarder.aclose()

            rows = await _rows(audit)
            assert rows[0]["outcome"] == "aborted"
            assert rows[0]["done_sent"] == 0
        finally:
            await audit.close()
