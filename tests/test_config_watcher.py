"""配置文件监听测试 - 手改 models.yaml 后自动热重载"""
import asyncio

import pytest

from openai_proxy.core.config_manager import ConfigManager
from openai_proxy.core.config_watcher import ConfigFileWatcher


_BASE = """
example-platform:
  baseUrl: "https://api.example.com/v1"
  apiKey: "${EXAMPLE_PLATFORM_API_KEY}"
  models:
    - "model-a"
"""


async def _wait_until(check, timeout: float = 3.0) -> bool:
    """轮询等待条件成立，避免测试里写死 sleep 时间"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if check():
            return True
        await asyncio.sleep(0.02)
    return False


class TestConfigFileWatcher:
    """文件变化触发一次重载，无效文件保留旧配置"""

    @pytest.mark.unit
    async def test_文件变化后自动重载(self, tmp_path):
        path = tmp_path / "models.yaml"
        path.write_text(_BASE, encoding="utf-8")
        manager = ConfigManager(str(path))
        await manager.initialize()

        seen = []

        async def on_reloaded(result):
            seen.append(result)

        watcher = ConfigFileWatcher(manager, on_reloaded, interval=0.05, debounce=0.02)
        await watcher.start()
        try:
            path.write_text(_BASE.replace('- "model-a"', '- "model-b"'), encoding="utf-8")

            assert await _wait_until(lambda: bool(seen))
            assert manager.current.models["example-platform"][0].model == "model-b"
            assert seen[0].reloaded is True
        finally:
            await watcher.stop()

    @pytest.mark.unit
    async def test_管理接口跳过的版本不会被自动重载(self, tmp_path):
        path = tmp_path / "models.yaml"
        path.write_text(_BASE, encoding="utf-8")
        manager = ConfigManager(str(path))
        await manager.initialize()

        seen = []

        async def on_reloaded(result):
            seen.append(result)

        watcher = ConfigFileWatcher(manager, on_reloaded, interval=0.05, debounce=0.02)
        await watcher.start()
        try:
            # 模拟管理接口写盘后因为 needs_restart / reload=false 跳过热重载
            path.write_text(_BASE.replace('- "model-a"', '- "model-d"'), encoding="utf-8")
            manager.ignore_current_file()
            await asyncio.sleep(0.4)

            assert seen == []
            assert manager.current.models["example-platform"][0].model == "model-a"

            # 下一次真正的外部修改仍然会自动重载
            path.write_text(_BASE.replace('- "model-a"', '- "model-e"'), encoding="utf-8")
            assert await _wait_until(lambda: bool(seen))
            assert manager.current.models["example-platform"][0].model == "model-e"
        finally:
            await watcher.stop()

    @pytest.mark.unit
    async def test_无效文件不会覆盖当前配置(self, tmp_path):
        path = tmp_path / "models.yaml"
        path.write_text(_BASE, encoding="utf-8")
        manager = ConfigManager(str(path))
        await manager.initialize()
        original = manager.current

        seen = []

        async def on_reloaded(result):
            seen.append(result)

        watcher = ConfigFileWatcher(manager, on_reloaded, interval=0.05, debounce=0.02)
        await watcher.start()
        try:
            path.write_text("example-platform: [", encoding="utf-8")
            await asyncio.sleep(0.4)

            assert manager.current is original
            assert seen == []

            # 修好文件后仍然能继续自动重载
            path.write_text(_BASE.replace('- "model-a"', '- "model-c"'), encoding="utf-8")
            assert await _wait_until(lambda: bool(seen))
            assert manager.current.models["example-platform"][0].model == "model-c"
        finally:
            await watcher.stop()
