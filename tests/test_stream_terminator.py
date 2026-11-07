"""流终止判定单元测试

覆盖 openai_proxy/utils/stream_terminator.py：
- is_done_event 只认 data 行的完整取值，正文里的 "[DONE]" 文本不算流结束
- [DONE] 之后的尾片（部分上游把 usage 放在 [DONE] 后面）在收尾窗口内仍会被读到
- [DONE] 之后上游挂起时，收尾窗口到期即结束转发，不再死等上游结束响应体
- 客户端断开（任务被取消）时取消信号原样外抛，供上层判定流被打断
- release 关闭上游迭代器并归还底层响应
"""

import asyncio
import time

import pytest

from openai_proxy.utils import stream_terminator
from openai_proxy.utils.stream_terminator import StreamTerminator, is_done_event


class _FakeResponse:
    """模拟上游 HTTP 响应对象，只关心是否被释放"""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeUpstream:
    """按脚本产出 SSE 块的假上游流：可选在 [DONE] 之后永久挂起"""

    def __init__(self, chunks, hang_at_end=False):
        self.chunks = list(chunks)
        self.hang_at_end = hang_at_end
        self.original_response = _FakeResponse()
        self.remaining = list(self.chunks)
        self.closed_cleanly = False
        self.interrupted = False

    async def __aiter__(self):
        try:
            for chunk in self.remaining:
                if chunk:
                    yield chunk
            if self.hang_at_end:
                await asyncio.Event().wait()
            self.closed_cleanly = True
        except (asyncio.CancelledError, GeneratorExit):
            self.interrupted = True
            raise


def _chunks_after_done(hang_at_end=False) -> _FakeUpstream:
    """典型上游：正文 → [DONE] → usage 尾片"""
    return _FakeUpstream(
        [
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
            b"data: [DONE]\n\n",
            b'data: {"usage":{"completion_tokens":7,"prompt_tokens":11}}\n\n',
        ],
        hang_at_end=hang_at_end,
    )


async def _drain(terminator: StreamTerminator):
    """模拟调用方的循环：读到 [DONE] 就通知终止器，然后把上游取空"""
    collected = []
    while True:
        chunk = await terminator.next_chunk()
        if chunk is None:
            return collected
        if is_done_event(chunk.decode("utf-8", "replace")):
            terminator.mark_done()
        collected.append(chunk)


class TestIsDoneEvent:
    """终止标记识别"""

    def test_data行完整取值就是终止标记(self):
        assert is_done_event("data: [DONE]\n\n") is True
        assert is_done_event("data: [DONE]") is True
        assert is_done_event('data:  [DONE]  \n\nevent: done') is True

    def test_带事件名的终止标记也识别(self):
        assert is_done_event("event: response.completed\ndata: [DONE]\n\n") is True

    def test_正文里出现DONE文本不算流结束(self):
        assert is_done_event('data: {"choices":[{"delta":{"content":"[DONE]"}}]}\n\n') is False

    def test_心跳注释与其它行不识别(self):
        assert is_done_event(": keep-alive\n\n") is False
        assert is_done_event('event: response.output_text.delta\ndata: {"delta":"x"}\n\n') is False


class TestDrainAfterDone:
    """[DONE] 之后的收尾窗口"""

    @pytest.mark.asyncio
    async def test_DONE之后的尾片仍会被读到(self):
        upstream = _chunks_after_done()
        terminator = StreamTerminator(upstream, drain_timeout=1.0)

        collected = await _drain(terminator)

        assert terminator.done_seen is True
        assert terminator.drain_expired is False
        assert len(collected) == 3
        assert b'"completion_tokens":7' in collected[-1]
        # 上游自己结束了，不需要额外释放
        assert upstream.closed_cleanly is True

    @pytest.mark.asyncio
    async def test_DONE之后上游挂起时窗口到期就结束(self, monkeypatch):
        """关键回归：旧实现会一直等上游结束 chunked body，客户端一断开就被误记为中断"""
        monkeypatch.setattr(stream_terminator, "DEFAULT_DRAIN_TIMEOUT", 0.2)
        upstream = _FakeUpstream([b"data: [DONE]\n\n"], hang_at_end=True)
        terminator = StreamTerminator(upstream)

        started = time.monotonic()
        collected = await _drain(terminator)
        elapsed = time.monotonic() - started

        assert len(collected) == 1
        assert terminator.done_seen is True
        assert terminator.drain_expired is True
        # 收尾窗口生效：不会拖到上游超时（测试里是永久挂起）
        assert elapsed < 2.0

    @pytest.mark.asyncio
    async def test_DONE之前仍然正常等上游(self, monkeypatch):
        """收尾窗口只在 [DONE] 之后生效，否则会把慢启动的流提前掐断"""
        monkeypatch.setattr(stream_terminator, "DEFAULT_DRAIN_TIMEOUT", 0.1)
        upstream = _FakeUpstream([b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'], hang_at_end=True)
        terminator = StreamTerminator(upstream)

        assert await terminator.next_chunk() is not None
        # 此时还没出现 [DONE]：取下一块必须继续等上游，而不是窗口到期返回 None
        pending = asyncio.create_task(terminator.next_chunk())
        await asyncio.sleep(0.3)
        assert pending.done() is False

        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert terminator.drain_expired is False


class TestCancellationAndRelease:
    """取消信号与资源释放"""

    @pytest.mark.asyncio
    async def test_客户端断开时取消信号原样外抛(self):
        upstream = _FakeUpstream([b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'], hang_at_end=True)
        terminator = StreamTerminator(upstream, drain_timeout=5.0)

        assert await terminator.next_chunk() is not None
        # 第二次取数挂在「等上游下一块」上：客户端此时关连接，任务被取消
        task = asyncio.create_task(terminator.next_chunk())
        await asyncio.sleep(0.05)
        assert task.done() is False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert upstream.interrupted is True

    @pytest.mark.asyncio
    async def test_release关闭上游并归还响应(self):
        upstream = _FakeUpstream([b"data: [DONE]\n\n"], hang_at_end=True)
        terminator = StreamTerminator(upstream, drain_timeout=1.0)

        await terminator.next_chunk()
        await terminator.release()

        assert upstream.original_response.closed is True
        assert upstream.interrupted is True

    @pytest.mark.asyncio
    async def test_release对已经结束的流是安全的(self):
        upstream = _FakeUpstream([b"data: [DONE]\n\n"])
        terminator = StreamTerminator(upstream, drain_timeout=1.0)
        await _drain(terminator)

        await terminator.release()
        await terminator.release()

        assert upstream.original_response.closed is True

    @pytest.mark.asyncio
    async def test_没有原始响应对象时只关迭代器(self):
        async def plain_stream():
            yield b"data: [DONE]\n\n"

        terminator = StreamTerminator(plain_stream(), drain_timeout=0.2)
        await _drain(terminator)
        await terminator.release()

        assert terminator.done_seen is True
