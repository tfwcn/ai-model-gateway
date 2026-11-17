"""错误信封单元测试

覆盖对外错误契约：
- 上游原始 message/type/code/param 尽量原样透传（例如图片下载失败这类长文案）；
- HTTP 状态码按错误语义映射（429 透传、上游 401 收敛成 502）；
- attempts 只暴露状态码和分类，不泄露平台名/模型名；
- 流式错误使用标准 SSE / Responses 事件。
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openai_proxy.core.api_errors import install_error_handlers
from openai_proxy.core.error_envelope import (
    AttemptFailure,
    ErrorEnvelope,
    ErrorSanitizer,
    GatewayFailoverError,
    MAX_PUBLIC_MESSAGE_CHARS,
    UpstreamErrorParser,
)


def _failure(category: str, status=None, message: str = "", **kwargs) -> AttemptFailure:
    """构造一条失败记录（测试用）"""
    return AttemptFailure(category=category, http_status=status, message=message, **kwargs)


class TestUpstreamErrorParser:
    def test_解析标准错误体(self):
        body = json.dumps({
            "error": {
                "message": "Failed to download image from https://api.example.com/img.png",
                "type": "invalid_request_error",
                "param": "messages[0].content",
                "code": "invalid_image",
            }
        })
        parsed = UpstreamErrorParser.parse(body)
        assert parsed["message"] == "Failed to download image from https://api.example.com/img.png"
        assert parsed["type"] == "invalid_request_error"
        assert parsed["param"] == "messages[0].content"
        assert parsed["code"] == "invalid_image"

    def test_无法解析时全部为空(self):
        parsed = UpstreamErrorParser.parse("Bad Gateway")
        assert parsed == {"message": None, "code": None, "type": None, "param": None}


class TestStatusMapping:
    def test_上游400保留原始文案和类型(self):
        message = "Failed to download image from https://api.example.com/img.png?example=1"
        envelope = ErrorEnvelope([
            _failure(
                "http_400_bad_request", 400, message,
                upstream_type="invalid_request_error",
                upstream_param="messages[0].content",
                upstream_code="invalid_image",
            )
        ])
        body = envelope.body()["error"]

        assert envelope.http_status == 400
        # 不再拼「HTTP 400: 模型 xxx 返回错误」
        assert body["message"] == message
        assert body["type"] == "invalid_request_error"
        assert body["param"] == "messages[0].content"
        assert body["code"] == "invalid_image"

    def test_上游429透传并带retry_after(self):
        envelope = ErrorEnvelope([_failure("http_429_rate_limit", 429, "slow down", retry_after=7)])
        assert envelope.http_status == 429
        assert envelope.error_type == "rate_limit_error"
        assert envelope.headers == {"Retry-After": "7"}

    def test_上游401收敛成502(self):
        # 不能把上游 401 透传给下游，否则下游会以为自己密钥失效
        envelope = ErrorEnvelope([_failure("http_401_unauthorized", 401, "invalid api key")])
        assert envelope.http_status == 502
        assert envelope.error_type == "api_error"

    def test_首块超时映射成504(self):
        envelope = ErrorEnvelope([_failure("first_chunk_timeout", None, "上游在 20 秒内未返回首个数据块")])
        assert envelope.http_status == 504
        assert envelope.error_code == "upstream_timeout"

    def test_全部禁用映射成503(self):
        envelope = ErrorEnvelope([_failure("no_available_model", None, "当前没有可用模型")])
        assert envelope.http_status == 503
        assert envelope.error_code == "upstream_unavailable"


class TestPrimarySelection:
    def test_保留最早的真实根因(self):
        # 第一个平台 429（根因），后面平台都变成「无可用模型」
        envelope = ErrorEnvelope([
            _failure("http_429_rate_limit", 429, "rate limit exceeded"),
            _failure("no_available_model", None, "当前没有可用模型"),
        ])
        assert envelope.public_message == "rate limit exceeded"
        assert envelope.http_status == 429

    def test_请求自身错误优先于上游错误(self):
        envelope = ErrorEnvelope([
            _failure("http_503_service_unavailable", 503, "service unavailable"),
            _failure("http_400_bad_request", 400, "bad request"),
        ])
        assert envelope.http_status == 400
        assert envelope.public_message == "bad request"

    def test_没有失败记录时给兜底(self):
        envelope = ErrorEnvelope([])
        assert envelope.http_status == 500
        assert envelope.public_message


class TestAttempts:
    def test_attempts只包含状态码和分类(self):
        envelope = ErrorEnvelope([
            _failure("http_429_rate_limit", 429, "rate limit"),
            _failure("http_503_service_unavailable", 503, "unavailable"),
            _failure("http_429_rate_limit", 429, "rate limit"),
        ])
        attempts = envelope.body()["error"]["attempts"]

        assert attempts == [
            {"status": 429, "category": "http_429_rate_limit"},
            {"status": 503, "category": "http_503_service_unavailable"},
        ]
        # 不泄露平台名、模型名
        assert "provider" not in attempts[0]
        assert "model" not in attempts[0]

    def test_单次尝试不输出attempts(self):
        envelope = ErrorEnvelope([_failure("http_502_bad_gateway", 502, "bad gateway")])
        assert "attempts" not in envelope.body()["error"]


class TestSanitizer:
    def test_脱敏常见凭据(self):
        # 密钥样例运行时拼接，避免源码里出现完整凭据形态的字符串
        fake_key = "sk-" + "example" + "1234567890"
        cleaned = ErrorSanitizer.clean(
            f"Authorization: Bearer abc.def.ghi api_key=EXAMPLE_PLATFORM_API_KEY key {fake_key}"
        )
        assert "abc.def.ghi" not in cleaned
        assert "EXAMPLE_PLATFORM_API_KEY" not in cleaned
        assert fake_key not in cleaned
        assert "[REDACTED]" in cleaned

    def test_长文本截断但保留前缀(self):
        message = "Failed to download image from " + "https://api.example.com/" + "a" * 2000
        envelope = ErrorEnvelope([_failure("http_400_bad_request", 400, message)])
        public = envelope.public_message

        assert public.startswith("Failed to download image from")
        assert len(public) <= MAX_PUBLIC_MESSAGE_CHARS + 1  # 末尾可能补一个省略号


class TestStreamEvents:
    def test_chat_sse是标准data行(self):
        envelope = ErrorEnvelope([_failure("http_429_rate_limit", 429, "slow down")])
        raw = envelope.chat_sse().decode("utf-8")

        assert raw.startswith("data: ")
        assert raw.endswith("\n\n")
        payload = json.loads(raw[len("data: "):].strip())
        assert payload["error"]["type"] == "rate_limit_error"

    def test_responses_event使用error事件(self):
        envelope = ErrorEnvelope([_failure("network_timeout", None, "上游请求超时（阈值 30 秒）")])
        raw = envelope.responses_event().decode("utf-8")

        assert raw.startswith("event: error\ndata: ")
        payload = json.loads(raw.split("data: ", 1)[1].strip())
        assert payload["type"] == "error"
        assert payload["code"] == "upstream_timeout"


class TestGatewayFailoverError:
    def test_异常处理器返回兼容错误体(self):
        app = FastAPI()
        install_error_handlers(app)
        envelope = ErrorEnvelope([_failure("http_429_rate_limit", 429, "slow down", retry_after=5)])

        @app.post("/v1/chat/completions")
        async def _raise():
            raise GatewayFailoverError(envelope)

        response = TestClient(app).post("/v1/chat/completions", json={})

        assert response.status_code == 429
        assert response.headers["retry-after"] == "5"
        body = response.json()
        assert set(body.keys()) == {"error"}
        assert body["error"]["type"] == "rate_limit_error"
        assert body["error"]["message"] == "slow down"

    def test_客户端错误构造函数(self):
        envelope = ErrorEnvelope.client_error("messages参数是必需的")
        assert envelope.http_status == 400
        assert envelope.error_type == "invalid_request_error"
        assert envelope.public_message == "messages参数是必需的"