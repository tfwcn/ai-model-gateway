"""
透传转发器 - 一次请求直连上游，原样返回响应

约束：
- 不做协议转换、不做重试、不做故障转移
- 客户端 Authorization 永不转发给上游：上游鉴权统一用路由配置的密钥
- 请求体需要解析维度时先缓存；超过 body_limit 直接 413
- 响应按块转发，兼容 JSON、二进制与 SSE
"""
import asyncio
import logging
import time
from typing import Any, Dict, Optional

import aiohttp
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from openai_proxy.passthrough.audit import PassthroughAuditRecorder
from openai_proxy.passthrough.config import PassthroughRoute
from openai_proxy.passthrough.dimensions import DimensionExtractor

logger = logging.getLogger(__name__)

# 响应里不能原样回传的逐跳头
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

_CHUNK_SIZE = 64 * 1024


class BodyTooLarge(Exception):
    """请求体超过路由配置的上限"""


class PassthroughForwarder:
    """把一条透传路由的请求转发到上游"""

    def __init__(self, session: aiohttp.ClientSession, route: PassthroughRoute, audit_manager: Any):
        self.session = session
        self.route = route
        self.audit_manager = audit_manager
        self.extractor = DimensionExtractor(route.dimensions)

    async def forward(self, request: Request, remainder: str, api_user: str) -> Response:
        """处理一次透传请求"""
        is_event_stream = "text/event-stream" in request.headers.get("accept", "").lower()
        try:
            body = await self._read_body(request)
        except BodyTooLarge:
            return _error(413, "request_too_large", f"请求体超过 {self.route.body_limit} 字节上限")

        dimensions = self.extractor.extract(
            body=body,
            query=dict(request.query_params),
            headers=request.headers,
            remainder_path=remainder,
        )
        recorder = PassthroughAuditRecorder(
            manager=self.audit_manager,
            route=self.route,
            api_user=api_user,
            dimensions=dimensions,
            stream=is_event_stream,
        )

        started = time.time()
        try:
            upstream = await self.session.request(
                request.method,
                self.route.target_url(remainder, request.url.query),
                headers=self._build_headers(request),
                data=body or None,
                timeout=self._timeout(is_event_stream),
                allow_redirects=False,
                auto_decompress=False,
            )
        except asyncio.TimeoutError:
            recorder.fail(asyncio.TimeoutError(), http_status=504)
            return _error(504, "upstream_timeout", "上游请求超时")
        except aiohttp.ClientError as exc:
            recorder.fail(exc, http_status=502)
            return _error(502, "upstream_unavailable", f"上游连接失败: {exc}")
        except Exception as exc:  # noqa: BLE001 - 未预期异常也要留审计
            logger.error(f"透传请求异常: {exc}", exc_info=True)
            recorder.fail(exc, http_status=502)
            return _error(502, "upstream_unavailable", "上游请求失败")

        content_type = upstream.headers.get("Content-Type", "")
        recorder.attempt.stream = _is_streaming(content_type) or is_event_stream
        recorder.start(
            http_status=upstream.status,
            probe_ms=int(round((time.time() - started) * 1000)),
        )
        return StreamingResponse(
            self._stream_body(upstream, recorder),
            status_code=upstream.status,
            headers=_response_headers(upstream),
        )

    # ---------- 请求 ----------

    async def _read_body(self, request: Request) -> bytes:
        """有上限地缓存请求体，避免大文件把内存打满"""
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.route.body_limit:
            raise BodyTooLarge()

        chunks = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > self.route.body_limit:
                raise BodyTooLarge()
            chunks.append(chunk)
        return b"".join(chunks)

    def _build_headers(self, request: Request) -> Dict[str, str]:
        """只放行白名单请求头，并替换上游鉴权"""
        headers: Dict[str, str] = {}
        for name in self.route.forward_headers:
            value = request.headers.get(name)
            if value:
                headers[name] = value
        headers.update(self.route.extra_headers)
        if self.route.api_key:
            headers[self.route.auth_header] = f"{self.route.auth_prefix}{self.route.api_key}"
        return headers

    def _timeout(self, is_event_stream: bool) -> aiohttp.ClientTimeout:
        """SSE 不设总时长，普通请求按配置的总超时兜底"""
        return aiohttp.ClientTimeout(
            total=None if is_event_stream else self.route.timeout,
            connect=self.route.connect_timeout,
            sock_connect=self.route.connect_timeout,
            sock_read=self.route.timeout,
        )

    # ---------- 响应 ----------

    async def _stream_body(self, upstream: aiohttp.ClientResponse, recorder: PassthroughAuditRecorder):
        """按块转发响应体，并在结束时收尾审计"""
        completed = False
        try:
            async for chunk in upstream.content.iter_chunked(_CHUNK_SIZE):
                recorder.first_byte()
                recorder.observe_chunk(chunk)
                yield chunk
            completed = True
        finally:
            upstream.release()
            recorder.finish(completed, upstream.status)


def _response_headers(upstream: aiohttp.ClientResponse) -> Dict[str, str]:
    """构造回传给客户端的响应头：保留内容相关头，去掉逐跳头"""
    headers: Dict[str, str] = {}
    for name, value in upstream.headers.items():
        if name.lower() in _HOP_BY_HOP:
            continue
        headers[name] = value
    return headers


def _is_streaming(content_type: str) -> bool:
    """判断响应是否按流式记账"""
    lowered = (content_type or "").lower()
    return "text/event-stream" in lowered or "application/x-ndjson" in lowered


def _error(status: int, code: str, message: str) -> JSONResponse:
    """透传链路的本地错误：保持 OpenAI 风格，方便客户端统一处理"""
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": "proxy_error", "code": code}},
    )
