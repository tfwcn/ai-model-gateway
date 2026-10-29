"""模型条目解析（model_entry）单元测试"""

import pytest

from openai_proxy.core.model_entry import ModelEntry


class TestModelEntryParse:
    """两种写法都要支持，且无效条目要能安全跳过"""

    @pytest.mark.unit
    def test_字符串写法只有模型名(self):
        entry = ModelEntry.parse("model-a")
        assert entry.name == "model-a"
        assert entry.overrides == {}

    @pytest.mark.unit
    def test_对象写法带模型级覆盖(self):
        entry = ModelEntry.parse({"name": "model-b", "first_chunk_timeout": 45, "connect_timeout": 3})
        assert entry.name == "model-b"
        assert entry.overrides == {"first_chunk_timeout": 45, "connect_timeout": 3}

    @pytest.mark.unit
    def test_模型名也接受model键(self):
        entry = ModelEntry.parse({"model": "m1", "timeout": 10})
        assert entry.name == "m1"
        assert entry.overrides == {"timeout": 10}

    @pytest.mark.unit
    def test_不支持的字段只告警不报错(self):
        entry = ModelEntry.parse({"name": "m1", "baseUrl": "https://evil/v1", "first_chunk_timeout": 5})
        assert entry.name == "m1"
        # 连接信息不允许在模型级别覆盖
        assert entry.overrides == {"first_chunk_timeout": 5}

    @pytest.mark.unit
    def test_无效条目返回None(self):
        assert ModelEntry.parse("") is None
        assert ModelEntry.parse("   ") is None
        assert ModelEntry.parse({"first_chunk_timeout": 5}) is None
        assert ModelEntry.parse(123) is None

    @pytest.mark.unit
    def test_parse_all过滤无效条目(self):
        entries = ModelEntry.parse_all(["a", {"name": "b", "timeout": 9}, "", None, 42])
        assert [e.name for e in entries] == ["a", "b"]
        assert entries[1].overrides == {"timeout": 9}

    @pytest.mark.unit
    def test_parse_all兼容非列表(self):
        assert ModelEntry.parse_all(None) == []
        assert ModelEntry.parse_all("a") == []


class TestModelEntryMerge:
    """插件模型与静态模型按名合并"""

    @pytest.mark.unit
    def test_同名模型只保留一个且静态覆盖优先(self):
        plugin = ModelEntry.parse_all(["m1", "m2"])
        static = ModelEntry.parse_all([{"name": "m1", "first_chunk_timeout": 30}])
        merged = ModelEntry.merge(plugin, static)

        assert [e.name for e in merged] == ["m1", "m2"]
        assert merged[0].overrides == {"first_chunk_timeout": 30}
        assert merged[1].overrides == {}

    @pytest.mark.unit
    def test_插件与静态模型名不同时全部保留(self):
        merged = ModelEntry.merge(ModelEntry.parse_all(["p1"]), ModelEntry.parse_all(["s1"]))
        assert [e.name for e in merged] == ["p1", "s1"]

    @pytest.mark.unit
    def test_合并后不会重复调用同一模型(self):
        plugin = ModelEntry.parse_all(["m1", "m1"])
        merged = ModelEntry.merge(plugin, [])
        assert len(merged) == 1