"""运行时配置管理器测试 - 快照、原子替换与重启判定"""
import pytest

from openai_proxy.core.config_manager import ConfigManager
from openai_proxy.core.config_loader import ConfigLoader, ConfigSnapshot
from openai_proxy.model.failover import ModelFailoverManager
from openai_proxy.service import OpenAIProxyService


_BASE = """
all_aliases:
  example-alias:
    - "example-platform|model-a"
audit:
  retention_days: 30
passthrough:
  example-service:
    upstream: "https://api.example.com/v1"
example-platform:
  baseUrl: "https://api.example.com/v1"
  apiKey: "${EXAMPLE_PLATFORM_API_KEY}"
  models:
    - "model-a"
"""


def _write(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


class TestSnapshot:
    """加载器只构建快照，不把状态留在自己身上"""

    @pytest.mark.unit
    async def test_快照包含所有配置段(self, tmp_path, monkeypatch):
        path = tmp_path / "models.yaml"
        _write(path, _BASE)
        monkeypatch.setenv("EXAMPLE_PLATFORM_API_KEY", "example-key")

        loader = ConfigLoader(str(path))
        snapshot = await loader.load_snapshot()

        assert snapshot.models["example-platform"][0].model == "model-a"
        assert snapshot.all_aliases["example-alias"] == ["example-platform|model-a"]
        assert snapshot.audit["retention_days"] == 30
        assert snapshot.passthrough["example-service"]["upstream"] == "https://api.example.com/v1"
        assert snapshot.platform_configs["example-platform"]["plugin"] is None
        assert snapshot.version

    @pytest.mark.unit
    async def test_加载失败不会修改当前快照(self, tmp_path):
        path = tmp_path / "models.yaml"
        _write(path, _BASE)
        manager = ConfigManager(str(path))
        original = await manager.initialize()

        path.write_text("example-platform: [", encoding="utf-8")
        result = await manager.reload()

        assert result.reloaded is False
        assert result.snapshot is original
        assert manager.current is original


class TestReload:
    """重载结果与需要重启的判定"""

    @pytest.mark.unit
    async def test_只改模型时进程内生效(self, tmp_path):
        path = tmp_path / "models.yaml"
        _write(path, _BASE)
        manager = ConfigManager(str(path))
        await manager.initialize()

        _write(path, _BASE.replace('- "model-a"', '- "model-b"'))
        result = await manager.reload()

        assert result.reloaded
        assert result.needs_restart is False
        assert manager.current.models["example-platform"][0].model == "model-b"

    @pytest.mark.unit
    async def test_audit段变化要求重启(self, tmp_path):
        path = tmp_path / "models.yaml"
        _write(path, _BASE)
        manager = ConfigManager(str(path))
        await manager.initialize()

        _write(path, _BASE.replace("retention_days: 30", "retention_days: 45"))
        result = await manager.reload()

        assert result.reloaded
        assert result.needs_restart is True
        assert "audit" in result.message

    @pytest.mark.unit
    async def test_重启提示会一直保留到进程重启(self, tmp_path):
        """audit 改动提交后，后续只改模型的重载仍然要提示重启"""
        path = tmp_path / "models.yaml"
        _write(path, _BASE)
        manager = ConfigManager(str(path))
        await manager.initialize()

        _write(path, _BASE.replace("retention_days: 30", "retention_days: 45"))
        first = await manager.reload()
        assert first.needs_restart is True

        _write(path, (
            _BASE
            .replace("retention_days: 30", "retention_days: 45")
            .replace('- "model-a"', '- "model-b"')
        ))
        second = await manager.reload()

        assert second.reloaded is True
        assert second.needs_restart is True
        assert "audit" in second.message

    @pytest.mark.unit
    async def test_插件结构变化要求重启(self):
        class FakeLoader:
            """按顺序返回两份快照，避免真的去加载插件"""

            def __init__(self):
                self.snapshots = [
                    ConfigSnapshot(platform_configs={"p": {"plugin": {"code": "a"}}}),
                    ConfigSnapshot(platform_configs={"p": {"plugin": {"code": "b"}}}),
                ]
                self.plugin_manager = None

            async def load_snapshot(self):
                return self.snapshots.pop(0)

        manager = ConfigManager("unused.yaml", loader=FakeLoader())
        await manager.initialize()

        result = await manager.reload()

        assert result.needs_restart is True
        assert "插件" in result.message


class TestServiceIntegration:
    """服务层只使用 ConfigManager 的快照，不再自己拼装配置"""

    @pytest.mark.unit
    async def test_重载后模型表与别名一起替换(self, tmp_path, monkeypatch):
        path = tmp_path / "models.yaml"
        _write(path, _BASE)
        monkeypatch.setenv("EXAMPLE_PLATFORM_API_KEY", "example-key")

        service = OpenAIProxyService(str(path))
        service.failover_manager = ModelFailoverManager({})
        await service.config_manager.initialize()

        _write(path, _BASE.replace(
            '    - "model-a"',
            '    - "model-b"',
        ).replace(
            '"example-platform|model-a"',
            '"example-platform|model-b"',
        ))
        await service.model_list_cache.set("stale", {"data": "old"})

        result = await service.reload_models()

        assert result["reloaded"] is True
        assert service.models["example-platform"][0].model == "model-b"
        assert service.failover_manager.models["example-platform"][0].model == "model-b"
        assert service.failover_manager.all_aliases["example-alias"] == ["example-platform|model-b"]
        assert await service.model_list_cache.get("stale") is None

        await service.passthrough.close()
