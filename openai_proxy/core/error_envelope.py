"""
OpenAI 兼容错误信封

背景：
- 故障转移会依次尝试多个模型，原来只把「最后一次失败」的文本拼进 detail，
  真实根因（第一个上游错误）经常被后面的「无可用模型」覆盖；
- 每个平台的错误语义都被压成 HTTP 500，下游无法区分限流、鉴权、超时；
- 流式链路在响应头发出后无法再改状态码，错误甚至以裸 JSON 下发。

本模块把「多次尝试的失败」收敛成一份下游可直接解析的错误体：
- 非流式：{"error": {...}} + 语义化 HTTP 状态码 + Retry-After
- 流式：data: {"error": {...}} + data: [DONE]
- Responses：event: error + data: {...}

约定：
- 对外只暴露「错误分类 + 状态码 + 上游原文」，不暴露平台名、模型名、base_url；
- 上游原文先脱敏再截断，避免把密钥片段带出去；
- 上游返回的 code/type 优先沿用，保持下游 SDK 的判断逻辑不变。
"""
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from fastapi import HTTPException
from fastapi.responses import JSONResponse

# 对外错误文本上限：既要保留上游关键信息，又不能把整段 HTML 回传
MAX_PUBLIC_MESSAGE_CHARS = 800

# 单次响应里最多列出多少条尝试记录，避免错误体无限膨胀
MAX_ATTEMPTS_IN_BODY = 8

# SSE 终止标记
SSE_DONE = b"data: [DONE]\n\n"


class ErrorSanitizer:
    """脱敏工具：把错误文本里可能出现的凭据替换掉"""

    # 常见凭据形态：sk-xxx / Bearer xxx / api_key=xxx / token: xxx
    _RULES = (
        (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "[REDACTED]"),
        (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"), "Bearer [REDACTED]"),
        (
            re.compile(r"(?i)((?:api[-_]?key|authorization|access[-_]?token|secret)\s*[=:]\s*)[^\s,;\"'}]+"),
            r"\1[REDACTED]",
        ),
    )

    @classmethod
    def clean(cls, text: Any, limit: int = MAX_PUBLIC_MESSAGE_CHARS) -> str:
        """脱敏并截断错误文本"""
        if text is None:
            return ""
        cleaned = str(text)
        for pattern, replacement in cls._RULES:
            cleaned = pattern.sub(replacement, cleaned)
        cleaned = cleaned.strip()
        if limit and len(cleaned) > limit:
            cleaned = cleaned[:limit].rstrip() + "…"
        return cleaned


class UpstreamErrorParser:
    """解析上游错误响应体，尽量复用上游自己的 message/code/type/param

    上游大多已经是 OpenAI 兼容形状，例如：

        {"error": {"message": "Failed to download image from ...",
                   "type": "invalid_request_error", "param": null, "code": null}}

    这类 message 本身就是给调用方看的，直接原样透传最友好；只有拿不到时才由
    网关用「HTTP 状态码 + 原始报文」拼一个兜底文本。
    """

    @classmethod
    def parse(cls, body: Any) -> Dict[str, Optional[str]]:
        """从上游响应体里提取 message/code/type/param，解析失败时全部返回 None"""
        result: Dict[str, Optional[str]] = {
            "message": None,
            "code": None,
            "type": None,
            "param": None,
        }
        if not body:
            return result

        data = body
        if isinstance(body, (str, bytes)):
            try:
                data = json.loads(body)
            except (ValueError, TypeError):
                return result
        if not isinstance(data, dict):
            return result

        error = data.get("error")
        if isinstance(error, str):
            result["message"] = error.strip() or None
            return result
        if isinstance(error, dict):
            for key in ("message", "code", "type", "param"):
                value = error.get(key)
                if isinstance(value, (str, int)) and str(value).strip():
                    result[key] = str(value).strip()
            return result
        # 少数上游把错误直接平铺在顶层
        if isinstance(data.get("message"), str) and data.get("message").strip():
            result["message"] = data["message"].strip()
        return result


@dataclass
class AttemptFailure:
    """单次尝试的失败画像（只保留对外可说的信息）"""

    category: str
    message: str = ""
    http_status: Optional[int] = None
    upstream_code: Optional[str] = None
    upstream_type: Optional[str] = None
    upstream_param: Optional[str] = None
    retry_after: Optional[float] = None
    stop_failover: bool = False


class ErrorEnvelope:
    """把若干次失败收敛成一份 OpenAI 兼容错误体"""

    # 错误分类 → (HTTP 状态码, OpenAI type)
    _STATUS_MAP: Dict[str, tuple] = {
        "http_400_bad_request": (400, "invalid_request_error"),
        "tool_call_id_error": (400, "invalid_request_error"),
        "http_429_rate_limit": (429, "rate_limit_error"),
        # 上游的鉴权/权限/模型不存在是网关侧的配置问题，不能透传 401，
        # 否则下游会误以为自己的密钥失效
        "http_401_unauthorized": (502, "api_error"),
        "http_403_forbidden": (502, "api_error"),
        "http_404_not_found": (502, "api_error"),
        "http_500_internal_error": (502, "api_error"),
        "http_502_bad_gateway": (502, "api_error"),
        "http_503_service_unavailable": (502, "api_error"),
        "http_504_gateway_timeout": (504, "api_error"),
        "network_timeout": (504, "api_error"),
        "first_chunk_timeout": (504, "api_error"),
        "network_connection_error": (502, "api_error"),
        "network_dns_error": (502, "api_error"),
        "invalid_response_format": (502, "api_error"),
        "missing_content": (502, "api_error"),
        "invalid_model": (502, "api_error"),
        "free_tier_blocked": (503, "api_error"),
        "no_available_model": (503, "api_error"),
        "unknown_error": (500, "api_error"),
    }

    # 没有上游 code 时按状态码给一个稳定 code
    _DEFAULT_CODES: Dict[int, str] = {
        400: "invalid_request",
        429: "rate_limit_exceeded",
        500: "internal_error",
        502: "upstream_error",
        503: "upstream_unavailable",
        504: "upstream_timeout",
    }

    # 上游返回这些标准 OpenAI type 时原样沿用，避免下游判断逻辑变化
    _STANDARD_TYPES = {
        "invalid_request_error",
        "authentication_error",
        "permission_error",
        "not_found_error",
        "rate_limit_error",
        "server_error",
        "api_error",
    }

    # 主错误优先级：0 最高（值越小越优先）
    _RANK_STOP_FAILOVER = 0
    _RANK_CLIENT_ERROR = 1
    _RANK_UPSTREAM_ERROR = 2
    _RANK_NO_MODEL = 3

    def __init__(
        self,
        failures: Sequence[AttemptFailure],
        request_id: Optional[str] = None,
        selector: Optional[str] = None,
    ):
        self.failures: List[AttemptFailure] = list(failures)
        self.request_id = request_id
        self.selector = selector

    # ---------- 构造入口 ----------

    @classmethod
    def from_exception(cls, exc: BaseException) -> "ErrorEnvelope":
        """把任意异常转成错误信封：Agent 自己抛的沿用原信封，其余归为未知错误"""
        if isinstance(exc, GatewayFailoverError):
            return exc.envelope
        return cls(
            [
                AttemptFailure(
                    category="unknown_error",
                    message=ErrorSanitizer.clean(f"网关内部错误: {exc}"),
                    http_status=500,
                )
            ]
        )

    @classmethod
    def client_error(
        cls,
        message: str,
        status: int = 400,
        code: Optional[str] = None,
        category: str = "http_400_bad_request",
    ) -> "ErrorEnvelope":
        """构造「请求本身不合法」类的错误信封"""
        return cls(
            [
                AttemptFailure(
                    category=category,
                    message=ErrorSanitizer.clean(message),
                    http_status=status,
                    upstream_code=code,
                )
            ]
        )

    # ---------- 主错误与状态映射 ----------

    @property
    def primary(self) -> AttemptFailure:
        """选出最能解释本次失败的那一条（同优先级取最早出现的一条）"""
        if not self.failures:
            return AttemptFailure(
                category="unknown_error",
                message="网关未捕获到具体的上游错误",
                http_status=500,
            )
        return min(self.failures, key=self._rank)

    def _rank(self, failure: AttemptFailure) -> int:
        """错误优先级：请求自身错误 > 上游错误 > 无可用模型"""
        if failure.stop_failover:
            return self._RANK_STOP_FAILOVER
        if failure.http_status == 400 or failure.category in ("http_400_bad_request", "tool_call_id_error"):
            return self._RANK_CLIENT_ERROR
        if failure.category == "no_available_model":
            return self._RANK_NO_MODEL
        return self._RANK_UPSTREAM_ERROR

    @property
    def http_status(self) -> int:
        mapped = self._STATUS_MAP.get(self.primary.category)
        if mapped is not None:
            return int(mapped[0])
        # 分类未知时按上游原始状态码兜底
        status = self.primary.http_status
        if status == 429:
            return 429
        if status is not None and 400 <= int(status) < 500:
            return 400 if int(status) == 400 else 502
        return 500

    @property
    def error_type(self) -> str:
        # 上游给的是标准 OpenAI 类型时原样沿用，保证下游判断逻辑不变
        upstream_type = (self.primary.upstream_type or "").strip()
        if upstream_type in self._STANDARD_TYPES:
            return upstream_type
        mapped = self._STATUS_MAP.get(self.primary.category)
        if mapped is not None:
            return str(mapped[1])
        return "invalid_request_error" if self.http_status == 400 else "api_error"

    @property
    def error_code(self) -> str:
        # 优先沿用上游 code（如 context_length_exceeded / insufficient_quota）
        if self.primary.upstream_code:
            return self.primary.upstream_code
        return self._DEFAULT_CODES.get(self.http_status, "internal_error")

    @property
    def public_message(self) -> str:
        message = self.primary.message or "上游服务暂时不可用"
        return ErrorSanitizer.clean(message)

    @property
    def retry_after_seconds(self) -> Optional[int]:
        """Retry-After：优先上游给的，其次按状态码给保守值"""
        if self.primary.retry_after is not None:
            return max(1, int(math.ceil(float(self.primary.retry_after))))
        if self.http_status == 429:
            return 10
        return None

    @property
    def headers(self) -> Dict[str, str]:
        retry_after = self.retry_after_seconds
        if retry_after is None:
            return {}
        return {"Retry-After": str(retry_after)}

    # ---------- 响应体 ----------

    def _attempts(self) -> List[Dict[str, Any]]:
        """只暴露状态码和错误分类，不泄露平台名/模型名"""
        attempts: List[Dict[str, Any]] = []
        seen = set()
        for failure in self.failures:
            key = (failure.http_status, failure.category)
            if key in seen:
                continue
            seen.add(key)
            attempts.append({"status": failure.http_status, "category": failure.category})
            if len(attempts) >= MAX_ATTEMPTS_IN_BODY:
                break
        return attempts

    def body(self, include_attempts: bool = True) -> Dict[str, Any]:
        """构造 OpenAI 规范的错误响应体"""
        error: Dict[str, Any] = {
            "message": self.public_message,
            "type": self.error_type,
            # 上游指明了出错字段时原样带出，下游可以据此定位参数
            "param": self.primary.upstream_param,
            "code": self.error_code,
        }
        if self.request_id:
            error["request_id"] = self.request_id
        if include_attempts:
            attempts = self._attempts()
            if len(self.failures) > 1 and attempts:
                error["attempts"] = attempts
        return {"error": error}

    # ---------- 流式事件 ----------

    def chat_sse(self) -> bytes:
        """Chat Completions 流式错误事件（标准 data: 行）"""
        payload = json.dumps(self.body(), ensure_ascii=False)
        return f"data: {payload}\n\n".encode("utf-8")

    def responses_event(self) -> bytes:
        """Responses API 流式错误事件（event: error）"""
        payload = json.dumps(
            {
                "type": "error",
                "code": self.error_code,
                "message": self.public_message,
                "param": self.primary.upstream_param,
            },
            ensure_ascii=False,
        )
        return f"event: error\ndata: {payload}\n\n".encode("utf-8")


class GatewayFailoverError(HTTPException):
    """携带错误信封的故障转移异常：由统一的异常处理器还原成兼容响应"""

    def __init__(self, envelope: ErrorEnvelope):
        self.envelope = envelope
        super().__init__(
            status_code=envelope.http_status,
            detail=envelope.public_message,
            headers=envelope.headers or None,
        )

    def to_response(self) -> JSONResponse:
        """直接构造响应（流式快速失败窗口里提前返回时用）"""
        return JSONResponse(
            status_code=self.envelope.http_status,
            content=self.envelope.body(),
            headers=self.envelope.headers or None,
        )