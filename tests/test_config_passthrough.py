"""透传配置的管理台/CLI 补丁测试

覆盖：
- schema 校验：前缀、维度表达式、重复前缀
- 配置视图脱敏
- add/set/unset/remove 四个补丁操作
- 配置加载器把 passthrough 从平台列表里摘出来
"""
import pytest
import yaml

from openai_proxy.config_store import schema
from openai_proxy.config_store.document import ConfigDocument
from openai_proxy.config_store.editor import apply_patch
from openai_proxy.core.config_loader import ConfigLoader

_BASE = """
passthrough:
  example-service:
    upstream: "https://api.example.com/v1"
    apiKey: "${EXAMPLE_PLATFORM_API_KEY}"
    timeout: 300
    dimensions:
      model: "body.model"
      size: "body.size"
"""


def _document(text: str = _BASE) -> ConfigDocument:
    return ConfigDocument.from_text("models.yaml", text)


class TestSchema:
    """passthrough 段校验"""

    @pytest.mark.unit
    def test_合法配置没有错误(self):
        issues = schema.check_document(yaml.safe_load(_BASE))
        errors = [issue for issue in issues if issue.level == "error"]
        assert not errors

    @pytest.mark.unit
    def test_前缀必须proxy开头(self):
        config = yaml.safe_load(_BASE)
        config["passthrough"]["example-service"]["prefix"] = "/api/assets"
        errors = [issue for issue in schema.check_document(config) if issue.level == "error"]
        assert any("prefix" in issue.path for issue in errors)

    @pytest.mark.unit
    def test_维度表达式必须指明来源(self):
        config = yaml.safe_load(_BASE)
        config["passthrough"]["example-service"]["dimensions"]["size"] = "payload.size"
        errors = [issue for issue in schema.check_document(config) if issue.level == "error"]
        assert any("dimensions.size" in issue.path for issue in errors)

    @pytest.mark.unit
    def test_前缀不能重复(self):
        config = yaml.safe_load(_BASE)
        config["passthrough"]["another"] = {
            "upstream": "https://api.example.com/v1",
            "prefix": "/proxy/example-service",
        }
        errors = [issue for issue in schema.check_document(config) if issue.level == "error"]
        assert any("重复" in issue.message for issue in errors)


class TestDocument:
    """脱敏视图"""

    @pytest.mark.unit
    def test_视图不返回密钥内容(self):
        document = _document(_BASE.replace("${EXAMPLE_PLATFORM_API_KEY}", "super-secret"))

        view = document.view()
        assert "super-secret" not in str(view)
        route = view["passthrough"][0]
        assert route["name"] == "example-service"
        assert route["dimensions"] == {"model": "body.model", "size": "body.size"}
        assert route["apiKey"]["kind"] == "literal"
        assert route["apiKey"]["preview"] == "****"


class TestPatch:
    """补丁操作"""

    @pytest.mark.unit
    def test_新增修改删除透传路由(self):
        document = _document("")
        result = apply_patch(document, [{
            "op": "add_passthrough",
            "name": "example-service",
            "config": {
                "upstream": "https://api.example.com/v1",
                "apiKey": "${EXAMPLE_PLATFORM_API_KEY}",
                "dimensions": {"model": "body.model"},
            },
        }])
        assert result.valid
        assert result.document.passthrough["example-service"]["upstream"] == "https://api.example.com/v1"

        result = apply_patch(_document(result.document.dump()), [{
            "op": "set_passthrough_field",
            "name": "example-service",
            "field": "timeout",
            "value": 600,
        }])
        assert result.valid
        assert result.document.passthrough["example-service"]["timeout"] == 600

        result = apply_patch(_document(result.document.dump()), [{
            "op": "unset_passthrough_field",
            "name": "example-service",
            "field": "timeout",
        }])
        assert result.valid
        assert "timeout" not in result.document.passthrough["example-service"]

        result = apply_patch(_document(result.document.dump()), [{
            "op": "remove_passthrough",
            "name": "example-service",
        }])
        assert result.valid
        assert "example-service" not in result.document.passthrough

    @pytest.mark.unit
    def test_非法维度被补丁拒绝(self):
        result = apply_patch(_document(), [{
            "op": "set_passthrough_field",
            "name": "example-service",
            "field": "dimensions",
            "value": {"size": "payload.size"},
        }])

        assert not result.valid
        assert any("dimensions.size" in issue["path"] for issue in result.errors)


class TestLoader:
    """配置加载"""

    @pytest.mark.unit
    async def test_passthrough不被当成平台(self, tmp_path):
        path = tmp_path / "models.yaml"
        path.write_text(_BASE, encoding="utf-8")
        loader = ConfigLoader(str(path))

        snapshot = await loader.load_snapshot()

        assert snapshot.models == {}
        assert snapshot.passthrough["example-service"]["upstream"] == "https://api.example.com/v1"
