"""审计与故障转移的集成单元测试

覆盖真实接线处（failover 只在三个调用点包一层 run_attempt）：
- 先失败后成功：同一 gateway_request_id 下落多条 attempt，attempt_seq 递增
- 同模型重试：retry_seq 同步递增，且不计入故障转移
- 切换模型：计数一次故障转移
- 未收尾的流 + 客户端断开：结局改写为 aborted
- 审计关闭时：转发行为与返回值完全不变
"""

import asyncio

import pytest

from openai_proxy.audit.config import AuditConfig
from openai_proxy.audit.manager import AuditManager
from openai_proxy.model.failover import ModelFailoverManager
from openai_proxy.models import ModelConfig


def _model_config(**overrides) -> ModelConfig:
    """构造测试用模型配置（示例平台/示例模型，不使用任何真实上游）"""
    params = dict(
        name="example-platform|model-a",
        api_key="EXAMPLE_PLATFORM_API_KEY",
        base_url="https://api.example.com/v1",
        model="model-a",
        timeout=30,
        retry_count=1,
    )
    params.update(overrides)
    return ModelConfig(**params)


class _FakeResponse:
    """模拟 aiohttp 响应：非流式路径用 async with 取用"""

    def __init__(self, status: int, payload: dict = None, error_text: str = "upstream error"):
        self.status = status
        self._payload = payload
        self._error_text = error_text
        self.closed = False

    async def json(self):
        return self._payload

    async def text(self):
        return self._error_text

    def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.close()


class _FakeNonStreamSession:
    """模拟 aiohttp 会话：按调用顺序返回预设响应"""

    closed = False

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, **_kwargs):
        self.calls.append(url)
        return self._responses.pop(0)

    async def close(self):
        self.closed = True


class _StreamResponse(_FakeResponse):
    """流式测试用的响应外壳：状态 200 + 事件流内容"""

    def __init__(self, content):
        super().__init__(200)
        self.content = content
        self.headers = {"Content-Type": "text/event-stream"}


class _FakeContent:
    """模拟 aiohttp 响应内容：首块可控，其余块依次产出"""

    def __init__(self, first_read, chunks=None):
        self._first_read = first_read
        self._chunks = list(chunks or [])

    async def read(self, _size):
        return self._first_read

    async def iter_any(self):
        for chunk in self._chunks:
            yield chunk


class _FakeStreamSession:
    """模拟流式调用的 aiohttp 会话：post 可被 wait_for 等待"""

    closed = False

    def __init__(self, response):
        self._response = response

    async def post(self, _url, **_kwargs):
        return self._response


_OK_PAYLOAD = {
    "id": "chatcmpl-example",
    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 120, "completion_tokens": 350, "total_tokens": 470},
}


async def _audit(tmp_path, **overrides) -> AuditManager:
    """启动一个写入临时库的审计管理器（测试结束自动关闭）"""
    config = AuditConfig(
        db_path=str(tmp_path / "audit.db"),
        flush_interval=30,
        # 关闭 Prometheus，避免单元测试污染全局指标注册表
        prometheus_enabled=False,
        **overrides,
    )
    manager = AuditManager(config)
    await manager.start()
    return manager


async def _rows(manager: AuditManager):
    """强制刷盘后按尝试序号读回全部审计行"""
    await manager.collector._flush_once()
    return manager.store.query("SELECT * FROM audit_attempts ORDER BY gateway_request_id, attempt_seq")


class TestNonStreamFailover:
    """非流式故障转移的审计落库"""

    @pytest.mark.asyncio
    async def test_先失败后成功时落多条attempt(self, tmp_path):
        first = _model_config(name="example-platform|model-a", model="model-a")
        second = _model_config(name="example-platform|model-b", model="model-b")
        manager = ModelFailoverManager(models={"example-platform": [first, second]})
        manager.session = _FakeNonStreamSession([
            _FakeResponse(500, error_text="boom"),
            _FakeResponse(200, payload=_OK_PAYLOAD),
        ])

        audit = await _audit(tmp_path)
        try:
            ctx = audit.new_context(endpoint="chat_completions", selector="all")
            await manager.chat_completion_non_stream({"model": "all", "messages": [{"role": "user", "content": "hi"}]}, ctx)

            rows = await _rows(audit)
            assert len(rows) == 2
            # 同一次客户端请求贯穿整条故障转移链
            assert rows[0]["gateway_request_id"] == rows[1]["gateway_request_id"] == ctx.gateway_request_id
            assert [row["attempt_seq"] for row in rows] == [1, 2]
            assert [row["model_name"] for row in rows] == ["example-platform|model-a", "example-platform|model-b"]
            assert rows[0]["outcome"] == "failure"
            assert rows[1]["outcome"] == "success"
        finally:
            await audit.close()

    @pytest.mark.asyncio
    async def test_失败attempt带错误分类与状态码(self, tmp_path):
        model = _model_config()
        manager = ModelFailoverManager(models={"example-platform": [model]})
        manager.session = _FakeNonStreamSession([_FakeResponse(429, error_text="rate limited")])

        audit = await _audit(tmp_path)
        try:
            ctx = audit.new_context(endpoint="chat_completions", selector="all")
            with pytest.raises(Exception):
                await manager.chat_completion_non_stream({"model": "all", "messages": [{"role": "user", "content": "hi"}]}, ctx)

            rows = await _rows(audit)
            assert len(rows) == 1
            assert rows[0]["outcome"] == "failure"
            assert rows[0]["http_status"] == 429
            assert rows[0]["error_category"] == "http_429_rate_limit"
        finally:
            await audit.close()

    @pytest.mark.asyncio
    async def test_成功attempt记录token用量(self, tmp_path):
        model = _model_config()
        manager = ModelFailoverManager(models={"example-platform": [model]})
        manager.session = _FakeNonStreamSession([_FakeResponse(200, payload=_OK_PAYLOAD)])

        audit = await _audit(tmp_path)
        try:
            ctx = audit.new_context(endpoint="chat_completions", selector="all")
            await manager.chat_completion_non_stream({"model": "all", "messages": [{"role": "user", "content": "hi"}]}, ctx)

            rows = await _rows(audit)
            assert rows[0]["has_usage"] == 1
            assert rows[0]["input_tokens"] == 120
            assert rows[0]["output_tokens"] == 350
            assert rows[0]["duration_ms"] is not None
        finally:
            await audit.close()

    @pytest.mark.asyncio
    async def test_切换模型时计数一次故障转移(self, tmp_path):
        first = _model_config(name="example-platform|model-a", model="model-a")
        second = _model_config(name="example-platform|model-b", model="model-b")
        manager = ModelFailoverManager(models={"example-platform": [first, second]})
        manager.session = _FakeNonStreamSession([
            _FakeResponse(500, error_text="boom"),
            _FakeResponse(200, payload=_OK_PAYLOAD),
        ])

        audit = await _audit(tmp_path)
        try:
            ctx = audit.new_context(endpoint="chat_completions", selector="all")
            await manager.chat_completion_non_stream({"model": "all", "messages": [{"role": "user", "content": "hi"}]}, ctx)

            stats = audit.collector.stats()
            assert stats["failover_total"] == 1
            # 一次请求两条尝试：故障转移次数等于 attempts - requests
            rows = await _rows(audit)
            assert len(rows) == 2
        finally:
            await audit.close()

    @pytest.mark.asyncio
    async def test_重试同一个模型时attempt与retry序号同步递增(self, tmp_path, monkeypatch):
        """指定单模型选择器走带重试的调用路径：每次重试都落一条 attempt"""
        from openai_proxy.model.error_classifier import ErrorCategory, ErrorClassifier

        # 重试延迟置零，避免单元测试真的等 2 秒
        monkeypatch.setitem(
            ErrorClassifier.ERROR_STRATEGIES[ErrorCategory.HTTP_500_INTERNAL_ERROR],
            "retry_delay_seconds",
            0.0,
        )

        model = _model_config(retry_count=2)
        manager = ModelFailoverManager(models={"example-platform": [model]})
        manager.session = _FakeNonStreamSession([
            _FakeResponse(500, error_text="boom"),
            _FakeResponse(200, payload=_OK_PAYLOAD),
        ])

        audit = await _audit(tmp_path)
        try:
            ctx = audit.new_context(endpoint="chat_completions", selector="example-platform|model-a")
            await manager.chat_completion_non_stream(
                {"model": "example-platform|model-a", "messages": [{"role": "user", "content": "hi"}]}, ctx
            )

            rows = await _rows(audit)
            assert [row["attempt_seq"] for row in rows] == [1, 2]
            assert [row["retry_seq"] for row in rows] == [1, 2]
            assert {row["model_name"] for row in rows} == {"example-platform|model-a"}
            assert [row["outcome"] for row in rows] == ["failure", "success"]
            # 同一个模型上的重试不算故障转移
            assert audit.collector.stats()["failover_total"] == 0
        finally:
            await audit.close()


class TestStreamAudit:
    """流式调用的审计收尾"""

    @pytest.mark.asyncio
    async def test_流被客户端打断时结局记为aborted(self, tmp_path):
        model = _model_config(first_chunk_timeout=5)
        sse = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        manager = ModelFailoverManager(models={"example-platform": [model]})
        manager.session = _FakeStreamSession(_StreamResponse(_FakeContent(sse, chunks=[b"data: [DONE]\n\n"])))

        audit = await _audit(tmp_path)
        try:
            ctx = audit.new_context(endpoint="chat_completions", selector="all", stream=True)
            success, stream = await ctx.run_attempt(model, manager.call_model_stream(model, {"model": "all", "messages": []}))
            assert success is True

            # 只消费一块就停止迭代：等价于客户端提前断开
            async for _ in stream:
                break

            ctx.finalize_last({"has_usage": True, "ttft": 0.5, "total": 1.5, "output_tokens": 10,
                               "output_chars": 20, "throughput": 6.7}, aborted=True)
            rows = await _rows(audit)

            assert len(rows) == 1
            assert rows[0]["outcome"] == "aborted"
            # 未收尾的行不算成功，收尾后才进入统计
            assert rows[0]["finalized"] == 1
            assert rows[0]["output_tokens"] == 10
            assert rows[0]["probe_ms"] is not None
        finally:
            await audit.close()

    @pytest.mark.asyncio
    async def test_DONE已转发后客户端关闭按完成记账(self, tmp_path):
        """[DONE] 已经转发出去 = 内容发完，随后客户端关连接不算客户端中断"""
        model = _model_config(first_chunk_timeout=5)
        sse = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        manager = ModelFailoverManager(models={"example-platform": [model]})
        manager.session = _FakeStreamSession(_StreamResponse(_FakeContent(sse, chunks=[b"data: [DONE]\n\n"])))

        audit = await _audit(tmp_path)
        try:
            ctx = audit.new_context(endpoint="chat_completions", selector="all", stream=True)
            _success, stream = await ctx.run_attempt(model, manager.call_model_stream(model, {"model": "all", "messages": []}))
            async for _ in stream:
                break

            ctx.finalize_last({"has_usage": True, "ttft": 0.5, "total": 1.5, "output_tokens": 10,
                               "output_chars": 20, "throughput": 6.7}, aborted=True, done_sent=True)
            rows = await _rows(audit)

            assert rows[0]["outcome"] == "success"
            assert rows[0]["done_sent"] == 1
            assert rows[0]["finalized"] == 1
        finally:
            await audit.close()

    @pytest.mark.asyncio
    async def test_流正常结束时结局记为success(self, tmp_path):
        model = _model_config(first_chunk_timeout=5)
        sse = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        manager = ModelFailoverManager(models={"example-platform": [model]})
        manager.session = _FakeStreamSession(_StreamResponse(_FakeContent(sse, chunks=[b"data: [DONE]\n\n"])))

        audit = await _audit(tmp_path)
        try:
            ctx = audit.new_context(endpoint="chat_completions", selector="all", stream=True)
            _success, stream = await ctx.run_attempt(model, manager.call_model_stream(model, {"model": "all", "messages": []}))
            async for _ in stream:
                pass

            ctx.finalize_last({"has_usage": True, "ttft": 0.2, "total": 2.0, "output_tokens": 40,
                               "output_chars": 80, "throughput": 20.0})
            rows = await _rows(audit)

            assert rows[0]["outcome"] == "success"
            assert rows[0]["ttft_ms"] == 200
            assert rows[0]["duration_ms"] == 2000
        finally:
            await audit.close()

    @pytest.mark.asyncio
    async def test_收尾前落一条未计入成功率的记录(self, tmp_path):
        """流刚交给上层时先落 stream_open，中途进程被杀也不会丢失这次调用"""
        model = _model_config(first_chunk_timeout=5)
        sse = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        manager = ModelFailoverManager(models={"example-platform": [model]})
        manager.session = _FakeStreamSession(_StreamResponse(_FakeContent(sse, chunks=[b"data: [DONE]\n\n"])))

        audit = await _audit(tmp_path)
        try:
            ctx = audit.new_context(endpoint="chat_completions", selector="all", stream=True)
            _success, _stream = await ctx.run_attempt(model, manager.call_model_stream(model, {"model": "all", "messages": []}))
            rows = await _rows(audit)

            assert rows[0]["outcome"] == "stream_open"
            assert rows[0]["finalized"] == 0
        finally:
            await audit.close()


class TestAuditDisabled:
    """审计关闭时不得影响转发"""

    @pytest.mark.asyncio
    async def test_关闭审计时不建库且转发结果不变(self, tmp_path):
        model = _model_config()
        manager = ModelFailoverManager(models={"example-platform": [model]})
        manager.session = _FakeNonStreamSession([_FakeResponse(200, payload=_OK_PAYLOAD)])

        audit = await _audit(tmp_path, enabled=False)
        try:
            ctx = audit.new_context(endpoint="chat_completions", selector="all")
            result = await manager.chat_completion_non_stream(
                {"model": "all", "messages": [{"role": "user", "content": "hi"}]}, ctx
            )

            assert result["choices"][0]["message"]["content"] == "ok"
            assert audit.collector is None
            assert audit.store.stats()["rows"] == 0
        finally:
            await audit.close()

    @pytest.mark.asyncio
    async def test_审计上下文为none时failover照常工作(self, tmp_path):
        model = _model_config()
        manager = ModelFailoverManager(models={"example-platform": [model]})
        manager.session = _FakeNonStreamSession([_FakeResponse(200, payload=_OK_PAYLOAD)])

        result = await manager.chat_completion_non_stream(
            {"model": "all", "messages": [{"role": "user", "content": "hi"}]}, None
        )

        assert result["usage"]["total_tokens"] == 470
        await manager.close()
        await asyncio.sleep(0)
