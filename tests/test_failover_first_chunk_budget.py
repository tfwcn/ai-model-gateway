"""流式「首块预算」故障转移单元测试

覆盖 call_model_stream 在以下场景的行为：
- 连接建立/等响应头超出首块预算 → 首块超时（可切换模型）
- 响应头已返回但首块迟迟不来 → 首块超时
- 上游只回 keep-alive 注释 → 探测窗口被预算截断 → 首块超时
- 首块正常到达 → 照常成功（回归保护）
- 连接超时与首块预算的取值优先级
"""

import asyncio

import pytest

from openai_proxy.model.error_classifier import ErrorCategory
from openai_proxy.model.failover import READ_FIRST_CHUNK_TIMEOUT, ModelFailoverManager
from openai_proxy.models import ModelConfig


def _model_config(**overrides) -> ModelConfig:
    """构造测试用模型配置"""
    params = dict(
        name="test|model",
        api_key="k",
        base_url="https://example.com/v1",
        model="model",
        timeout=120,
    )
    params.update(overrides)
    return ModelConfig(**params)


def _manager(model_config: ModelConfig, session) -> ModelFailoverManager:
    """构造注入了假 session 的故障转移管理器"""
    manager = ModelFailoverManager(models={"test": [model_config]})
    manager.session = session
    return manager


class _FakeContent:
    """模拟 aiohttp 响应内容：read() 可控、iter_any() 依次产出剩余块"""

    def __init__(self, first_read, chunks=None):
        self._first_read = first_read
        self._chunks = list(chunks or [])
        self._read_calls = 0

    async def read(self, _size):
        self._read_calls += 1
        if callable(self._first_read):
            return await self._first_read(self._read_calls)
        return self._first_read

    async def iter_any(self):
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    """模拟 aiohttp 响应对象"""

    def __init__(self, content, content_type="text/event-stream", status=200):
        self.content = content
        self.headers = {"Content-Type": content_type}
        self.status = status
        self.closed = False

    def close(self):
        self.closed = True


class _FakeSession:
    """模拟 aiohttp 会话：记录超时设置，post 行为可注入"""

    closed = False

    def __init__(self, response=None, post_delay=0.0):
        self._response = response
        self._post_delay = post_delay
        self.last_timeout = None

    async def post(self, _url, **kwargs):
        self.last_timeout = kwargs.get("timeout")
        if self._post_delay:
            await asyncio.sleep(self._post_delay)
        return self._response


async def _hang(*_args, **_kwargs):
    """永不返回的读取，用于制造首块静默"""
    await asyncio.sleep(30)
    return b""


class TestFirstChunkBudget:
    """首块预算触发的故障转移"""

    @pytest.mark.asyncio
    async def test_post超出预算时判定首块超时(self):
        """连接建立/等响应头超过预算 → 返回首块超时错误，交由上层切换模型"""
        model_config = _model_config(first_chunk_timeout=0.1)
        session = _FakeSession(post_delay=1.0)
        manager = _manager(model_config, session)

        success, result = await manager.call_model_stream(model_config, {"messages": []})

        assert success is False
        assert result.category == ErrorCategory.FIRST_CHUNK_TIMEOUT
        # 首块超时不应在同一个模型上继续重试
        assert result.is_retryable is False
        assert result.should_disable_model is False

    @pytest.mark.asyncio
    async def test_响应头返回但首块静默时判定首块超时(self):
        """响应头已到、首块迟迟不来 → 预算耗尽后判定首块超时"""
        model_config = _model_config(first_chunk_timeout=0.1)
        response = _FakeResponse(_FakeContent(_hang))
        manager = _manager(model_config, _FakeSession(response=response))

        success, result = await manager.call_model_stream(model_config, {"messages": []})

        assert success is False
        assert result.category == ErrorCategory.FIRST_CHUNK_TIMEOUT
        assert response.closed is True

    @pytest.mark.asyncio
    async def test_仅心跳时探测窗口被预算截断(self):
        """上游只回 keep-alive 注释 → 探测窗口不超过预算，到期按首块超时处理"""
        model_config = _model_config(first_chunk_timeout=0.15, stream_probe_timeout=30)
        response = _FakeResponse(_FakeContent(b": keep-alive\n\n"))
        manager = _manager(model_config, _FakeSession(response=response))

        success, result = await manager.call_model_stream(model_config, {"messages": []})

        assert success is False
        assert result.category == ErrorCategory.FIRST_CHUNK_TIMEOUT

    @pytest.mark.asyncio
    async def test_首块正常时可以照常成功(self):
        """首块在预算内到达 → 成功返回，且回放已缓冲数据"""
        model_config = _model_config(first_chunk_timeout=5)
        sse = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        response = _FakeResponse(_FakeContent(sse, chunks=[b"data: [DONE]\n\n"]))
        manager = _manager(model_config, _FakeSession(response=response))

        success, stream = await manager.call_model_stream(model_config, {"messages": []})

        assert success is True
        chunks = [chunk async for chunk in stream]
        assert sse in chunks
        assert b"data: [DONE]\n\n" in chunks

    @pytest.mark.asyncio
    async def test_连接超时独立于总超时(self):
        """connect/sock_connect 用短连接超时，total 不设限，sock_read 用总超时"""
        model_config = _model_config(timeout=120, connect_timeout=3)
        sse = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        response = _FakeResponse(_FakeContent(sse))
        session = _FakeSession(response=response)
        manager = _manager(model_config, session)

        await manager.call_model_stream(model_config, {"messages": []})

        assert session.last_timeout.connect == 3
        assert session.last_timeout.sock_connect == 3
        assert session.last_timeout.total is None
        assert session.last_timeout.sock_read == 120


class TestTimeoutResolution:
    """超时取值优先级"""

    @pytest.mark.unit
    def test_首块预算优先模型配置且不超过总超时(self):
        manager = ModelFailoverManager(models={})
        assert manager._get_first_chunk_timeout(_model_config(timeout=120)) == manager.first_chunk_timeout
        assert manager._get_first_chunk_timeout(_model_config(first_chunk_timeout=8)) == 8
        # 预算不得大于模型总超时
        assert manager._get_first_chunk_timeout(_model_config(timeout=5, first_chunk_timeout=30)) == 5

    @pytest.mark.unit
    def test_连接超时优先模型配置且不超过总超时(self):
        manager = ModelFailoverManager(models={})
        assert manager._get_connect_timeout(_model_config(timeout=120)) == manager.connect_timeout
        assert manager._get_connect_timeout(_model_config(connect_timeout=2)) == 2
        assert manager._get_connect_timeout(_model_config(timeout=1, connect_timeout=30)) == 1

    @pytest.mark.unit
    def test_读取首块硬超时保持5秒(self):
        """首块读取硬超时语义不变，仍是 5 秒"""
        assert READ_FIRST_CHUNK_TIMEOUT == 5.0
