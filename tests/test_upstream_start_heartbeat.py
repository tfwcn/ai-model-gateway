"""等待上游返回流对象期间的心跳（UpstreamStartHeartbeat）单元测试"""

import asyncio

import pytest

from openai_proxy.utils.keepalive import KeepAliveStream, UpstreamStartHeartbeat


class TestUpstreamStartHeartbeat:
    @pytest.mark.asyncio
    async def test_静默期产出心跳并返回结果(self):
        """等待期间空闲即产出心跳，结束后通过 result 取回上游结果"""

        async def _slow_target():
            await asyncio.sleep(0.08)
            return "stream-object"

        waiter = UpstreamStartHeartbeat(_slow_target(), interval=0.02)
        collected = [chunk async for chunk in waiter]

        assert collected.count(KeepAliveStream.HEARTBEAT) >= 1
        assert waiter.result == "stream-object"

    @pytest.mark.asyncio
    async def test_间隔为0时关闭心跳(self):
        """interval<=0 时不产出心跳，直接等待结果"""

        async def _target():
            await asyncio.sleep(0.03)
            return 42

        waiter = UpstreamStartHeartbeat(_target(), interval=0)
        assert [chunk async for chunk in waiter] == []
        assert waiter.result == 42

    @pytest.mark.asyncio
    async def test_上游异常原样抛出(self):
        """上游异常在迭代结束后抛出，交由调用方按原逻辑处理"""

        async def _boom():
            await asyncio.sleep(0.01)
            raise ValueError("upstream failed")

        waiter = UpstreamStartHeartbeat(_boom(), interval=0.01)
        with pytest.raises(ValueError):
            async for _ in waiter:
                pass

    @pytest.mark.asyncio
    async def test_提前退出时回收上游任务(self):
        """客户端断开导致提前退出时，上游任务必须被取消回收"""
        cancelled = asyncio.Event()

        async def _pending():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        waiter = UpstreamStartHeartbeat(_pending(), interval=0.01)
        agen = waiter.__aiter__()
        # 先拿到一个心跳，再模拟客户端断开（生成器被关闭）
        assert await agen.__anext__() == KeepAliveStream.HEARTBEAT
        await agen.aclose()

        assert cancelled.is_set() is True
