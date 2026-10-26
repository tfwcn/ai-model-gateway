"""
Tests for SSE keep-alive stream wrapper.
"""
import asyncio

import pytest

from openai_proxy.utils.keepalive import KeepAliveStream


async def _delayed_source(chunks, delay: float):
    """按序产出 chunk，每个 chunk 前先等待 delay（制造静默期）。"""
    for chunk in chunks:
        if delay:
            await asyncio.sleep(delay)
        yield chunk


class TestKeepAliveStream:
    @pytest.mark.asyncio
    async def test_passthrough_when_disabled(self):
        """interval<=0 时关闭心跳，原样透传。"""
        stream = KeepAliveStream(_delayed_source([b"a", b"b"], 0), interval=0)
        assert [chunk async for chunk in stream] == [b"a", b"b"]

    @pytest.mark.asyncio
    async def test_heartbeat_during_idle_period(self):
        """源流静默超过间隔时插入注释心跳，且不丢业务数据。"""
        stream = KeepAliveStream(_delayed_source([b"data: x\n\n"], 0.08), interval=0.02)
        collected = [chunk async for chunk in stream]
        assert collected[-1] == b"data: x\n\n"
        assert collected.count(KeepAliveStream.HEARTBEAT) >= 1

    @pytest.mark.asyncio
    async def test_no_heartbeat_when_source_is_fast(self):
        """源流持续有数据时不应插入心跳。"""
        stream = KeepAliveStream(_delayed_source([b"a", b"b", b"c"], 0), interval=1.0)
        collected = [chunk async for chunk in stream]
        assert KeepAliveStream.HEARTBEAT not in collected

    @pytest.mark.asyncio
    async def test_source_exception_propagates(self):
        """源流异常应向上抛出，交由调用方按原逻辑处理。"""
        async def _boom():
            yield b"a"
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            async for _ in KeepAliveStream(_boom(), interval=1.0):
                pass
