"""管理接口单元测试 - /admin/usage、/admin/audit、/metrics

约定：
- 未配置 GATEWAY_API_KEY 时管理接口一律 403（默认关闭对外暴露）
- 配置密钥后缺少/错误密钥 401
- 不进入 lifespan：只验证路由与鉴权，不加载 models.yaml、不启动插件
"""

import pytest
from fastapi.testclient import TestClient

from openai_proxy.audit.config import AuditConfig
from openai_proxy.audit.manager import AuditManager
from openai_proxy.audit.query import UsageQueryService
from openai_proxy.audit.record import AuditAttempt
from openai_proxy.audit.store import SqliteAuditStore
from openai_proxy.service import OpenAIProxyService

_KEY = "test-gateway-key"


def _attempt(key: str, ts: str, model: str, outcome: str) -> AuditAttempt:
    """构造一条已收尾的审计行（示例平台/示例模型）"""
    attempt = AuditAttempt(
        attempt_key=key,
        gateway_request_id=key.rsplit("-", 1)[0],
        endpoint="chat_completions",
        stream=True,
        selector="all",
        platform=model.split("|", 1)[0],
        model_name=model,
        model_id=model.split("|", 1)[-1],
        attempt_seq=int(key.rsplit("-", 1)[1]),
        ts=ts,
    )
    if outcome == "failure":
        attempt.mark_failure()
    elif outcome == "aborted":
        attempt.mark_aborted()
    else:
        attempt.mark_success({
            "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
            "choices": [{"message": {"content": "ok"}}],
        })
    return attempt


def _client(tmp_path) -> TestClient:
    """构造一个不跑 lifespan 的客户端，并把审计库指到临时文件"""
    db_path = str(tmp_path / "audit.db")
    store = SqliteAuditStore(db_path)
    store.init_schema()
    store.insert_many([
        _attempt("r1-1", "2026-01-01T10:00:00.000+00:00", "example-platform|model-a", "success").to_row(),
        _attempt("r2-1", "2026-01-01T10:01:00.000+00:00", "example-platform|model-a", "failure").to_row(),
        _attempt("r2-2", "2026-01-01T10:01:05.000+00:00", "example-platform|model-b", "success").to_row(),
    ])

    service = OpenAIProxyService(config_file="models.yaml")
    service.audit = AuditManager(AuditConfig(db_path=db_path, prometheus_enabled=True))
    service.audit.store = store
    service.audit.query_service = UsageQueryService(store)
    # 不启动后台刷盘任务：本组测试只读
    return TestClient(service.create_app())


class TestAuth:
    """管理接口的鉴权边界"""

    @pytest.mark.unit
    def test_未配置密钥时管理接口被拒绝(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
        client = _client(tmp_path)

        for path in ("/admin/usage/summary", "/admin/audit", "/admin/audit/stats", "/metrics"):
            response = client.get(path)
            assert response.status_code == 403, path
            assert response.json()["error"]["code"] == "admin_disabled"

    @pytest.mark.unit
    def test_配置密钥后缺少密钥返回401(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", _KEY)
        client = _client(tmp_path)

        response = client.get("/admin/usage/summary")
        assert response.status_code == 401

    @pytest.mark.unit
    def test_密钥错误时返回401(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", _KEY)
        client = _client(tmp_path)

        response = client.get("/admin/usage/summary", headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401

    @pytest.mark.unit
    def test_健康检查始终不需要密钥(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", _KEY)
        client = _client(tmp_path)

        assert client.get("/health").status_code == 200

    @pytest.mark.unit
    def test_前端静态壳不因管理接口鉴权而被拒(self, tmp_path, monkeypatch):
        """页面本身不含敏感数据，加载它不需要密钥；数据接口仍需密钥"""
        monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
        client = _client(tmp_path)

        assert client.get("/admin/ui").status_code != 403


class TestUsageEndpoints:
    """用量与性能统计接口"""

    @pytest.mark.unit
    def test_summary按模型聚合(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", _KEY)
        client = _client(tmp_path)
        headers = {"Authorization": f"Bearer {_KEY}"}

        payload = client.get("/admin/usage/summary?group_by=model&since=2026-01-01", headers=headers).json()

        by_model = {item["bucket"]: item for item in payload["items"]}
        assert by_model["example-platform|model-a"]["attempts"] == 2
        assert by_model["example-platform|model-a"]["success"] == 1
        assert by_model["example-platform|model-a"]["failure"] == 1
        assert by_model["example-platform|model-a"]["success_rate"] == pytest.approx(0.5, abs=1e-3)
        # 一次请求触发的故障转移：两条 attempt 共用同一个 gateway_request_id 才会被算作切换
        assert by_model["example-platform|model-a"]["output_tokens"] == 200

    @pytest.mark.unit
    def test_summary非法分组返回400(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", _KEY)
        client = _client(tmp_path)
        headers = {"Authorization": f"Bearer {_KEY}"}

        assert client.get("/admin/usage/summary?group_by=nope", headers=headers).status_code == 400

    @pytest.mark.unit
    def test_timeseries按小时出点(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", _KEY)
        client = _client(tmp_path)
        headers = {"Authorization": f"Bearer {_KEY}"}

        payload = client.get("/admin/usage/timeseries?bucket=hour&since=2026-01-01", headers=headers).json()

        assert [point["bucket"] for point in payload["points"]] == ["2026-01-01T10"]

    @pytest.mark.unit
    def test_非法时间参数返回400(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", _KEY)
        client = _client(tmp_path)
        headers = {"Authorization": f"Bearer {_KEY}"}

        assert client.get("/admin/usage/timeseries?since=昨天", headers=headers).status_code == 400

    @pytest.mark.unit
    def test_模型榜可用任意维度排序(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", _KEY)
        client = _client(tmp_path)
        headers = {"Authorization": f"Bearer {_KEY}"}

        payload = client.get("/admin/usage/models?sort=tokens", headers=headers).json()

        assert payload["items"][0]["bucket"] == "example-platform|model-a"

    @pytest.mark.unit
    def test_非法排序返回400(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", _KEY)
        client = _client(tmp_path)
        headers = {"Authorization": f"Bearer {_KEY}"}

        assert client.get("/admin/usage/models?sort=random", headers=headers).status_code == 400


class TestAuditEndpoints:
    """审计明细接口"""

    @pytest.mark.unit
    def test_明细支持按结局过滤(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", _KEY)
        client = _client(tmp_path)
        headers = {"Authorization": f"Bearer {_KEY}"}

        payload = client.get("/admin/audit?outcome=failure", headers=headers).json()

        assert payload["total"] == 1
        assert payload["items"][0]["outcome"] == "failure"

    @pytest.mark.unit
    def test_非法结局返回400(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", _KEY)
        client = _client(tmp_path)
        headers = {"Authorization": f"Bearer {_KEY}"}

        assert client.get("/admin/audit?outcome=nope", headers=headers).status_code == 400

    @pytest.mark.unit
    def test_可以还原一次请求的故障转移链(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", _KEY)
        client = _client(tmp_path)
        headers = {"Authorization": f"Bearer {_KEY}"}

        payload = client.get("/admin/audit/chain/r2", headers=headers).json()

        assert [item["attempt_seq"] for item in payload["attempts"]] == [1, 2]
        assert [item["model_name"] for item in payload["attempts"]] == [
            "example-platform|model-a",
            "example-platform|model-b",
        ]

    @pytest.mark.unit
    def test_stats暴露审计自身状态(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", _KEY)
        client = _client(tmp_path)
        headers = {"Authorization": f"Bearer {_KEY}"}

        payload = client.get("/admin/audit/stats", headers=headers).json()

        assert payload["store"]["rows"] == 3
        assert payload["config"]["db_path"].endswith("audit.db")
        # 未启动采集器时也要能给出可读状态
        assert payload["collector"]["enabled"] is True

    @pytest.mark.unit
    def test_删除指定时间之前的记录(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", _KEY)
        client = _client(tmp_path)
        headers = {"Authorization": f"Bearer {_KEY}"}

        payload = client.delete("/admin/audit?before=2026-01-01T10:01:00.000%2B00:00", headers=headers).json()

        assert payload["deleted"] == 1
        assert client.get("/admin/audit", headers=headers).json()["total"] == 2

    @pytest.mark.unit
    def test_非法清理时间返回400(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", _KEY)
        client = _client(tmp_path)
        headers = {"Authorization": f"Bearer {_KEY}"}

        assert client.delete("/admin/audit?before=not-a-time", headers=headers).status_code == 400


class TestMetricsEndpoint:
    """Prometheus 指标出口"""

    @pytest.mark.unit
    def test_metrics返回prometheus文本(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", _KEY)
        client = _client(tmp_path)
        headers = {"Authorization": f"Bearer {_KEY}"}

        response = client.get("/metrics", headers=headers)

        assert response.status_code == 200
        assert "proxy_requests_total" in response.text
        assert "proxy_stream_throughput_tokens_per_second" in response.text

    @pytest.mark.unit
    def test_关闭prometheus时指标出口返回404(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", _KEY)
        db_path = str(tmp_path / "audit.db")
        service = OpenAIProxyService(config_file="models.yaml")
        service.audit = AuditManager(AuditConfig(db_path=db_path, prometheus_enabled=False))
        client = TestClient(service.create_app())

        response = client.get("/metrics", headers={"Authorization": f"Bearer {_KEY}"})

        assert response.status_code == 404
