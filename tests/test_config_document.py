"""配置文档单元测试 - round-trip 保真、脱敏视图、环境变量状态

关键回归点：写回必须只改动需要改的行，用户手工维护的注释与格式不能被机器重写。
"""

import os
import re

import pytest

from openai_proxy.config_store.document import ConfigDocument, entry_name, model_selector

# 示例配置：覆盖行内注释、被注释掉的历史条目、引号风格、数字开头的环境变量名
SAMPLE = """# 模型组别名配置
all_aliases:
  alias-a:
    # - "example-platform|retired-model"
    - "example-platform|model-a"
    # - "second-platform|model-b"
    - "2example|model-c"
example-platform:
  baseUrl: "https://api.example.com/v1"
  apiKey: "${EXAMPLE_PLATFORM_API_KEY}"
  timeout: 120
  weight: 10 # 优先级最高
  connect_timeout: 5 # 连接建立超时
  models:
    - "model-a"
    - name: "model-b"
      first_chunk_timeout: 45 # 首块预算
2example:
  baseUrl: "https://api.example2.com/v1"
  apiKey: "${2EXAMPLE_API_KEY}"
  models:
    - "model-c"
"""


@pytest.fixture
def sample_file(tmp_path):
    """把示例配置写到临时文件"""
    path = tmp_path / "models.yaml"
    path.write_text(SAMPLE, encoding="utf-8")
    return str(path)


def _normalized(text: str) -> list:
    """按行归一化：ruamel 保住注释列，取值变短会让注释前多一个空格"""
    return [re.sub(r"\s+#", " #", line) for line in text.splitlines()]


class TestRoundTrip:
    """写回保真度"""

    @pytest.mark.unit
    def test_原样往返字节完全一致(self, sample_file):
        document = ConfigDocument.load(sample_file)
        assert document.dump() == SAMPLE

    @pytest.mark.unit
    def test_改一个字段只影响那一行且保留行内注释(self, sample_file):
        document = ConfigDocument.load(sample_file)
        document.platform("example-platform")["weight"] = 3

        before = _normalized(SAMPLE)
        after = _normalized(document.dump())
        changed = [b for a, b in zip(before, after) if a != b]

        # ruamel 会保住注释所在的列，取值变短只会让注释前多一个空格
        assert changed == ["  weight: 3 # 优先级最高"]
        assert "weight: 3 # 优先级最高" in "\n".join(_normalized(document.dump()))
        # 注释总数不变：被注释掉的历史条目不会被删除
        assert SAMPLE.count("#") == document.dump().count("#")

    @pytest.mark.unit
    def test_二次加载仍然稳定(self, sample_file):
        first = ConfigDocument.load(sample_file).dump()
        second = ConfigDocument.from_text(sample_file, first).dump()
        assert first == second

    @pytest.mark.unit
    def test_配置文件不存在时抛出可用于转404的异常(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ConfigDocument.load(str(tmp_path / "missing.yaml"))


class TestStructure:
    """结构访问"""

    @pytest.mark.unit
    def test_平台列表排除顶层保留段(self, sample_file):
        document = ConfigDocument.load(sample_file)
        assert set(document.platforms) == {"example-platform", "2example"}

    @pytest.mark.unit
    def test_数字开头的平台名可以正常访问(self, sample_file):
        document = ConfigDocument.load(sample_file)
        assert document.platform("2example")["apiKey"] == "${2EXAMPLE_API_KEY}"

    @pytest.mark.unit
    def test_两种模型写法都能定位(self, sample_file):
        document = ConfigDocument.load(sample_file)

        entry, position = document.find_model("example-platform", "model-a")
        assert isinstance(entry, str) and position == 0

        entry, position = document.find_model("example-platform", "model-b")
        assert entry["first_chunk_timeout"] == 45 and position == 1

        assert document.find_model("example-platform", "nope") == (None, -1)

    @pytest.mark.unit
    def test_条目名与selector的生成规则(self):
        assert entry_name("model-a") == "model-a"
        assert entry_name({"name": "group/model-b"}) == "group/model-b"
        # selector 里的 / 会被换成 -，与 ModelConfig.name 的规则一致
        assert model_selector("example-platform", "group/model-b") == "example-platform|group-model-b"


class TestView:
    """脱敏视图"""

    @pytest.mark.unit
    def test_视图不包含任何密钥内容(self, sample_file, monkeypatch):
        monkeypatch.setenv("EXAMPLE_PLATFORM_API_KEY", "super-secret-value")
        view = ConfigDocument.load(sample_file).view()

        assert "super-secret-value" not in str(view)
        platform = next(item for item in view["platforms"] if item["name"] == "example-platform")
        assert platform["apiKey"]["kind"] == "env_placeholder"
        assert platform["apiKey"]["env"] == "EXAMPLE_PLATFORM_API_KEY"
        assert platform["apiKey"]["env_set"] is True

    @pytest.mark.unit
    def test_裸串密钥只显示掩码(self, sample_file, monkeypatch):
        document = ConfigDocument.load(sample_file)
        document.platform("2example")["apiKey"] = "literal-secret"
        view = ConfigDocument.from_text(sample_file, document.dump()).view()

        platform = next(item for item in view["platforms"] if item["name"] == "2example")
        assert platform["apiKey"]["kind"] == "literal"
        assert platform["apiKey"]["preview"] == "****"
        assert "literal-secret" not in str(view)

    @pytest.mark.unit
    def test_视图列出可编辑字段与补丁操作(self, sample_file):
        editable = ConfigDocument.load(sample_file).view()["editable"]

        assert "first_chunk_timeout" in editable["platform_fields"]
        assert "baseUrl" in editable["platform_fields"]
        assert "models" not in editable["platform_fields"]
        assert "set_model_field" in editable["ops"]

    @pytest.mark.unit
    def test_模型条目带可直接使用的selector与覆盖项(self, sample_file):
        view = ConfigDocument.load(sample_file).view()
        models = next(item for item in view["platforms"] if item["name"] == "example-platform")["models"]

        assert models[0]["selector"] == "example-platform|model-a"
        assert models[1]["overrides"] == {"first_chunk_timeout": 45}


class TestEnvStatus:
    """环境变量状态（只报告存在性）"""

    @pytest.mark.unit
    def test_占位符与平台名约定都会被检查(self, sample_file, monkeypatch):
        monkeypatch.setenv("EXAMPLE_PLATFORM_API_KEY", "x")
        monkeypatch.delenv("EXAMPLE_PLATFORM2_API_KEY", raising=False)

        items = ConfigDocument.load(sample_file).env_status()
        by_env = {item["env"]: item for item in items}

        assert by_env["EXAMPLE_PLATFORM_API_KEY"]["set"] is True
        # 数字开头的环境变量名不能被拒绝，否则这类平台无法保存
        assert by_env["2EXAMPLE_API_KEY"]["set"] is False
        assert by_env["2EXAMPLE_API_KEY"]["source"] == "yaml_placeholder"

    @pytest.mark.unit
    def test_状态里不含密钥取值(self, sample_file, monkeypatch):
        monkeypatch.setenv("EXAMPLE_PLATFORM_API_KEY", "super-secret-value")
        assert "super-secret-value" not in str(ConfigDocument.load(sample_file).env_status())
