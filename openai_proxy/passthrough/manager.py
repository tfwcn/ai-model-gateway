"""
透传管理器 - 路由表、共享会话与热重载

路由按前缀长度倒序匹配；配置热重载时整体替换元组，
进行中的请求各自持有旧的 route 对象，不会被改到一半。
"""
import logging
from typing import Any, Dict, List, Optional

import aiohttp
from fastapi import Request
from fastapi.responses import Response

from openai_proxy.passthrough.config import PassthroughRoute, parse_routes
from openai_proxy.passthrough.forwarder import PassthroughForwarder

logger = logging.getLogger(__name__)


class PassthroughManager:
    """透传路由注册表与 aiohttp 会话"""

    def __init__(self, audit_manager: Any):
        self.audit_manager = audit_manager
        self.routes: tuple = ()
        self.session: Optional[aiohttp.ClientSession] = None

    # ---------- 配置 ----------

    def configure(self, raw: Optional[Dict[str, Any]]) -> None:
        """应用 passthrough 段；非法路由只跳过自己"""
        routes = [route for route in parse_routes(raw) if route.enabled]
        routes.sort(key=lambda item: len(item.prefix), reverse=True)
        self.routes = tuple(routes)
        logger.info(
            "透传路由已加载: "
            + (", ".join(f"{route.name}{route.prefix}" for route in routes) if routes else "无")
        )

    def describe(self) -> List[Dict[str, Any]]:
        """管理接口展示用（不含上游地址与密钥）"""
        return [route.describe() for route in self.routes]

    # ---------- 请求 ----------

    async def handle(self, request: Request) -> Optional[Response]:
        """匹配路由并转发；没有命中返回 None，由上层给 404"""
        path = request.url.path
        for route in self.routes:
            remainder = route.matches(path)
            if remainder is None:
                continue
            session = await self.get_session()
            caller = getattr(request.state, "caller", None)
            api_user = getattr(caller, "name", "") or "admin"
            forwarder = PassthroughForwarder(session, route, self.audit_manager)
            return await forwarder.forward(request, remainder, api_user)
        return None

    # ---------- 会话 ----------

    async def get_session(self) -> aiohttp.ClientSession:
        """懒创建共享会话（连接池上限与模型转发保持一致）"""
        if self.session is None or self.session.closed:
            connector = aiohttp.TCPConnector(limit=100, limit_per_host=20)
            self.session = aiohttp.ClientSession(connector=connector)
        return self.session

    async def close(self) -> None:
        """关闭共享会话"""
        if self.session is not None and not self.session.closed:
            await self.session.close()
        self.session = None
