"""/admin/config 接口单元测试 - 鉴权、脱敏、预演、写盘、并发冲突、回滚

约定（与 /admin/usage 一致）：
- 未配置 GATEWAY_API_KEY 时管理接口一律 403
- 配置了密钥但缺少/错误 Bearer 时 401
- 不进入 lifespan：只验证路由行为，不加载插件、不启动爬虫
"""

import os

import pytest
from fastapi.testclient import TestClient

from openai_proxy.config_store.service import ConfigCommandService
from openai_proxy.models import ModelConfig
from openai_proxy.service import OpenAIProxyService

_KEY = "test-gateway-key"

CONFIG = """example-platform:
  baseUrl: "https://api.example.com/v1"
  apiKey: "${EXAMPLE_PLATFORM_API_KEY}"
  timeout: 120
  weight: 10 # 优先级最高
  models:
    - "model-a"
    - name: "model-b"
      first_chunk_timeout: 45
scraper-platform:
  baseUrl: "https://api.scraper.example.com/v1"
  apiKey: "${SCRAPER_PLATFORM_API_KEY}"
  plugin:
    code: "plugin.example-scraper"
    args:
      scrape_url: "https://models.example.com/list"
"""


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个用例结束后恢复环境变量，避免影响其它测试"""
    saved = {name: os.environ.get(name) for name in ("GATEWAY_API_KEY", "CONFIG_API_ENABLED")}
    for name in saved:
        monkeypatch.delenv(name, raising=False)
    yield
    for name, value in saved.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def _client(tmp_path, config_text: str = CONFIG, api_key: str = _KEY, runtime_models=None):
    """构造不跑 lifespan 的客户端，并把配置服务指向临时文件

    Args:
        runtime_models: 塞进进程内的模型表（`{平台名: [ModelConfig, ...]}`），
                        用来测 /admin/config/models 给出的运行时候选
    """
    if api_key is None:
        os.environ.pop("GATEWAY_API_KEY", None)
    else:
        os.environ["GATEWAY_API_KEY"] = api_key

    config_path = tmp_path / "models.yaml"
    config_path.write_text(config_text, encoding="utf-8")

    service = OpenAIProxyService(config_file=str(config_path))
    reloaded = []
    recorder = []

    service._config_service = ConfigCommandService(
        config_file=str(config_path),
        backup_dir=str(tmp_path / "backups"),
        reload_callback=lambda: (reloaded.append(True), {"reloaded": True})[1],
        audit_recorder=lambda ops, detail="", success=True: recorder.append({"ops": ops, "success": success}) or True,
    )

    # 运行时模型表：路由里的 /admin/config/models 读的就是它（热重载会整张换掉）
    service.models = runtime_models if runtime_models is not None else {}

    client = TestClient(service.create_app())
    return client, str(config_path), reloaded, recorder


def _model(platform: str, name: str):
    """造一条进程内的模型条目（只给接口要读的字段）"""
    return ModelConfig(
        name=f"{platform}|{name}",
        api_key="sk-should-never-leak",
        base_url="https://api.example.com/v1",
        model=name,
    )


def _auth(key: str = _KEY) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class TestAuth:
    """鉴权"""

    @pytest.mark.unit
    def test_未配置网关密钥时配置接口整体拒绝(self, tmp_path):
        client, _, _, _ = _client(tmp_path, api_key=None)

        response = client.post("/admin/config/document")

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "admin_disabled"

    @pytest.mark.unit
    def test_缺少Bearer时返回401(self, tmp_path):
        client, _, _, _ = _client(tmp_path)

        assert client.post("/admin/config/document").status_code == 401
        assert client.post("/admin/config/document", headers=_auth("wrong-key")).status_code == 401

    @pytest.mark.unit
    def test_CONFIG_API_ENABLED为false时路由不存在(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONFIG_API_ENABLED", "false")
        client, _, _, _ = _client(tmp_path)

        assert client.post("/admin/config/document", headers=_auth()).status_code == 404


class TestRead:
    """读接口"""

    @pytest.mark.unit
    def test_document返回脱敏视图(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EXAMPLE_PLATFORM_API_KEY", "super-secret-value")
        client, _, _, _ = _client(tmp_path)

        body = client.post("/admin/config/document", headers=_auth()).json()

        assert "super-secret-value" not in str(body)
        platform = next(item for item in body["platforms"] if item["name"] == "example-platform")
        assert platform["apiKey"]["env"] == "EXAMPLE_PLATFORM_API_KEY"
        assert platform["apiKey"]["env_set"] is True
        assert platform["models"][1]["overrides"] == {"first_chunk_timeout": 45}

    @pytest.mark.unit
    def test_raw返回原始YAML(self, tmp_path):
        client, path, _, _ = _client(tmp_path)

        body = client.post("/admin/config/raw", headers=_auth()).json()

        assert body["text"] == CONFIG
        assert body["version"]

    @pytest.mark.unit
    def test_env_status只报告存在性(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EXAMPLE_PLATFORM_API_KEY", "super-secret-value")
        monkeypatch.delenv("SCRAPER_PLATFORM_API_KEY", raising=False)
        client, _, _, _ = _client(tmp_path)

        body = client.post("/admin/config/env-status", headers=_auth()).json()
        by_env = {item["env"]: item for item in body["items"]}

        assert by_env["EXAMPLE_PLATFORM_API_KEY"]["set"] is True
        assert by_env["SCRAPER_PLATFORM_API_KEY"]["set"] is False
        assert "super-secret-value" not in str(body)

    @pytest.mark.unit
    def test_validate与schema可用于前端渲染表单(self, tmp_path):
        client, _, _, _ = _client(tmp_path)

        validate = client.post("/admin/config/validate", headers=_auth()).json()
        schema = client.post("/admin/config/schema", headers=_auth()).json()

        assert validate["valid"]
        assert "set_model_field" in schema["ops"]
        assert "first_chunk_timeout" in schema["platform_fields"]
        assert "weight" in schema["model_fields"]
        assert schema["limits"]["timeout"] == {"min": 1, "max": 3600}

    @pytest.mark.unit
    def test_schema把字段字典与结构说明一起给出(self, tmp_path):
        """前端不再自己写文案：每个能改的字段都在 schema 里有说明"""
        from openai_proxy.config_store import schema as rules

        client, _, _, _ = _client(tmp_path)
        docs = client.post("/admin/config/schema", headers=_auth()).json()["docs"]

        for scope, fields in (
            ("platform", rules.PLATFORM_FIELDS),
            ("model", rules.MODEL_FIELDS),
            ("audit", rules.AUDIT_FIELDS),
        ):
            assert set(docs[scope]) == set(fields), f"{scope} 的字段字典与白名单不一致"
            for field, entry in docs[scope].items():
                assert entry["label"] and entry["summary"], f"{scope}.{field} 没有说明"

    @pytest.mark.unit
    def test_schema给出有限取值字段的候选(self, tmp_path):
        """取值只有几种写法的字段：界面靠选项，而不是让人在输入框里猜写法"""
        from openai_proxy.config_store import field_docs
        from openai_proxy.config_store import schema as rules

        client, _, _, _ = _client(tmp_path)
        payload = client.post("/admin/config/schema", headers=_auth()).json()
        enums = payload["enums"]
        kinds = {**rules.PLATFORM_FIELDS, **rules.MODEL_FIELDS, **rules.AUDIT_FIELDS}

        assert set(enums) == set(field_docs.enum_fields()), "接口给出的取值选项与字段字典不是同一份"
        assert enums["quota_period"]["values"] == ["hourly", "daily", "weekly", "monthly"]
        assert enums["quota_period"]["allow_none"] is True

        for field, entry in enums.items():
            assert field in kinds, f"{field} 还挂着选项，但已经不是可编辑字段了"
            assert entry["values"] and len(set(entry["values"])) == len(entry["values"]), f"{field} 的选项为空或重复"
            for value in entry["values"]:
                issues = rules.check_value(kinds[field], field, value, f"platforms.example-platform.{field}")
                assert not issues, f"{field}={value} 会被后端自己的校验拒绝"
            if entry["allow_none"]:
                issues = rules.check_value(kinds[field], field, None, f"platforms.example-platform.{field}")
                assert not issues, f"{field} 标了「可以不配置」，但写空值会被拒绝"

    @pytest.mark.unit
    def test_schema给出配置文件本身的结构说明(self, tmp_path):
        client, _, _, _ = _client(tmp_path)
        sections = client.post("/admin/config/schema", headers=_auth()).json()["sections"]

        assert sections, "结构说明不能为空"
        assert all(item["title"] and item["body"] for item in sections)
        body = "\n".join(item["body"] for item in sections)
        for keyword in ("all_aliases", "audit", "热重载", "重启"):
            assert keyword in body, f"结构说明里没提到: {keyword}"

    @pytest.mark.unit
    def test_backups初始为空(self, tmp_path):
        client, _, _, _ = _client(tmp_path)

        assert client.post("/admin/config/backups", headers=_auth()).json()["items"] == []


class TestRuntimeModels:
    """运行时模型清单：爬虫平台的模型只在进程里，界面的候选只能从这里拿"""

    @pytest.mark.unit
    def test_给出按平台分组的候选与selector(self, tmp_path):
        client, _, _, _ = _client(tmp_path, runtime_models={
            # 同一个条目灌两次（插件重跑时会出现）：候选里只该出现一次
            "scraper-platform": [_model("scraper-platform", "scraped-model"), _model("scraper-platform", "another-model"),
                                 _model("scraper-platform", "scraped-model")],
            "example-platform": [_model("example-platform", "model-a")],
        })

        body = client.post("/admin/config/models", headers=_auth()).json()

        # 顺序稳定：按平台名排序，界面下拉不会每次刷新都换次序
        assert [item["name"] for item in body["platforms"]] == ["example-platform", "scraper-platform"]
        assert body["loaded"] is True and body["total"] == 3
        names = {item["name"]: [model["name"] for model in item["models"] ] for item in body["platforms"]}
        assert names == {"example-platform": ["model-a"], "scraper-platform": ["another-model", "scraped-model"]}
        # 别名指向要的是 `平台名|条目名`，所以接口直接给好
        selectors = [model["selector"] for item in body["platforms"] for model in item["models"]]
        assert "scraper-platform|scraped-model" in selectors

    @pytest.mark.unit
    def test_清单里没有任何密钥与端点(self, tmp_path):
        client, _, _, _ = _client(tmp_path, runtime_models={"scraper-platform": [_model("scraper-platform", "scraped-model")]})

        text = str(client.post("/admin/config/models", headers=_auth()).json())

        for forbidden in ("sk-should-never-leak", "api.example.com", "api_key", "apiKey", "base_url"):
            assert forbidden not in text, f"运行时清单泄露了 {forbidden}"

    @pytest.mark.unit
    def test_没有模型时给人话说明而不是空列表(self, tmp_path):
        # 只加载平台、还没加载模型时，models[平台] 是平台配置字典而不是模型列表：这种条目要跳过
        client, _, _, _ = _client(tmp_path, runtime_models={"example-platform": {"baseUrl": "https://api.example.com/v1"}})

        body = client.post("/admin/config/models", headers=_auth()).json()

        assert body["platforms"] == [] and body["loaded"] is False and body["total"] == 0
        assert body["note"], "没有候选时必须说明为什么是空的，否则界面看着像坏了"

    @pytest.mark.unit
    def test_清单同样要密钥(self, tmp_path):
        client, _, _, _ = _client(tmp_path, runtime_models={"scraper-platform": [_model("scraper-platform", "scraped-model")]})

        assert client.post("/admin/config/models").status_code == 401
        assert client.post("/admin/config/models", headers=_auth("wrong-key")).status_code == 401

    @pytest.mark.unit
    def test_清单读的是进程当下这份而不是启动时的快照(self, tmp_path):
        runtime = {"scraper-platform": [_model("scraper-platform", "old-model")]}
        client, _, _, _ = _client(tmp_path, runtime_models=runtime)

        assert client.post("/admin/config/models", headers=_auth()).json()["total"] == 1

        # 热重载会换掉平台里的模型：接口每次都要读最新的表，否则下拉里全是已经不存在的模型
        runtime["scraper-platform"] = [_model("scraper-platform", "new-model")]
        after = client.post("/admin/config/models", headers=_auth()).json()

        assert after["platforms"][0]["models"][0]["name"] == "new-model"


class TestPlan:
    """预演"""

    @pytest.mark.unit
    def test_plan不写盘但给出差异(self, tmp_path):
        client, path, reloaded, recorder = _client(tmp_path)

        body = client.post("/admin/config/plan", headers=_auth(), json={
            "ops": [{"op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 3}],
        }).json()

        assert body["valid"]
        assert _text(path) == CONFIG
        assert reloaded == []
        assert recorder == []
        assert any(line.startswith("+  weight: 3") for line in body["diff"])

    @pytest.mark.unit
    def test_plan的行级差异按新增修改删除分类(self, tmp_path):
        """界面要按颜色显示差异，靠的是 diff_rows：每一行给出类别、原行、新行与所属段"""
        client, _, _, _ = _client(tmp_path)

        body = client.post("/admin/config/plan", headers=_auth(), json={
            "ops": [
                {"op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 3},
                {"op": "add_model", "platform": "example-platform", "model": "model-new"},
            ],
        }).json()

        rows = body["diff_rows"]
        kinds = [row["kind"] for row in rows]
        assert "modify" in kinds and "add" in kinds, kinds

        modify = next(row for row in rows if row["kind"] == "modify")
        assert "weight: 10" in modify["before"] and "weight: 3" in modify["after"]
        assert modify["context"] == "example-platform:"

        added = [row for row in rows if row["kind"] == "add"]
        assert added and all(row["before"] == "" for row in added)
        assert any("model-new" in row["after"] for row in added)

    @pytest.mark.unit
    def test_plan对非法操作返回校验结果而不是异常(self, tmp_path):
        client, _, _, _ = _client(tmp_path)

        response = client.post("/admin/config/plan", headers=_auth(), json={
            "ops": [{"op": "set_platform_field", "platform": "example-platform", "field": "timeout", "value": 99999}],
        })

        assert response.status_code == 200
        assert not response.json()["valid"]
        assert "必须在" in response.json()["errors"][0]["message"]


class TestApply:
    """写盘"""

    @pytest.mark.unit
    def test_apply写盘并备份且触发热重载(self, tmp_path):
        client, path, reloaded, recorder = _client(tmp_path)
        version = client.post("/admin/config/raw", headers=_auth()).json()["version"]

        response = client.post("/admin/config/apply", headers=_auth(), json={
            "ops": [
                {"op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 3},
                {"op": "add_model", "platform": "example-platform", "model": "model-new"},
            ],
            "version": version,
            "note": "界面测试",
        })

        assert response.status_code == 200
        body = response.json()
        assert body["reloaded"] is True
        assert reloaded == [True]
        assert recorder[0]["success"] is True
        content = _text(path)
        assert "weight: 3" in content and '"model-new"' in content
        # 写盘前的内容一定被备份了
        assert client.post("/admin/config/backups", headers=_auth()).json()["items"][0]["note"] == "界面测试"

    @pytest.mark.unit
    def test_行内注释在改取值后仍然保留(self, tmp_path):
        client, path, _, _ = _client(tmp_path)

        client.post("/admin/config/apply", headers=_auth(), json={
            "ops": [{"op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 7}],
        })

        assert "优先级最高" in _text(path)

    @pytest.mark.unit
    def test_version过期时返回409(self, tmp_path):
        client, path, _, _ = _client(tmp_path)

        response = client.post("/admin/config/apply", headers=_auth(), json={
            "ops": [{"op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 3}],
            "version": "客户端看到的是旧版本",
        })

        assert response.status_code == 409
        assert "刷新" in response.json()["error"]["message"]
        assert _text(path) == CONFIG

    @pytest.mark.unit
    def test_IfMatch头也可以做并发检查(self, tmp_path):
        client, _, _, _ = _client(tmp_path)
        version = client.post("/admin/config/raw", headers=_auth()).json()["version"]
        headers = dict(_auth())
        headers["If-Match"] = f'"{version}"'

        response = client.post("/admin/config/apply", headers=headers, json={
            "ops": [{"op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 3}],
        })

        assert response.status_code == 200

    @pytest.mark.unit
    def test_IfMatch与请求体的version不一致时返回400(self, tmp_path):
        client, _, _, _ = _client(tmp_path)
        headers = dict(_auth())
        headers["If-Match"] = "aaa"

        response = client.post("/admin/config/apply", headers=headers, json={
            "ops": [{"op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 3}],
            "version": "bbb",
        })

        assert response.status_code == 400

    @pytest.mark.unit
    def test_校验不通过时返回400且不写盘(self, tmp_path):
        client, path, reloaded, _ = _client(tmp_path)

        response = client.post("/admin/config/apply", headers=_auth(), json={
            "ops": [{"op": "set_platform_field", "platform": "ghost", "field": "weight", "value": 3}],
        })

        assert response.status_code == 400
        assert "ghost" in response.json()["error"]["details"]["errors"][0]["message"]
        assert _text(path) == CONFIG
        assert reloaded == []

    @pytest.mark.unit
    def test_爬虫平台的模型只能屏蔽不能删除(self, tmp_path):
        client, _, _, _ = _client(tmp_path)

        response = client.post("/admin/config/apply", headers=_auth(), json={
            "ops": [{"op": "remove_model", "platform": "scraper-platform", "model": "any-model"}],
        })

        assert response.status_code == 400
        detail = response.json()["error"]["details"]["errors"][0]
        assert "blacklist" in detail["message"]
        assert detail["path"].endswith("blacklist")

    @pytest.mark.unit
    def test_结构性改动标记needs_restart(self, tmp_path):
        client, _, reloaded, _ = _client(tmp_path)

        body = client.post("/admin/config/apply", headers=_auth(), json={
            "ops": [{"op": "remove_platform", "platform": "scraper-platform"}],
        }).json()

        assert body["needs_restart"] is True
        # 服务不会自己重启，也不会假装已经生效
        assert body["reloaded"] is False
        assert reloaded == []

    @pytest.mark.unit
    def test_reload为false时跳过热重载(self, tmp_path):
        client, _, reloaded, _ = _client(tmp_path)

        body = client.post("/admin/config/apply", headers=_auth(), json={
            "ops": [{"op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 3}],
            "reload": False,
        }).json()

        assert body["reloaded"] is False
        assert reloaded == []

    @pytest.mark.unit
    def test_新增条目可以一次带上模型级覆盖(self, tmp_path):
        """界面「新增模型条目」用的是这一条 op：新增与覆盖不必分两次写盘"""
        client, path, reloaded, _ = _client(tmp_path)

        body = client.post("/admin/config/apply", headers=_auth(), json={
            "ops": [{"op": "add_model", "platform": "example-platform", "model": "model-new",
                     "overrides": {"weight": 5, "first_chunk_timeout": 30}}],
        }).json()

        assert body["valid"] is True
        text = _text(path)
        assert '- name: "model-new"' in text
        assert "weight: 5" in text
        assert "first_chunk_timeout: 30" in text
        assert reloaded == [True]

    @pytest.mark.unit
    def test_无覆盖的新增保持字符串写法(self, tmp_path):
        """不填覆盖时不要凭空把条目改写成对象，避免无谓的结构变化"""
        client, path, _, _ = _client(tmp_path)

        client.post("/admin/config/apply", headers=_auth(), json={
            "ops": [{"op": "add_model", "platform": "example-platform", "model": "model-plain"}],
        })

        assert '- "model-plain"' in _text(path)

    @pytest.mark.unit
    def test_删除模型级覆盖后回落到平台取值(self, tmp_path):
        client, path, _, _ = _client(tmp_path)

        body = client.post("/admin/config/apply", headers=_auth(), json={
            "ops": [{"op": "unset_model_field", "platform": "example-platform", "model": "model-b",
                     "field": "first_chunk_timeout"}],
        }).json()

        assert body["valid"] is True
        text = _text(path)
        assert "first_chunk_timeout: 45" not in text
        assert '- "model-b"' in text  # 覆盖清空后条目回到字符串写法

    @pytest.mark.unit
    def test_删除平台字段后回落到全局默认(self, tmp_path):
        client, path, _, _ = _client(tmp_path)

        body = client.post("/admin/config/apply", headers=_auth(), json={
            "ops": [{"op": "unset_platform_field", "platform": "example-platform", "field": "weight"}],
        }).json()

        assert body["valid"] is True
        assert "weight:" not in _text(path)

    @pytest.mark.unit
    def test_删除不存在的字段时给出明确错误(self, tmp_path):
        client, path, _, _ = _client(tmp_path)

        response = client.post("/admin/config/apply", headers=_auth(), json={
            "ops": [{"op": "unset_platform_field", "platform": "example-platform", "field": "quota_period"}],
        })

        assert response.status_code == 400
        assert "没有字段" in response.json()["error"]["details"]["errors"][0]["message"]
        assert _text(path) == CONFIG

    @pytest.mark.unit
    def test_新增条目与覆盖可以放在同一次补丁里(self, tmp_path):
        """先新增再改字段：两条 op 依次作用在同一份内存文档上"""
        client, path, _, _ = _client(tmp_path)

        body = client.post("/admin/config/apply", headers=_auth(), json={
            "ops": [
                {"op": "add_model", "platform": "example-platform", "model": "model-new"},
                {"op": "set_model_field", "platform": "example-platform", "model": "model-new",
                 "field": "retry_count", "value": 2},
            ],
        }).json()

        assert body["valid"] is True
        text = _text(path)
        assert '- name: "model-new"' in text
        assert "retry_count: 2" in text

    @pytest.mark.unit
    def test_retry_count写0会被拒绝(self, tmp_path):
        """0 会让重试分支没有兜底赋值，非流式请求变成 500：界面与接口都要拦住"""
        client, path, _, _ = _client(tmp_path)

        response = client.post("/admin/config/apply", headers=_auth(), json={
            "ops": [{"op": "set_platform_field", "platform": "example-platform",
                     "field": "retry_count", "value": 0}],
        })

        assert response.status_code == 400
        detail = response.json()["error"]["details"]
        assert "只试一次请写 1" in detail["errors"][0]["message"]
        assert _text(path) == CONFIG

    @pytest.mark.unit
    def test_别名不存在时同一条op就能把它建出来(self, tmp_path):
        """界面上的「新增别名」用的就是 set_alias_selectors：不需要额外的 op"""
        client, path, reloaded, _ = _client(tmp_path)

        body = client.post("/admin/config/apply", headers=_auth(), json={
            "ops": [{"op": "set_alias_selectors", "alias": "cheap", "selectors": ["example-platform|model-a"]}],
        }).json()

        assert body["valid"] is True
        text = _text(path)
        assert "all_aliases:" in text
        assert "cheap:" in text
        assert '- "example-platform|model-a"' in text
        assert reloaded == [True]

    @pytest.mark.unit
    def test_新建的别名可以接着改指向(self, tmp_path):
        client, path, _, _ = _client(tmp_path)

        client.post("/admin/config/apply", headers=_auth(), json={
            "ops": [{"op": "set_alias_selectors", "alias": "cheap", "selectors": ["example-platform|model-a"]}],
        })
        version = client.post("/admin/config/document", headers=_auth()).json()["version"]

        body = client.post("/admin/config/apply", headers=_auth(), json={
            "ops": [{"op": "set_alias_selectors", "alias": "cheap", "selectors": ["all"]}],
            "version": version,
        }).json()

        assert body["valid"] is True
        text = _text(path)
        assert '- "all"' in text
        assert "example-platform|model-a" not in text

    @pytest.mark.unit
    def test_指向不存在平台的别名会被警告(self, tmp_path):
        """别名写错平台名永远不会命中：预演和写盘都要把这件事说出来"""
        client, path, _, _ = _client(tmp_path)

        plan = client.post("/admin/config/plan", headers=_auth(), json={
            "ops": [{"op": "set_alias_selectors", "alias": "cheap", "selectors": ["ghost-platform"]}],
        }).json()
        assert any("不存在" in issue["message"] for issue in plan["warnings"])

        # 只是警告：配置还能用，所以允许写盘
        body = client.post("/admin/config/apply", headers=_auth(), json={
            "ops": [{"op": "set_alias_selectors", "alias": "cheap", "selectors": ["ghost-platform"]}],
        }).json()

        assert body["valid"] is True
        assert any("不存在" in issue["message"] for issue in body["warnings"])
        assert "ghost-platform" in _text(path)


class TestRollbackAndReload:
    """回滚与手动重载"""

    @pytest.mark.unit
    def test_回滚恢复到改动前(self, tmp_path):
        client, path, reloaded, _ = _client(tmp_path)

        applied = client.post("/admin/config/apply", headers=_auth(), json={
            "ops": [{"op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 3}],
        }).json()

        rolled = client.post("/admin/config/rollback", headers=_auth(), json={"name": applied["backup"]})

        assert rolled.status_code == 200
        assert _text(path) == CONFIG
        assert rolled.json()["version_after"] == applied["version_before"]
        assert reloaded == [True, True]

    @pytest.mark.unit
    def test_回滚不存在的备份返回500(self, tmp_path):
        client, _, _, _ = _client(tmp_path)

        response = client.post("/admin/config/rollback", headers=_auth(), json={"name": "models.yaml.19700101T000000Z.bak"})

        assert response.status_code == 500

    @pytest.mark.unit
    def test_reload接口可以单独调用(self, tmp_path):
        client, _, reloaded, _ = _client(tmp_path)

        response = client.post("/admin/config/reload", headers=_auth())

        assert response.status_code == 200
        assert response.json()["reloaded"] is True
        assert reloaded == [True]


class TestWiring:
    """服务层的装配"""

    @pytest.mark.unit
    def test_build_config_service把热重载与审计接到审计管理器(self, tmp_path):
        config_path = tmp_path / "models.yaml"
        config_path.write_text(CONFIG, encoding="utf-8")
        service = OpenAIProxyService(config_file=str(config_path))

        built = service.build_config_service()

        assert built is service.build_config_service()
        assert built.reload_callback == service._reload_models_blocking
        assert built.audit_recorder is not None

    @pytest.mark.unit
    def test_服务未启动时热重载返回需要重启而不是报错(self, tmp_path):
        config_path = tmp_path / "models.yaml"
        config_path.write_text(CONFIG, encoding="utf-8")
        service = OpenAIProxyService(config_file=str(config_path))

        outcome = service._reload_models_blocking()

        assert outcome["reloaded"] is False
        assert outcome["needs_restart"] is True
