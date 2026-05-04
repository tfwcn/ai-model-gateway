"""测试BasePlugin基类"""

import os
import pytest
from unittest.mock import AsyncMock, patch

from openai_proxy.core.base_plugin import BasePlugin


class TestPlugin(BasePlugin):
    """测试用插件实现"""
    
    async def get_models(self, plugin_config=None):
        return ["test-model-1", "test-model-2"]
    
    async def health_check(self, plugin_config=None):
        return {"status": "healthy", "response_time_ms": 10}


def test_base_plugin_init():
    """测试BasePlugin初始化"""
    plugin = TestPlugin(
        api_key="test-key",
        base_url="https://api.test.com",
        cache_ttl=600,
        plugin_config={'args': {'custom_param': 'custom-value'}}
    )
    
    assert plugin.api_key == "test-key"
    assert plugin.base_url == "https://api.test.com"
    assert plugin.cache_ttl == 600
    # 测试新的配置获取方式
    assert plugin.get_plugin_arg('custom_param') == 'custom-value'
    assert plugin.plugin_config == {'args': {'custom_param': 'custom-value'}}


def test_resolve_env_vars():
    """测试环境变量解析"""
    # 设置测试环境变量
    os.environ["TEST_VAR"] = "test-value"
    
    result = BasePlugin.resolve_env_vars("prefix-${TEST_VAR}-suffix")
    assert result == "prefix-test-value-suffix"
    
    # 测试不存在的环境变量
    result = BasePlugin.resolve_env_vars("prefix-${NONEXISTENT_VAR}-suffix")
    assert result == "prefix-${NONEXISTENT_VAR}-suffix"
    
    # 测试非字符串值
    result = BasePlugin.resolve_env_vars(123)
    assert result == 123


def test_parse_plugin_config():
    """测试插件配置解析"""
    plugin = TestPlugin()
    
    config = {
        "code": "test.plugin",
        "cache_timeout": 300,
        "custom_field": "${TEST_VAR}",
        "number_field": 42
    }
    
    # 设置测试环境变量
    os.environ["TEST_VAR"] = "resolved-value"
    
    parsed = plugin.parse_plugin_config(config)
    
    # code和cache_timeout应该被跳过
    assert "code" not in parsed
    assert "cache_timeout" not in parsed
    assert parsed["custom_field"] == "resolved-value"
    assert parsed["number_field"] == 42


def test_cache_validity():
    """测试缓存有效性"""
    plugin = TestPlugin(cache_ttl=10)
    
    # 初始状态：无缓存
    assert not plugin.is_cache_valid()
    
    # 添加缓存
    plugin.update_cache(["model1", "model2"])
    assert plugin.is_cache_valid()
    
    # 禁用缓存
    plugin.cache_ttl = 0
    assert not plugin.is_cache_valid()
    
    # 重新启用缓存
    plugin.cache_ttl = 10
    assert plugin.is_cache_valid()


def test_clear_cache():
    """测试清除缓存"""
    plugin = TestPlugin()
    plugin.update_cache(["model1", "model2"])
    
    assert len(plugin.models_cache) == 2
    assert plugin.last_cache_time > 0
    
    plugin.clear_cache()
    assert len(plugin.models_cache) == 0
    assert plugin.last_cache_time == 0


@pytest.mark.asyncio
async def test_make_api_request_success():
    """测试API请求成功"""
    plugin = TestPlugin(api_key="test-key")
    
    with patch('aiohttp.ClientSession') as mock_session:
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json.return_value = {"data": "test"}
        
        mock_session.return_value.__aenter__.return_value.get.return_value.__aenter__.return_value = mock_response
        
        result = await plugin._make_api_request("https://api.test.com/test")
        assert result == {"data": "test"}


@pytest.mark.asyncio
async def test_make_api_request_failure():
    """测试API请求失败"""
    plugin = TestPlugin(api_key="test-key")
    
    with patch('aiohttp.ClientSession') as mock_session:
        mock_response = AsyncMock()
        mock_response.status = 401
        mock_response.text.return_value = "Unauthorized"
        
        mock_session.return_value.__aenter__.return_value.get.return_value.__aenter__.return_value = mock_response
        
        with pytest.raises(Exception) as exc_info:
            await plugin._make_api_request("https://api.test.com/test")
        
        assert "HTTP 401: Unauthorized" in str(exc_info.value)


def test_build_model_list_request_get():
    """测试构建GET请求配置"""
    plugin = TestPlugin(base_url="https://api.test.com", api_key="test-key")
    
    plugin_config = {
        "args": {
            "model_list_method": "GET",
            "request_params": {"category": "free", "limit": 10}
        }
    }
    
    request_config = plugin._build_model_list_request(plugin_config)
    
    assert request_config["url"] == "https://api.test.com/models"
    assert request_config["method"] == "GET"
    assert request_config["params"] == {"category": "free", "limit": 10}
    assert request_config["json_data"] is None


def test_build_model_list_request_post():
    """测试构建POST请求配置"""
    plugin = TestPlugin(base_url="https://api.test.com")
    
    plugin_config = {
        "args": {
            "model_list_method": "POST",
            "request_body": {"filter": {"type": "chat"}},
            "model_list_headers": {"X-Custom-Header": "value"}
        }
    }
    
    request_config = plugin._build_model_list_request(plugin_config)
    
    assert request_config["url"] == "https://api.test.com/models"
    assert request_config["method"] == "POST"
    assert request_config["params"] is None
    assert request_config["json_data"] == {"filter": {"type": "chat"}}
    assert request_config["headers"]["X-Custom-Header"] == "value"


def test_build_model_list_request_custom_url():
    """测试自定义模型列表URL"""
    plugin = TestPlugin(base_url="https://api.test.com")
    
    plugin_config = {
        "args": {
            "model_list_url": "https://custom.api.com/v1/models",
            "model_list_method": "GET"
        }
    }
    
    request_config = plugin._build_model_list_request(plugin_config)
    
    assert request_config["url"] == "https://custom.api.com/v1/models"


def test_build_model_list_request_no_url_error():
    """测试未配置URL时抛出错误"""
    plugin = TestPlugin(base_url=None)
    
    plugin_config = {
        "args": {
            "model_list_method": "GET"
        }
    }
    
    with pytest.raises(ValueError, match="无法确定模型列表 URL"):
        plugin._build_model_list_request(plugin_config)


def test_validate_request_config_valid():
    """测试验证正确的配置"""
    plugin = TestPlugin(base_url="https://api.test.com")
    
    plugin_config = {
        "args": {
            "model_list_url": "https://api.test.com/models",
            "model_list_method": "GET",
            "request_params": {"category": "free"}
        }
    }
    
    warnings = plugin._validate_request_config(plugin_config)
    assert warnings == []


def test_validate_request_config_missing_url():
    """测试验证缺少URL的配置"""
    plugin = TestPlugin(base_url=None)
    
    plugin_config = {
        "args": {
            "model_list_method": "GET"
        }
    }
    
    warnings = plugin._validate_request_config(plugin_config)
    assert len(warnings) > 0
    assert any("未配置 model_list_url" in w for w in warnings)


def test_validate_request_config_post_without_body():
    """测试验证POST请求缺少body"""
    plugin = TestPlugin(base_url="https://api.test.com")
    
    plugin_config = {
        "args": {
            "model_list_method": "POST"
        }
    }
    
    warnings = plugin._validate_request_config(plugin_config)
    assert len(warnings) > 0
    assert any("POST 请求未配置 request_body" in w for w in warnings)