"""管理接口：永久维度汇总与维度候选

复用现有审计库构造方式，不启动 lifespan。
"""
import csv
import io

import pytest
from fastapi.testclient import TestClient

from openai_proxy.audit.config import AuditConfig
from openai_proxy.audit.manager import AuditManager
from openai_proxy.audit.query import UsageQueryService
from openai_proxy.audit.record import AuditAttempt
from openai_proxy.audit.store import SqliteAuditStore
from openai_proxy.service import OpenAIProxyService

_KEY = "test-gateway-key"


def _attempt(key: str, user: str, model: str, size: str) -> AuditAttempt:
    attempt = AuditAttempt(
        attempt_key=key,
        gateway_request_id=key.rsplit("-", 1)[0],
        endpoint="passthrough:example-service",
        stream=False,
        api_user=user,
        dimensions={
            "platform": "passthrough",
            "model": model,
            "model_id": model,
            "selector": "example-service",
            "size": size,
        },
        ts="2026-01-01T10:00:00.000+00:00",
        input_tokens=100,
        cached_tokens=10,
        output_tokens=20,
    )
    attempt.mark_success()
    return attempt


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("GATEWAY_API_KEY", _KEY)
    db_path = str(tmp_path / "audit.db")
    store = SqliteAuditStore(db_path)
    store.init_schema()
    store.insert_many([
        _attempt("r1-1", "user-a", "model-a", "1024x1024").to_row(),
        _attempt("r2-1", "user-a", "model-a", "512x512").to_row(),
        _attempt("r3-1", "user-b", "model-b", "1024x1024").to_row(),
    ])
    store.aggregate_usage()

    service = OpenAIProxyService(config_file="models.yaml")
    service.audit = AuditManager(AuditConfig(db_path=db_path, prometheus_enabled=False))
    service.audit.store = store
    service.audit.query_service = UsageQueryService(store)
    service.passthrough.audit_manager = service.audit
    service.passthrough.configure({
        "example-service": {
            "upstream": "https://api.example.com/v1",
            "dimensions": {"model": "body.model", "size": "body.size"},
        }
    })
    return TestClient(service.create_app())


class TestDimensionEndpoints:
    """维度汇总接口"""

    @pytest.mark.unit
    def test_按模型与分辨率交叉汇总(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        headers = {"Authorization": f"Bearer {_KEY}"}

        payload = client.post(
            "/admin/usage/dimensions?group_by=model,size&since=2026-01-01",
            headers=headers,
        ).json()

        assert payload["group_by"] == ["model", "size"]
        assert payload["count"] == 3
        values = {(item["values"]["model"], item["values"]["size"]) for item in payload["items"]}
        assert values == {("model-a", "1024x1024"), ("model-a", "512x512"), ("model-b", "1024x1024")}

    @pytest.mark.unit
    def test_按用户过滤(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        headers = {"Authorization": f"Bearer {_KEY}"}

        payload = client.post(
            "/admin/usage/dimensions?group_by=model&user=user-a",
            headers=headers,
        ).json()

        assert payload["count"] == 1
        assert payload["items"][0]["values"]["model"] == "model-a"
        assert payload["items"][0]["requests"] == 2

    @pytest.mark.unit
    def test_按月与用户模型组合返回tokens(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        headers = {"Authorization": f"Bearer {_KEY}"}

        payload = client.post(
            "/admin/usage/dimensions?group_by=month,user,model&since=2026-01-01",
            headers=headers,
        ).json()

        assert payload["group_by"] == ["month", "user", "model"]
        first = payload["items"][0]
        assert first["values"]["month"] == "2026-01"
        assert first["input_tokens"] > 0
        assert first["cached_tokens"] > 0
        assert first["output_tokens"] > 0

    @pytest.mark.unit
    def test_导出csv包含维度列与固定指标(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        headers = {"Authorization": f"Bearer {_KEY}"}

        response = client.post(
            "/admin/usage/dimensions/export?group_by=model,size&since=2026-01-01",
            headers=headers,
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers["content-disposition"]

        # BOM 让 Excel 正确识别 UTF-8；解析时用 utf-8-sig 去掉
        text = response.content.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text)))
        assert rows[0] == [
            "model", "size", "请求", "尝试", "成功", "失败", "中断",
            "输入tokens", "缓存tokens", "输出tokens", "成功率",
        ]
        assert len(rows) == 4
        assert {tuple(row[:2]) for row in rows[1:]} == {
            ("model-a", "1024x1024"),
            ("model-a", "512x512"),
            ("model-b", "1024x1024"),
        }
        assert {row[-1] for row in rows[1:]} == {"100.0%"}

    @pytest.mark.unit
    def test_维度候选合并配置与历史(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        response = client.post("/admin/usage/dimension-keys", headers={"Authorization": f"Bearer {_KEY}"})

        payload = response.json()
        assert payload["routes"][0]["route"] == "example-service"
        assert {"model", "size"} <= set(payload["keys"])
        assert "model" in payload["stored"]

    @pytest.mark.unit
    def test_非法分组名返回400(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)

        response = client.post(
            "/admin/usage/dimensions?group_by=model;DROP",
            headers={"Authorization": f"Bearer {_KEY}"},
        )

        assert response.status_code == 400
