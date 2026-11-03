"""用户级密钥单元测试

覆盖：
- 管理密钥与用户密钥的归属判定
- SQLite 落库只保存摘要，列表永不返回明文
- 新增/删除后快照立即生效
- 管理接口只接受管理密钥，用户密钥只能走数据面
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from openai_proxy.audit.query import UsageQueryService
from openai_proxy.audit.record import AuditAttempt
from openai_proxy.audit.store import SqliteAuditStore
from openai_proxy.auth.keys import (
    ADMIN_NAME,
    ADMIN_ROLE,
    USER_ROLE,
    ApiKeyRegistry,
    hash_api_key,
)
from openai_proxy.auth.manager import UserKeyManager, UserNameExists
from openai_proxy.service import OpenAIProxyService

_ADMIN_KEY = "test-admin-key"


def _ctx(**overrides):
    """构造一条审计行，默认属于 user-a"""
    params = dict(
        attempt_key="r1-1",
        gateway_request_id="r1",
        endpoint="chat_completions",
        stream=True,
        platform="example-platform",
        model_name="example-platform|model-a",
        model_id="model-a",
        attempt_seq=1,
        api_user="user-a",
    )
    params.update(overrides)
    attempt = AuditAttempt(**params)
    attempt.mark_success()
    return attempt


class TestRegistry:
    """内存快照的解析规则"""

    @pytest.mark.unit
    def test_管理密钥解析为管理员(self):
        registry = ApiKeyRegistry(admin_key=_ADMIN_KEY)

        caller = registry.resolve(_ADMIN_KEY)

        assert caller is not None
        assert caller.name == ADMIN_NAME and caller.role == ADMIN_ROLE

    @pytest.mark.unit
    def test_用户密钥按摘要命中(self):
        registry = ApiKeyRegistry(admin_key=_ADMIN_KEY)
        registry.set_user_hashes({hash_api_key("gw_user-a"): "user-a"})

        caller = registry.resolve("gw_user-a")

        assert caller is not None
        assert caller.name == "user-a" and caller.role == USER_ROLE

    @pytest.mark.unit
    def test_未知密钥返回空(self):
        registry = ApiKeyRegistry(admin_key=_ADMIN_KEY)
        registry.set_user_hashes({hash_api_key("gw_user-a"): "user-a"})

        assert registry.resolve("gw_unknown") is None
        assert registry.resolve("") is None

    @pytest.mark.unit
    def test_只有用户密钥时管理接口保持关闭(self):
        registry = ApiKeyRegistry(admin_key="")
        registry.set_user_hashes({hash_api_key("gw_user-a"): "user-a"})

        assert registry.enabled is True
        assert registry.admin_enabled is False


class TestStore:
    """SQLite 持久化与快照刷新"""

    @pytest.fixture
    def manager(self, tmp_path):
        instance = UserKeyManager(admin_key=_ADMIN_KEY, db_path=str(tmp_path / "api_keys.db"))
        instance.start_sync()
        yield instance
        instance.store.close()

    @pytest.mark.unit
    def test_新增用户返回明文但库里只存摘要(self, manager):
        created = manager.create_user("user-a")
        key = created["key"]

        assert key.startswith("gw_")
        assert created["user"]["name"] == "user-a"
        # 列表不含明文与摘要
        listed = manager.list_users()["items"]
        assert listed[0]["key_prefix"] == key[:9]
        assert "key" not in listed[0] and "key_hash" not in listed[0]

        rows = manager.store.snapshot()
        assert rows == {hash_api_key(key): "user-a"}

    @pytest.mark.unit
    def test_新增后立即可以鉴权(self, manager):
        key = manager.create_user("user-b")["key"]

        caller = manager.resolve(key)
        assert caller is not None and caller.name == "user-b"

    @pytest.mark.unit
    def test_用户名重复时拒绝(self, manager):
        manager.create_user("user-a")

        with pytest.raises(UserNameExists):
            manager.create_user("user-a")

    @pytest.mark.unit
    def test_数据库层也拒绝重名(self, manager):
        """即使绕过 create_user 的预检查，表上的 UNIQUE 约束仍然生效"""
        manager.create_user("user-a")

        with pytest.raises(sqlite3.IntegrityError):
            manager.store.create_user("user-a", "gw_other01", hash_api_key("gw_other-key"))

    @pytest.mark.unit
    def test_删除后密钥立即失效(self, manager):
        created = manager.create_user("user-c")
        key = created["key"]
        user_id = created["user"]["id"]

        assert manager.delete_user(user_id) is True
        assert manager.resolve(key) is None
        assert manager.list_users()["items"] == []

    @pytest.mark.unit
    def test_删除不存在的用户返回false(self, manager):
        assert manager.delete_user(999) is False


class TestRoutes:
    """管理接口与数据面的鉴权边界"""

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", _ADMIN_KEY)
        monkeypatch.setenv("API_KEYS_DB_PATH", str(tmp_path / "api_keys.db"))
        service = OpenAIProxyService(config_file="models.yaml")
        app = service.create_app()
        service.user_keys.start_sync()
        yield TestClient(app), service

    @staticmethod
    def _admin_headers() -> dict:
        return {"Authorization": f"Bearer {_ADMIN_KEY}"}

    @pytest.mark.unit
    def test_新增用户返回一次性明文密钥(self, client):
        api, _ = client

        response = api.post("/admin/users", headers=self._admin_headers(), json={"name": "user-a"})

        assert response.status_code == 201
        body = response.json()
        assert body["user"]["name"] == "user-a"
        assert body["key"].startswith("gw_")

    @pytest.mark.unit
    def test_列表不返回明文与摘要(self, client):
        api, _ = client
        key = api.post("/admin/users", headers=self._admin_headers(), json={"name": "user-a"}).json()["key"]

        body = api.post("/admin/users/list", headers=self._admin_headers()).json()

        assert body["count"] == 1
        item = body["items"][0]
        assert item["name"] == "user-a"
        assert item["key_prefix"] == key[:9]
        assert key not in str(body)
        assert "key_hash" not in item

    @pytest.mark.unit
    def test_用户密钥可以访问数据面但不能访问管理接口(self, client):
        api, _ = client
        key = api.post("/admin/users", headers=self._admin_headers(), json={"name": "user-a"}).json()["key"]
        user_headers = {"Authorization": f"Bearer {key}"}

        assert api.get("/v1/models", headers=user_headers).status_code == 200
        forbidden = api.post("/admin/users/list", headers=user_headers)
        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "forbidden"
        assert api.get("/metrics", headers=user_headers).status_code == 403

    @pytest.mark.unit
    def test_删除后用户密钥立即失效(self, client):
        api, _ = client
        created = api.post("/admin/users", headers=self._admin_headers(), json={"name": "user-a"}).json()
        user_headers = {"Authorization": f"Bearer {created['key']}"}

        deleted = api.post(f"/admin/users/{created['user']['id']}/delete", headers=self._admin_headers())

        assert deleted.status_code == 200
        assert api.get("/v1/models", headers=user_headers).status_code == 401

    @pytest.mark.unit
    def test_错误密钥与缺少密钥都返回401(self, client):
        api, _ = client
        assert api.get("/v1/models").status_code == 401
        assert api.get("/v1/models", headers={"Authorization": "Bearer gw_wrong"}).status_code == 401

    @pytest.mark.unit
    def test_非法用户id返回400(self, client):
        api, _ = client

        response = api.post("/admin/users/abc/delete", headers=self._admin_headers())
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_user_id"

    @pytest.mark.unit
    def test_重复用户名返回409和错误码(self, client):
        api, _ = client
        api.post("/admin/users", headers=self._admin_headers(), json={"name": "user-a"})

        response = api.post("/admin/users", headers=self._admin_headers(), json={"name": "user-a"})

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "user_exists"

    @pytest.mark.unit
    def test_不存在的用户返回404和错误码(self, client):
        api, _ = client

        response = api.post("/admin/users/999/delete", headers=self._admin_headers())

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "user_not_found"

    @pytest.mark.unit
    def test_请求体校验失败返回422统一错误(self, client):
        api, _ = client

        response = api.post("/admin/users", headers=self._admin_headers(), json={"name": ""})

        assert response.status_code == 422
        body = response.json()
        assert body["error"]["code"] == "validation_error"
        assert body["error"]["details"]["errors"]

    @pytest.mark.unit
    def test_管理查询不再接受GET(self, client):
        api, _ = client

        response = api.get("/admin/users/list", headers=self._admin_headers())

        assert response.status_code == 405
        assert response.json()["error"]["code"] == "method_not_allowed"


class TestUsageByUser:
    """按用户聚合与过滤"""

    @pytest.fixture
    def queries(self, tmp_path):
        store = SqliteAuditStore(str(tmp_path / "audit.db"))
        store.init_schema()
        store.insert_many([
            _ctx(attempt_key="r1-1", gateway_request_id="r1", api_user="user-a").to_row(),
            _ctx(attempt_key="r2-1", gateway_request_id="r2", api_user="user-b").to_row(),
            _ctx(attempt_key="r3-1", gateway_request_id="r3", api_user="").to_row(),
        ])
        yield UsageQueryService(store)
        store.close()

    @pytest.mark.unit
    def test_按用户聚合(self, queries):
        result = queries.summary(group_by="user")

        buckets = {item["bucket"]: item for item in result["items"]}
        assert buckets["user-a"]["requests"] == 1
        assert buckets["user-b"]["requests"] == 1
        # 历史行或未启用鉴权的流量统一归到 admin
        assert buckets["admin"]["requests"] == 1

    @pytest.mark.unit
    def test_按用户过滤(self, queries):
        result = queries.summary(group_by="model", user="user-b")

        assert result["count"] == 1
        assert result["items"][0]["requests"] == 1

    @pytest.mark.unit
    def test_审计明细可以按用户过滤(self, queries):
        result = queries.list_attempts(user="user-a")

        assert result["total"] == 1
        assert result["items"][0]["api_user"] == "user-a"
