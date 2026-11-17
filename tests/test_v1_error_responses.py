"""数据面错误响应的端到端形状测试

验证 /v1 接口最终返回给下游的内容：
- 非流式：OpenAI 兼容 error 信封 + 真实状态码；
- 流式：快速失败窗口内返回真实状态码；窗口外以标准 SSE / Responses 事件通报。
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from openai_proxy.adapter.responses import ResponsesAdapter
from openai_proxy.core.error_envelope import AttemptFailure, ErrorEnvelope, GatewayFailoverError
from openai_proxy.service import OpenAIProxyService
from openai_proxy.utils.stream_start_gate import StreamStartGate

_KEY = "test-gateway-key"
_HEADERS = {"Authorization": f"Bearer {_KEY}"}
_CHAT_PAYLOAD = {"model": "example-platform|model-a", "messages": [{"role": "user", "content": "hi"}]}


def _envelope(status: int = 429, category: str = "http_429_rate_limit") -> ErrorEnvelope:
    return ErrorEnvelope([AttemptFailure(category=category, http_status=status, message="slow down")])


class _StubFailover:
    """替换真实 failover：按需立刻失败或延迟失败"""

    def __init__(self, error: Exception, delay: float = 0.0):
        self.error = error
        self.delay = delay

    async def chat_completion_non_stream(self, _payload, _audit_ctx=None):
        raise self.error

    async def chat_completion_stream(self, _payload, _audit_ctx=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        raise self.error


def _client(tmp_path, monkeypatch, failover: _StubFailover, grace: float) -> TestClient:
    monkeypatch.setenv("GATEWAY_API_KEY", _KEY)
    service = OpenAIProxyService(config_file=str(tmp_path / "models.yaml"))
    service.failover_manager = failover  # type: ignore[assignment]
    service.responses_adapter = ResponsesAdapter()
    service.stream_start_gate = StreamStartGate(grace=grace)
    return TestClient(service.create_app())


class TestNonStreamErrorResponse:
    def test_返回兼容错误体与真实状态码(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch, _StubFailover(GatewayFailoverError(_envelope())), grace=3)

        response = client.post("/v1/chat/completions", json=_CHAT_PAYLOAD, headers=_HEADERS)

        assert response.status_code == 429
        body = response.json()
        assert set(body.keys()) == {"error"}
        assert body["error"]["type"] == "rate_limit_error"
        assert body["error"]["message"] == "slow down"


class TestStreamFastFailWindow:
    def test_窗口内失败返回真实状态码(self, tmp_path, monkeypatch):
        error = GatewayFailoverError(_envelope(400, "http_400_bad_request"))
        client = _client(tmp_path, monkeypatch, _StubFailover(error), grace=3)

        response = client.post(
            "/v1/chat/completions",
            json={**_CHAT_PAYLOAD, "stream": True},
            headers=_HEADERS,
        )

        assert response.status_code == 400
        assert response.json()["error"]["type"] == "invalid_request_error"

    def test_窗口外失败以SSE事件下发(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch, _StubFailover(GatewayFailoverError(_envelope()), delay=0.05), grace=0.01)

        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={**_CHAT_PAYLOAD, "stream": True},
            headers=_HEADERS,
        ) as response:
            text = "".join(response.iter_text())

        assert response.status_code == 200
        assert "data: " in text
        assert '"type": "rate_limit_error"' in text
        assert "data: [DONE]" in text

    def test_Responses窗口外失败以error事件下发(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch, _StubFailover(GatewayFailoverError(_envelope()), delay=0.05), grace=0.01)

        with client.stream(
            "POST",
            "/v1/responses",
            json={"model": "example-platform|model-a", "input": "hi", "stream": True},
            headers=_HEADERS,
        ) as response:
            text = "".join(response.iter_text())

        assert response.status_code == 200
        assert "event: error" in text
        assert "data: [DONE]" in text