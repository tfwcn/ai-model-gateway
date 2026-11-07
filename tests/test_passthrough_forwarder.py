"""透传转发链路测试

用真实 aiohttp 上游 + FastAPI ASGI 客户端验证：
- 路径、查询串、请求体原样转发
- 客户端 Authorization 被替换成上游密钥
- 响应状态码与响应头原样返回
- 流式响应按块转发
- 上游失败会记成失败审计
"""
import asyncio

import aiohttp
import httpx
import pytest
from aiohttp import web

from openai_proxy.audit.config import AuditConfig
from openai_proxy.audit.manager import AuditManager
from openai_proxy.service import OpenAIProxyService


class UpstreamServer:
    """测试用上游：回显请求信息，并提供流式与错误响应"""

    def __init__(self):
        self.app = web.Application()
        self.runner = None
        self.site = None
        self.base_url = ""
        self.app.router.add_route("*", "/v1/echo", self.echo)
        self.app.router.add_get("/v1/stream", self.stream)
        self.app.router.add_get("/v1/fail", self.fail)

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}/v1"

    async def stop(self):
        if self.runner is not None:
            await self.runner.cleanup()

    @staticmethod
    async def echo(request):
        body = await request.text()
        return web.json_response({
            "method": request.method,
            "path": request.path,
            "query": dict(request.query),
            "body": body,
            "authorization": request.headers.get("Authorization"),
            "tenant": request.headers.get("X-Tenant"),
        }, headers={"X-Upstream": "yes"})

    @staticmethod
    async def stream(request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for chunk in ("data: one\n\n", "data: two\n\n"):
            await response.write(chunk.encode())
        await response.write_eof()
        return response

    @staticmethod
    async def fail(request):
        return web.json_response({"error": "bad"}, status=429)


def build_service(monkeypatch, upstream: str, route: dict, audit: AuditManager = None):
    """构造不跑 lifespan 的服务，并手动灌入透传路由"""
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("API_KEYS_DB_PATH", raising=False)
    service = OpenAIProxyService(config_file="models.yaml")
    if audit is not None:
        service.audit = audit
    service.passthrough.audit_manager = service.audit
    service.passthrough.configure({"example-service": route | {"upstream": upstream}})
    return service


@pytest.fixture
async def upstream():
    server = UpstreamServer()
    await server.start()
    yield server
    await server.stop()


class TestForwarding:
    """请求与响应透传"""

    @pytest.mark.integration
    async def test_路径查询体与请求头原样转发(self, upstream, monkeypatch):
        service = build_service(monkeypatch, upstream.base_url, {
            "apiKey": "upstream-secret",
            "forward_headers": ["x-tenant"],
        })
        app = service.create_app()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/proxy/example-service/echo?size=1024",
                json={"model": "model-a"},
                headers={"Authorization": "Bearer gateway-key", "X-Tenant": "team-a"},
            )

        assert response.status_code == 200
        assert response.headers["x-upstream"] == "yes"
        payload = response.json()
        assert payload["method"] == "POST"
        assert payload["path"] == "/v1/echo"
        assert payload["query"] == {"size": "1024"}
        assert payload["body"] == '{"model":"model-a"}'
        # 客户端密钥不会被带到上游
        assert payload["authorization"] == "Bearer upstream-secret"
        assert payload["tenant"] == "team-a"
        await service.passthrough.close()

    @pytest.mark.integration
    async def test_流式响应按块转发(self, upstream, monkeypatch):
        service = build_service(monkeypatch, upstream.base_url, {"apiKey": "upstream-secret"})
        app = service.create_app()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/proxy/example-service/stream")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.text == "data: one\n\ndata: two\n\n"
        await service.passthrough.close()

    @pytest.mark.integration
    async def test_上游错误状态原样返回(self, upstream, monkeypatch):
        service = build_service(monkeypatch, upstream.base_url, {"apiKey": "upstream-secret"})
        app = service.create_app()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/proxy/example-service/fail")

        assert response.status_code == 429
        assert response.json() == {"error": "bad"}
        await service.passthrough.close()

    @pytest.mark.integration
    async def test_未配置前缀返回404(self, upstream, monkeypatch):
        service = build_service(monkeypatch, upstream.base_url, {})
        app = service.create_app()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/proxy/nope/echo")

        assert response.status_code == 404
        await service.passthrough.close()


class TestAudit:
    """透传请求进入审计与维度日汇总"""

    @pytest.mark.integration
    async def test_请求记入审计与维度汇总(self, upstream, monkeypatch, tmp_path):
        audit = AuditManager(AuditConfig(
            db_path=str(tmp_path / "audit.db"), flush_interval=0.05, prometheus_enabled=False
        ))
        service = build_service(monkeypatch, upstream.base_url, {
            "apiKey": "upstream-secret",
            "dimensions": {"model": "body.model", "size": "body.size"},
        }, audit=audit)
        app = service.create_app()
        await audit.start()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/proxy/example-service/echo",
                json={"model": "model-a", "size": "1024x1024"},
            )
            assert response.status_code == 200

        # 等后台刷盘，或主动推一次
        await asyncio.sleep(0.2)
        await audit.collector._flush_once()

        attempts = audit.query_service.list_attempts()["items"]
        assert len(attempts) == 1
        row = attempts[0]
        assert row["endpoint"] == "passthrough:example-service"
        assert row["outcome"] == "success"
        assert row["http_status"] == 200
        assert row["model_name"] == "model-a"
        assert '"size":"1024x1024"' in row["dimensions"]

        dimensions = audit.query_service.dimension_usage("model,size")
        assert dimensions["items"][0]["values"] == {"model": "model-a", "size": "1024x1024"}
        assert dimensions["items"][0]["requests"] == 1

        await audit.close()
        await service.passthrough.close()

    @pytest.mark.integration
    async def test_用户密钥标签写入透传审计(self, upstream, monkeypatch, tmp_path):
        audit = AuditManager(AuditConfig(
            db_path=str(tmp_path / "audit.db"), flush_interval=0.05, prometheus_enabled=False
        ))
        service = build_service(monkeypatch, upstream.base_url, {
            "apiKey": "upstream-secret",
            "dimensions": {"model": "body.model"},
        }, audit=audit)
        monkeypatch.setenv("API_KEYS_DB_PATH", str(tmp_path / "keys.db"))
        app = service.create_app()
        await service.user_keys.start()
        user = service.user_keys.create_user("user-a")
        await audit.start()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/proxy/example-service/echo",
                json={"model": "model-a"},
                headers={"Authorization": f"Bearer {user['key']}"},
            )

        assert response.status_code == 200
        await asyncio.sleep(0.2)
        await audit.collector._flush_once()

        row = audit.query_service.list_attempts()["items"][0]
        assert row["api_user"] == "user-a"

        await audit.close()
        await service.user_keys.close()
        await service.passthrough.close()

    @pytest.mark.integration
    async def test_上游429记成失败审计(self, upstream, monkeypatch, tmp_path):
        audit = AuditManager(AuditConfig(
            db_path=str(tmp_path / "audit.db"), flush_interval=0.05, prometheus_enabled=False
        ))
        service = build_service(monkeypatch, upstream.base_url, {
            "apiKey": "upstream-secret",
            "dimensions": {"model": "body.model"},
        }, audit=audit)
        app = service.create_app()
        await audit.start()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/proxy/example-service/fail")

        assert response.status_code == 429
        await asyncio.sleep(0.2)
        await audit.collector._flush_once()

        row = audit.query_service.list_attempts()["items"][0]
        assert row["outcome"] == "failure"
        assert row["http_status"] == 429
        assert row["error_category"] == "upstream_4xx"

        await audit.close()
        await service.passthrough.close()
