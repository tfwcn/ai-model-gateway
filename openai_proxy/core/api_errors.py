"""
统一错误响应 - 只作用于非转发接口

约定：
- 转发协议接口（/v1/*、/proxy/*）保持原有形状不动，避免影响 OpenAI 兼容客户端与透传链路；
- 其余接口（/admin/*、/metrics、/health 等）一律返回：

      {"error": {"code": "user_exists", "message": "...", "details": {...}?}}

  HTTP 状态码仍然保留，用来区分大类；code 用来给调用方做程序判断。
- details 只在需要回传结构化信息时出现（校验错误列表、版本冲突的 expected/actual 等）。
"""
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# 转发协议前缀：这些接口的错误形状保持不变
FORWARD_PREFIXES = ("/v1", "/proxy")

# 没有显式指定 code 时，按状态码给一个通用 code
_DEFAULT_CODES: Dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}

_DEFAULT_MESSAGES: Dict[int, str] = {
    400: "请求参数不正确",
    401: "密钥缺失或不正确",
    403: "没有访问权限",
    404: "资源不存在",
    405: "请求方法不允许",
    409: "请求与当前状态冲突",
    422: "请求参数校验失败",
    429: "请求过于频繁",
    500: "服务内部错误",
    503: "服务暂时不可用",
}


class ApiError(HTTPException):
    """带机器可读 code 的接口异常（只在非转发接口使用）"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ):
        self.code = code
        self.message = message
        self.details = details
        super().__init__(status_code=status_code, detail=message, headers=headers)


def is_forward_path(path: str) -> bool:
    """是否属于转发协议接口（这些接口的错误形状保持原样）"""
    return any(path == prefix or path.startswith(prefix + "/") for prefix in FORWARD_PREFIXES)


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> JSONResponse:
    """构造统一错误响应"""
    body: Dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    """给应用注册统一错误处理器（转发接口自动走原形状）"""

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if is_forward_path(request.url.path):
            # 转发协议接口：保持 FastAPI/Starlette 默认的 {"detail": ...}
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": jsonable_encoder(exc.detail)},
                headers=getattr(exc, "headers", None),
            )

        code, message, details = _split_detail(exc)
        return error_response(exc.status_code, code, message, details, getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = jsonable_encoder(exc.errors())
        if is_forward_path(request.url.path):
            return JSONResponse(status_code=422, content={"detail": errors})
        return error_response(
            422,
            "validation_error",
            "请求参数校验失败",
            {"errors": errors},
        )


def _split_detail(exc: StarletteHTTPException) -> tuple:
    """把路由里写的 detail 归一成 (code, message, details)"""
    status_code = int(getattr(exc, "status_code", 500))
    code = getattr(exc, "code", None) or _DEFAULT_CODES.get(status_code, "internal_error")
    message = getattr(exc, "message", None)
    details: Optional[Dict[str, Any]] = getattr(exc, "details", None)
    detail = getattr(exc, "detail", None)

    if isinstance(detail, dict):
        code = str(detail.get("code") or code)
        message = message or detail.get("message")
        # 除了 code/message 之外的键都是结构化细节，原样回传
        extra = {key: value for key, value in detail.items() if key not in ("code", "message")}
        if extra:
            details = {**(details or {}), **extra}
    elif isinstance(detail, list):
        message = message or "请求参数校验失败"
        details = {"errors": detail}
    elif isinstance(detail, str) and detail:
        message = message or detail

    return code, message or _DEFAULT_MESSAGES.get(status_code, "请求失败"), details
