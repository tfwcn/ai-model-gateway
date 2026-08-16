"""OpenRouter 插件单元测试 - 新配置结构"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
import sys
import os
import time

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugin.openrouter import OpenRouterPlugin


class TestOpenRouterPluginInit:
    """测试 OpenRouterPlugin 初始化"""

    def test_init_with_defaults(self):
        """测试使用默认值初始化"""
        plugin = OpenRouterPlugin(api_key="test-key")

        assert plugin.api_key == "test-key"
        assert plugin.base_url == "https://openrouter.ai/api"
        assert plugin.cache_ttl == 300

    def test_init_with_custom_params(self):
        """测试使用自定义参数初始化"""
        plugin = OpenRouterPlugin(
            api_key="custom-key",
            base_url="https://custom.api.com",
            cache_ttl=600
        )

        assert plugin.api_key == "custom-key"
        assert plugin.base_url == "https://custom.api.com"
        assert plugin.cache_ttl == 600

    def test_init_without_api_key_warning(self):
        """测试未提供 API Key 时不抛出异常"""
        import os
        original_key = os.environ.pop('OPENROUTER_API_KEY', None)

        # 应该能正常初始化,不抛出异常
        plugin = OpenRouterPlugin()
        assert plugin.api_key is None

        # 恢复环境变量
        if original_key:
            os.environ['OPENROUTER_API_KEY'] = original_key


class TestGetModelsNewConfig:
    """测试使用新配置结构的 get_models()"""

    @pytest.mark.asyncio
    async def test_get_models_with_scraper(self):
        """测试使用爬虫获取模型列表（通过内存缓存）"""
        plugin = OpenRouterPlugin(
            api_key="test-key",
            scrape_url="https://openrouter.ai/models?max_price=0",
            plugin_config={'args': {'enable_tool_capability_test': False}}
        )

        # 模拟爬虫已完成并更新了内存缓存
        plugin.initial_scrape_completed = True
        from plugin.openrouter import OpenRouterModel
        plugin.models_cache = [
            OpenRouterModel(model_id='model1:free', model_name='Model 1'),
            OpenRouterModel(model_id='model2:free', model_name='Model 2')
        ]
        plugin.last_cache_time = time.time()

        plugin_config = {
            "args": {
                "scrape_url": "https://openrouter.ai/models?max_price=0",
                "max_models": 50
            }
        }

        result = await plugin.get_models(plugin_config)

        assert len(result) == 2
        assert result[0].model_id == 'model1:free'
        assert result[1].model_id == 'model2:free'

    @pytest.mark.asyncio
    async def test_get_models_uses_cache(self):
        """测试缓存命中时不发起请求"""
        plugin = OpenRouterPlugin(
            api_key="test-key",
            scrape_url="https://openrouter.ai/models?max_price=0",
            plugin_config={'args': {'enable_tool_capability_test': False}}
        )

        # 标记首次爬虫已完成
        plugin.initial_scrape_completed = True

        # 先填充缓存
        from plugin.openrouter import OpenRouterModel
        cached_models = [OpenRouterModel(model_id='cached:free', model_name='Cached')]
        plugin.update_cache(cached_models)

        plugin_config = {"args": {"scrape_url": "https://openrouter.ai/models?max_price=0"}}

        with patch('plugin.openrouter.OpenRouterModelScraper') as MockScraper:
            result = await plugin.get_models(plugin_config)

            # 应该使用缓存，不调用爬虫
            MockScraper.assert_not_called()
            assert len(result) == 1
            assert result[0].model_id == 'cached:free'

    @pytest.mark.asyncio
    async def test_get_models_no_scrape_url_returns_empty(self):
        """测试未配置爬虫URL时返回空列表"""
        plugin = OpenRouterPlugin(
            api_key="test-key",
            plugin_config={'args': {'enable_tool_capability_test': False, 'cache_file': 'data/test_openrouter_empty.json'}}
        )

        plugin_config = {"args": {}}

        result = await plugin.get_models(plugin_config)

        assert result == []
