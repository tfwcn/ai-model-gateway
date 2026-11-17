"""故障转移失败信息传递测试

确保「关键错误」能到达下游：
- 多平台/多模型全部失败时，保留最早的根因（不被后面的「无可用模型」覆盖）；
- 上游原始 message/code/param 原样透传；
- 全部模型被周期禁用时，带上最近一次失败原因。
"""

import json

import pytest

from openai_proxy.core.error_envelope import GatewayFailoverError
from openai_proxy.model.error_classifier import ErrorClassifier
from openai_proxy.model.failover import ModelFailoverManager
from openai_proxy.models import ModelConfig

_IMAGE_ERROR_BODY = json.dumps({
    "error": {
        "message": "Failed to download image from https://api.example.com/img.png?example=1",
        "type": "invalid_request_error",
        "param": "messages[0].content",
        "code": "invalid_image",
    }
})

_REQUEST = {"model": "all", "messages": [{"role": "user", "content": "hi"}]}


def _model_config(name: str, model: str, **overrides) -> ModelConfig:
    """构造测试用模型配置（示例平台/示例模型，不使用任何真实上游）"""
    params = dict(
        name=name,
        api_key="EXAMPLE_PLATFORM_API_KEY",
        base_url="https://api.example.com/v1",
        model=model,
        timeout=30,
        retry_count=1,
    )
    params.update(overrides)
    return ModelConfig(**params)


class _FakeResponse:
    """模拟 aiohttp 非流式响应"""

    def __init__(self, status: int, body: str = "", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def json(self):
        return json.loads(self._body)

    async def text(self):
        return self._body

    def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        pass


class _FakeContent:
    """模拟流式响应体：首块返回错误 JSON"""

    def __init__(self, body: str):
        self._body = body.encode("utf-8")
        self._read = False

    async def read(self, _size: int) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._body

    async def iter_any(self):
        if False:  # pragma: no cover - 仅用于满足接口
            yield b""


class _FakeStreamErrorResponse:
    """模拟状态码非 200 的流式响应"""

    status = 400

    def __init__(self, body: str):
        self.headers = {"Content-Type": "application/json"}
        self.content = _FakeContent(body)

    def close(self):
        pass


class _FakeSession:
    """按调用顺序返回预设响应的 aiohttp 会话（非流式：post 返回异步上下文管理器）"""

    closed = False

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, **_kwargs):
        self.calls.append(url)
        return self._responses.pop(0)

    async def close(self):
        self.closed = True


class _FakeAsyncSession(_FakeSession):
    """流式调用需要 post 本身可 await"""

    async def post(self, url, **_kwargs):
        self.calls.append(url)
        return self._responses.pop(0)


async def _fail_result(manager: ModelFailoverManager, payload=None):
    """执行一次非流式请求并返回抛出的故障转移异常"""
    with pytest.raises(GatewayFailoverError) as excinfo:
        await manager.chat_completion_non_stream(dict(payload or _REQUEST))
    return excinfo.value


class TestNonStreamErrorPropagation:
    @pytest.mark.asyncio
    async def test_跨平台保留最早的根因(self):
        first = _model_config(name="example-platform|model-a", model="model-a")
        second = _model_config(name="example-platform-b|model-b", model="model-b")
        manager = ModelFailoverManager(models={
            "example-platform": [first],
            "example-platform-b": [second],
        })
        manager.session = _FakeSession([
            _FakeResponse(400, body=_IMAGE_ERROR_BODY),
            _FakeResponse(503, body=json.dumps({"error": {"message": "service unavailable"}})),
        ])

        error = await _fail_result(manager)
        body = error.envelope.body()["error"]

        # 根因是第一个平台的 400，而不是最后的 503
        assert error.envelope.http_status == 400
        assert body["message"] == "Failed to download image from https://api.example.com/img.png?example=1"
        assert body["type"] == "invalid_request_error"
        assert body["param"] == "messages[0].content"
        assert body["code"] == "invalid_image"
        # 尝试次数只暴露状态码和分类
        assert body["attempts"] == [
            {"status": 400, "category": "http_400_bad_request"},
            {"status": 503, "category": "http_503_service_unavailable"},
        ]

    @pytest.mark.asyncio
    async def test_同平台内保留第一条错误(self):
        first = _model_config(name="example-platform|model-a", model="model-a")
        second = _model_config(name="example-platform|model-b", model="model-b")
        manager = ModelFailoverManager(models={"example-platform": [first, second]})
        manager.session = _FakeSession([
            _FakeResponse(400, body=_IMAGE_ERROR_BODY),
            _FakeResponse(500, body=json.dumps({"error": {"message": "boom"}})),
        ])

        error = await _fail_result(manager)

        assert error.envelope.http_status == 400
        assert "Failed to download image" in error.envelope.public_message

    @pytest.mark.asyncio
    async def test_上游429带retry_after(self):
        model = _model_config(name="example-platform|model-a", model="model-a")
        manager = ModelFailoverManager(models={"example-platform": [model]})
        manager.session = _FakeSession([
            _FakeResponse(
                429,
                body=json.dumps({"error": {"message": "slow down", "type": "rate_limit_error"}}),
                headers={"Retry-After": "7"},
            ),
        ])

        error = await _fail_result(manager)

        assert error.status_code == 429
        assert error.envelope.headers == {"Retry-After": "7"}
        assert error.envelope.error_type == "rate_limit_error"

    @pytest.mark.asyncio
    async def test_全部禁用时带出最近失败原因(self):
        model = _model_config(
            name="example-platform|model-a", model="model-a", quota_period="daily"
        )
        manager = ModelFailoverManager(models={"example-platform": [model]})

        classified = ErrorClassifier.classify_http_error(
            401, json.dumps({"error": {"message": "invalid api key"}}), model.name
        )
        await manager.model_state_manager.disable_model_for_period(model, classified)

        error = await _fail_result(manager)

        assert error.envelope.http_status == 503
        assert "没有可用模型" in error.envelope.public_message
        assert "invalid api key" in error.envelope.public_message

    @pytest.mark.asyncio
    async def test_缺少messages返回400(self):
        model = _model_config(name="example-platform|model-a", model="model-a")
        manager = ModelFailoverManager(models={"example-platform": [model]})

        error = await _fail_result(manager, {"model": "all"})

        assert error.envelope.http_status == 400
        assert error.envelope.error_type == "invalid_request_error"

    @pytest.mark.asyncio
    async def test_没有配置模型返回503(self):
        manager = ModelFailoverManager(models={})

        error = await _fail_result(manager)

        assert error.envelope.http_status == 503
        assert error.envelope.error_code == "no_available_model"


class TestStreamErrorPropagation:
    @pytest.mark.asyncio
    async def test_流式非200保留上游原文(self):
        model = _model_config(name="example-platform|model-a", model="model-a")
        manager = ModelFailoverManager(models={"example-platform": [model]})
        manager.session = _FakeAsyncSession([_FakeStreamErrorResponse(_IMAGE_ERROR_BODY)])

        with pytest.raises(GatewayFailoverError) as excinfo:
            await manager.chat_completion_stream(dict(_REQUEST))

        error = excinfo.value
        assert error.envelope.http_status == 400
        assert "Failed to download image" in error.envelope.public_message