"""配置补丁单元测试 - 十种操作、注释保真、结构性约束

最看重的一条：改完写回后，文件里只有目标行发生变化，
行内注释与被注释掉的历史条目必须原样保留。
"""

import re

import pytest

from openai_proxy.config_store.document import ConfigDocument
from openai_proxy.config_store.editor import OPS, apply_patch

SAMPLE = """# 模型组别名配置
all_aliases:
  alias-a:
    # - "example-platform|retired-model"
    - "example-platform|model-a"
    - "2example|model-c"
example-platform:
  baseUrl: "https://api.example.com/v1"
  apiKey: "${EXAMPLE_PLATFORM_API_KEY}"
  timeout: 120
  weight: 10 # 优先级最高
  models:
    - "model-a"
    # - "model-legacy"
    - name: "model-b"
      first_chunk_timeout: 45 # 首块预算
2example:
  baseUrl: "https://api.example2.com/v1"
  apiKey: "${2EXAMPLE_API_KEY}"
  models:
    - "model-c"
scraper-platform:
  baseUrl: "https://api.scraper.example.com/v1"
  apiKey: "${SCRAPER_PLATFORM_API_KEY}"
  plugin:
    code: "plugin.example-scraper"
    args:
      scrape_url: "https://models.example.com/list"
"""


# 别名条目之间夹着注释的夹具：覆盖整段时这类注释保不住，必须报告
ALIAS_SAMPLE = """all_aliases:
  alias-a:
    - "example-platform|model-a"
    # - "example-platform|retired-model"
    - "2example|model-c"
example-platform:
  baseUrl: "https://api.example.com/v1"
  apiKey: "${EXAMPLE_PLATFORM_API_KEY}"
  models:
    - "model-a"
2example:
  baseUrl: "https://api.example2.com/v1"
  apiKey: "${2EXAMPLE_API_KEY}"
  models:
    - "model-c"
"""


@pytest.fixture
def document():
    """内存中的示例文档（不落盘）"""
    return ConfigDocument.from_text("models.yaml", SAMPLE)


def normalized_lines(text: str) -> list:
    """
    按行归一化：ruamel 会保住注释所在的列，取值变短时行内注释前会多一个空格

    比较「改了哪几行」时不该为这种空白差异报错，所以只比注释列的空白。
    """
    return [re.sub(r"\s+#", " #", line) for line in text.splitlines()]


def changed_lines(result) -> list:
    """补丁后与原文件不同的行（只应包含目标行）"""
    return [b for a, b in zip(normalized_lines(SAMPLE), normalized_lines(result.document.dump())) if a != b]


def apply(document, *ops):
    return apply_patch(document, list(ops))


def lost_comments(result) -> list:
    """补丁顺带删掉的注释（这些必须如实报告，不能假装还在）"""
    return [issue["message"] for issue in result.warnings if "注释" in issue["message"]]


class TestBasics:
    """补丁的通用行为"""

    @pytest.mark.unit
    def test_十种操作都被支持(self):
        assert len(OPS) == 10

    @pytest.mark.unit
    def test_源文档不会被补丁改动(self, document):
        apply(document, {"op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 1})
        assert document.platform("example-platform")["weight"] == 10

    @pytest.mark.unit
    def test_不支持的操作给出可操作的错误(self, document):
        result = apply(document, {"op": "rename_platform", "platform": "example-platform"})
        assert not result.summary()["valid"]
        assert "不支持的操作" in result.errors[0]["message"]

    @pytest.mark.unit
    def test_摘要带上前后版本号(self, document):
        summary = apply(document, {"op": "set_audit_field", "field": "retention_days", "value": 45}).summary()
        assert summary["version_before"] == document.version
        assert summary["version_after"] != document.version


class TestPlatformFields:
    """平台级字段"""

    @pytest.mark.unit
    def test_改平台字段只改那一行且保留行内注释(self, document):
        result = apply(document, {"op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 3})
        summary = result.summary()

        assert summary["valid"]
        assert changed_lines(result) == ["  weight: 3 # 优先级最高"]
        assert summary["changes"][0]["before"] == 10
        assert summary["changes"][0]["after"] == 3

    @pytest.mark.unit
    def test_删除平台字段(self, document):
        result = apply(document, {"op": "unset_platform_field", "platform": "example-platform", "field": "timeout"})
        assert "timeout" not in result.document.platform("example-platform")
        assert result.summary()["valid"]

    @pytest.mark.unit
    def test_删除带行内注释的字段时会报告该注释(self, document):
        """字段连它行尾的注释一起没了：不报告的话用户会以为注释还在文件里"""
        result = apply(document, {"op": "unset_platform_field", "platform": "example-platform", "field": "weight"})

        assert result.summary()["valid"]
        assert '改动后这条注释消失了: 优先级最高' in [issue["message"] for issue in result.warnings]

    @pytest.mark.unit
    def test_改blacklist可以屏蔽模型(self, document):
        result = apply(document, {"op": "set_platform_field", "platform": "example-platform", "field": "blacklist", "value": ["model-a"]})
        assert result.summary()["valid"]
        assert result.document.platform("example-platform")["blacklist"] == ["model-a"]

    @pytest.mark.unit
    def test_拒绝越界的取值(self, document):
        result = apply(document, {"op": "set_platform_field", "platform": "example-platform", "field": "timeout", "value": 99999})
        assert "必须在" in result.errors[0]["message"]

    @pytest.mark.unit
    def test_未知平台字段被拒绝(self, document):
        result = apply(document, {"op": "set_platform_field", "platform": "example-platform", "field": "notAField", "value": 1})
        assert "不支持的平台字段" in result.errors[0]["message"]

    @pytest.mark.unit
    def test_models与plugin必须走专用操作(self, document):
        result = apply(document, {"op": "set_platform_field", "platform": "example-platform", "field": "models", "value": ["x"]})
        assert "add_model" in result.errors[0]["message"]

        result = apply(document, {"op": "set_platform_field", "platform": "example-platform", "field": "plugin", "value": {}})
        assert "重启" in result.errors[0]["message"]

    @pytest.mark.unit
    def test_新增平台必须提供完整连接信息(self, document):
        result = apply(document, {"op": "add_platform", "platform": "new-platform", "config": {"weight": 1}})
        assert "baseUrl" in result.errors[0]["message"]

    @pytest.mark.unit
    def test_新增平台成功后可以在视图里看到(self, document):
        result = apply(document, {
            "op": "add_platform",
            "platform": "new-platform",
            "config": {"baseUrl": "https://api.new.example.com/v1", "apiKey": "${NEW_PLATFORM_API_KEY}", "models": ["model-x"]},
        })
        assert result.summary()["valid"]
        assert result.document.platform("new-platform")["models"] == ["model-x"]

    @pytest.mark.unit
    def test_删除平台后别名指向它的selector只告警(self, document):
        result = apply(document, {"op": "remove_platform", "platform": "2example"})
        assert result.summary()["valid"]
        assert result.document.platform("2example") is None


class TestModels:
    """模型条目"""

    @pytest.mark.unit
    def test_新增模型保持双引号风格(self, document):
        result = apply(document, {"op": "add_model", "platform": "example-platform", "model": "model-new"})
        assert '    - "model-new"' in result.document.dump()

    @pytest.mark.unit
    def test_新增模型带覆盖项时写成对象条目(self, document):
        result = apply(document, {"op": "add_model", "platform": "example-platform", "model": "model-new", "overrides": {"first_chunk_timeout": 30}})
        dump = result.document.dump()
        assert '    - name: "model-new"' in dump
        assert "      first_chunk_timeout: 30" in dump

    @pytest.mark.unit
    def test_删除模型时如实报告随之消失的注释(self, document):
        """挂在被删条目上的注释无法安全保留：必须报告它，且其余注释不能串到别的模型上"""
        result = apply(document, {"op": "remove_model", "platform": "example-platform", "model": "model-a"})
        dump = result.document.dump()

        assert '"model-a"' not in dump
        assert lost_comments(result) == ['该条目附带的注释已随改动删除: - "model-legacy"']
        # 别名段里被注释掉的历史条目不受影响
        assert '# - "example-platform|retired-model"' in dump
        # 相邻条目自己的行内注释还在原来的位置
        assert 'first_chunk_timeout: 45 # 首块预算' in dump

    @pytest.mark.unit
    def test_爬虫平台的模型不能删除只能加blacklist(self, document):
        result = apply(document, {"op": "remove_model", "platform": "scraper-platform", "model": "any-model"})
        message = result.errors[0]["message"]
        assert "blacklist" in message
        assert result.errors[0]["path"].endswith("blacklist")

    @pytest.mark.unit
    def test_字符串条目改字段时升级为对象条目(self, document):
        result = apply(document, {"op": "set_model_field", "platform": "example-platform", "model": "model-a", "field": "weight", "value": 5})
        dump = result.document.dump()
        assert '    - name: "model-a"' in dump
        assert "      weight: 5" in dump
        # 挂在旧条目上的注释保不住位置：宁可报告消失，也不写出「注释在解释新字段」的误导排版
        assert '# - "model-legacy"' not in dump
        assert lost_comments(result) == ['该条目附带的注释已随改动删除: - "model-legacy"']

    @pytest.mark.unit
    def test_模型级不允许覆盖连接信息(self, document):
        result = apply(document, {"op": "set_model_field", "platform": "example-platform", "model": "model-a", "field": "baseUrl", "value": "https://api.example.com/v1"})
        assert "平台统一提供" in result.errors[0]["message"]

    @pytest.mark.unit
    def test_覆盖项删光后退回字符串写法(self, document):
        result = apply(document, {"op": "unset_model_field", "platform": "example-platform", "model": "model-b", "field": "first_chunk_timeout"})
        dump = result.document.dump()

        assert '    - "model-b"' in dump
        # 退成字符串条目后没有可以挂注释的键，字段上的行内注释只能丢掉
        assert lost_comments(result) == ['该条目附带的注释已随改动删除: 首块预算']

    @pytest.mark.unit
    def test_删除不存在的模型条目会报错(self, document):
        result = apply(document, {"op": "remove_model", "platform": "example-platform", "model": "ghost"})
        assert "静态条目" in result.errors[0]["message"]


class TestAliases:
    """模型组别名"""

    @pytest.mark.unit
    def test_覆盖别名selector只换生效条目不碰注释掉的历史(self, document):
        """被注释掉的历史 selector 是用户留下的记录，覆盖生效列表时不该连带删除"""
        result = apply(document, {"op": "set_alias_selectors", "alias": "alias-a", "selectors": ["all"]})
        dump = result.document.dump()

        assert '    - "all"' in dump
        assert '# - "example-platform|retired-model"' in dump
        assert '    - "example-platform|model-a"' not in dump
        assert not lost_comments(result)

    @pytest.mark.unit
    def test_夹在条目之间的注释被覆盖时必须报告(self):
        """注释挂在被清掉的条目上时无法保留：补丁必须如实报告，不能假装还在"""
        document = ConfigDocument.from_text("models.yaml", ALIAS_SAMPLE)
        result = apply(document, {"op": "set_alias_selectors", "alias": "alias-a", "selectors": ["all"]})

        assert result.summary()["valid"]
        assert 'retired-model"' not in result.document.dump()
        assert any("注释消失了" in message for message in lost_comments(result))

    @pytest.mark.unit
    def test_非法selector被拒绝(self, document):
        result = apply(document, {"op": "set_alias_selectors", "alias": "alias-a", "selectors": ["example-platform||bad"]})
        assert not result.summary()["valid"]

    @pytest.mark.unit
    def test_空selector列表被拒绝(self, document):
        result = apply(document, {"op": "set_alias_selectors", "alias": "alias-a", "selectors": []})
        assert "非空数组" in result.errors[0]["message"]

    @pytest.mark.unit
    def test_新建别名段(self, document):
        result = apply(document, {"op": "set_alias_selectors", "alias": "alias-new", "selectors": ["example-platform|model-a"]})
        assert result.summary()["valid"]
        assert result.document.aliases["alias-new"] == ["example-platform|model-a"]


class TestAuditSection:
    """审计段"""

    @pytest.mark.unit
    def test_新建audit段(self, document):
        result = apply(document, {"op": "set_audit_field", "field": "retention_days", "value": 45})
        assert result.document.audit == {"retention_days": 45}
        assert "\naudit:\n  retention_days: 45" in result.document.dump()

    @pytest.mark.unit
    def test_未知审计字段被拒绝(self, document):
        result = apply(document, {"op": "set_audit_field", "field": "whatever", "value": 1})
        assert "不支持的审计字段" in result.errors[0]["message"]

    @pytest.mark.unit
    def test_审计字段取值越界被拒绝(self, document):
        result = apply(document, {"op": "set_audit_field", "field": "flush_interval", "value": 0})
        assert not result.summary()["valid"]


class TestAtomicity:
    """补丁的原子性"""

    @pytest.mark.unit
    def test_某一步失败时前面的改动也不写盘(self, document):
        result = apply(document,
                       {"op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 1},
                       {"op": "remove_model", "platform": "scraper-platform", "model": "any-model"})
        # 调用方只在 valid 为真时写盘，因此这份文档不会被使用
        assert not result.summary()["valid"]
        assert result.document.platform("example-platform")["weight"] == 1

    @pytest.mark.unit
    def test_补丁后的问题会同时给出补丁错误与文档校验结果(self, document):
        result = apply(document, {"op": "set_platform_field", "platform": "ghost", "field": "weight", "value": 1})
        assert any("ghost" in issue["message"] for issue in result.errors)

    @pytest.mark.unit
    def test_写回自检发现序列化结果与内存内容不一致时拒绝(self, document):
        """注释搬运出错通常表现为「读回来少一个模型」，自检必须拦住这种写盘"""
        from openai_proxy.config_store.editor import _serialize_check

        document.dump = lambda: "example-platform:\n  weight: 999\n"
        issues = _serialize_check(document)

        assert any("写回后内容与预期不一致" in issue.message for issue in issues)

    @pytest.mark.unit
    def test_写回自检发现格式不稳定时拒绝(self, document):
        """重复保存会得到不同结果（引号风格来回变）同样不能写盘"""
        from openai_proxy.config_store.editor import _serialize_check

        document.dump = lambda: SAMPLE.replace("timeout: 120\n", "timeout: 120 \n")
        issues = _serialize_check(document)

        assert any("格式不稳定" in issue.message for issue in issues)
