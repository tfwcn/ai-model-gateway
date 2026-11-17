"""流式快速失败窗口测试

窗口内失败要能用真实 HTTP 状态码响应；超时才转入 SSE + 心跳模式。
"""

import asyncio

import pytest

from openai_proxy.core.error_envelope import AttemptFailure, ErrorEnvelope, GatewayFailoverError
from openai_proxy.utils.stream_start_gate import StreamStartGate


def _envelope() -> ErrorEnvelope:
    return ErrorEnvelope([AttemptFailure(category="http_429_rate_limit", http_status=429, message="slow down")])


class TestStreamStartGate:
    @pytest.mark.asyncio
    async def test_窗口内成功直接拿到结果(self):
        async def _quick():
            return "stream-object"

        outcome = await StreamStartGate(grace=1).open(_quick())

        assert not outcome.failed
        assert outcome.task.done()
        assert outcome.task.result() == "stream-object"

    @pytest.mark.asyncio
    async def test_窗口内失败带出真实异常(self):
        async def _fail():
            raise GatewayFailoverError(_envelope())

        outcome = await StreamStartGate(grace=1).open(_fail())

        assert outcome.failed
        assert isinstance(outcome.error, GatewayFailoverError)
        assert outcome.error.envelope.http_status == 429

    @pytest.mark.asyncio
    async def test_超时后任务继续运行(self):
        async def _slow():
            await asyncio.sleep(0.05)
            return "late-stream"

        outcome = await StreamStartGate(grace=0.01).open(_slow())

        assert not outcome.failed
        assert not outcome.task.done()
        # 超时分支由流生成器继续等待同一个任务
        assert await outcome.task == "late-stream"

    @pytest.mark.asyncio
    async def test_grace为零时立即返回(self):
        async def _slow():
            await asyncio.sleep(0.05)
            return "late-stream"

        outcome = await StreamStartGate(grace=0).open(_slow())

        assert not outcome.failed
        assert outcome.task is not None
        assert await outcome.task == "late-stream"