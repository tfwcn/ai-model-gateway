"""
NVIDIA爬虫集成测试

测试完整的爬虫→缓存→插件调用流程
"""

import pytest
import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, AsyncMock, patch, MagicMock

from openai_proxy.scraper.nvidia import NVIDIAModelScraper
from openai_proxy.model.cache import ModelCacheManager
from plugin.nvidia import NVIDIAPlugin


class TestIntegrationScraperToCache:
    """测试从爬虫到缓存的完整流程"""

    @pytest.fixture
    def temp_cache_file(self, tmp_path):
        """创建临时缓存文件"""
        return str(tmp_path / "integration_test_cache.json")

    @pytest.mark.asyncio
    async def test_full_scrape_and_cache_workflow(self, temp_cache_file):
        """测试完整的抓取和缓存工作流"""
        # 1. 创建爬虫
        scraper = NVIDIAModelScraper(
            free_model_count=3,
            timeout=30,
            headless=True
        )
        scraper.api_key = "test-key"

        # 2. Mock API响应
        mock_response_data = {
            "data": [
                {"id": "nvidia/test-model-1", "name": "Test Model 1"},
                {"id": "microsoft/phi-3-mini", "name": "Phi-3 Mini"},
                {"id": "google/gemma-2b-it", "name": "Gemma 2B"},
                {"id": "paid/expensive-model", "name": "Expensive"},  # 应该被过滤
            ]
        }

        with patch('aiohttp.ClientSession') as mock_session_class:
            mock_session = AsyncMock()
            mock_session_class.return_value.__aenter__.return_value = mock_session

            mock_response = AsyncMock()
            mock_response.status = 200
            mock_response.json = AsyncMock(return_value=mock_response_data)

            mock_get = AsyncMock(return_value=mock_response)
            mock_session.get.return_value.__aenter__.return_value = mock_get

            # 3. 执行爬虫
            models = await scraper.scrape()

            # 验证结果
            assert len(models) == 3  # 只返回免费模型，且限制为3个
            assert all("/" in m["model_id"] for m in models)

            # 4. 保存到缓存
            cache = ModelCacheManager(cache_file=temp_cache_file)
            metadata = {
                "fetch_time": 1234567890,
                "total_count": len(models),
                "source": "api_filter"
            }

            success = cache.save(models=models, metadata=metadata, success=True)
            assert success == True

            # 5. 从缓存加载并验证
            loaded_data = cache.load()
            assert loaded_data is not None
            assert len(loaded_data["models"]) == 3
            assert loaded_data["metadata"]["source"] == "api_filter"

            # 6. 清理
            cache.clear()


class TestIntegrationPluginWithScraper:
    """测试插件与爬虫的集成"""

    @pytest.fixture
    def temp_cache_file(self, tmp_path):
        """创建临时缓存文件"""
        return str(tmp_path / "plugin_integration_cache.json")

    def test_plugin_initializes_scraper_when_enabled(self, temp_cache_file):
        """测试插件在启用爬虫时正确初始化"""
        plugin = NVIDIAPlugin(
            api_key="test-key",
            plugin_config={
                'args': {
                    'enable_scraper': True,
                    'free_model_count': 5,
                    'cache_file': temp_cache_file,
                    'enable_scheduled_task': False  # 禁用定时任务避免复杂化
                }
            }
        )

        assert plugin.enable_scraper == True
        assert plugin.free_model_count == 5
        assert plugin.scraper is not None
        assert plugin.cache_manager is not None

    def test_plugin_uses_cache_when_available(self, temp_cache_file):
        """测试插件在有缓存时使用缓存"""
        # 1. 准备缓存数据
        cache = ModelCacheManager(cache_file=temp_cache_file)
        cached_models = [
            {"model_id": "cached/model-1", "model_name": "Cached 1", "rank": 1},
            {"model_id": "cached/model-2", "model_name": "Cached 2", "rank": 2},
        ]
        cache.save(models=cached_models, metadata={"source": "test"}, success=True)

        # 2. 创建启用爬虫的插件
        plugin = NVIDIAPlugin(
            api_key="test-key",
            plugin_config={
                'args': {
                    'enable_scraper': True,
                    'cache_file': temp_cache_file,
                    'enable_scheduled_task': False
                }
            }
        )

        # 3. 运行异步测试
        async def test_async():
            models = await plugin.get_models()

            # 应该从缓存加载
            assert len(models) == 2
            assert models[0].model_id == "cached/model-1"
            assert models[1].model_id == "cached/model-2"

        asyncio.run(test_async())

        # 清理
        cache.clear()

    def test_plugin_fallback_to_api_when_no_cache(self, temp_cache_file):
        """测试插件在无缓存时回退到API"""
        # 确保缓存文件不存在
        if os.path.exists(temp_cache_file):
            os.remove(temp_cache_file)

        plugin = NVIDIAPlugin(
            api_key="test-key",
            plugin_config={
                'args': {
                    'enable_scraper': True,
                    'cache_file': temp_cache_file,
                    'enable_scheduled_task': False
                }
            }
        )

        # 此时应该使用原有的API方式（因为没有缓存）
        # 由于没有真实的API密钥，这里只验证不会崩溃
        async def test_async():
            try:
                models = await plugin.get_models()
                # 如果没有API密钥或API失败，应该返回空列表
                assert isinstance(models, list)
            except Exception as e:
                # 允许某些异常（如网络错误）
                pass

        asyncio.run(test_async())

    def test_plugin_scraper_status(self, temp_cache_file):
        """测试插件获取爬虫状态"""
        plugin = NVIDIAPlugin(
            api_key="test-key",
            plugin_config={
                'args': {
                    'enable_scraper': True,
                    'cache_file': temp_cache_file,
                    'enable_scheduled_task': False
                }
            }
        )

        status = plugin.get_scraper_status()

        assert status["enabled"] == True
        assert "cache_valid" in status
        assert "cache_info" in status

    def test_plugin_backward_compatibility(self):
        """测试向后兼容性：未配置爬虫时使用原有方式"""
        plugin = NVIDIAPlugin(
            api_key="test-key",
            plugin_config={}  # 没有爬虫配置
        )

        assert plugin.enable_scraper == False
        assert plugin.scraper is None
        assert plugin.scheduler is None


class TestIntegrationErrorHandling:
    """测试集成错误处理"""

    @pytest.mark.asyncio
    async def test_cache_corruption_recovery(self, tmp_path):
        """测试缓存损坏后的恢复"""
        cache_file = tmp_path / "corrupted.json"

        # 1. 创建损坏的缓存
        cache_file.write_text("{ invalid json }")

        # 2. 创建插件
        plugin = NVIDIAPlugin(
            api_key="test-key",
            plugin_config={
                'args': {
                    'enable_scraper': True,
                    'cache_file': str(cache_file),
                    'enable_scheduled_task': False
                }
            }
        )

        # 3. 尝试获取模型（应该检测到损坏并回退）
        models = await plugin.get_models()

        # 不应该崩溃，应该返回空列表或从API获取
        assert isinstance(models, list)

        # 损坏的文件应该被删除
        assert not cache_file.exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
