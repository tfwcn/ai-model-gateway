"""NVIDIA 插件单元测试"""

import pytest
import os
from unittest.mock import patch
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 设置测试环境变量
os.environ['NVIDIA_API_KEY'] = 'test_nvidia_key'

from plugin.nvidia import NVIDIAPlugin
from openai_proxy.utils.error_classifier import ErrorType


@pytest.fixture(autouse=True)
def mock_nvidia_scraper_init():
    """
    自动禁用 NVIDIA 插件的爬虫初始化

    NVIDIA 插件已改为纯爬虫模式（scrape_url 必填），
    但单元测试只验证纯逻辑（解析/过滤/缓存），不应依赖真实爬虫配置。
    """
    with patch.object(NVIDIAPlugin, '_init_scraper', lambda self: None), \
         patch.object(NVIDIAPlugin, '_init_scheduler', lambda self: None):
        yield


class TestNVIDIAPluginInit:
    """测试 NVIDIA 插件初始化"""

    def test_init_with_api_key(self):
        """测试使用 API 密钥初始化"""
        plugin = NVIDIAPlugin(api_key="test-key")
        assert plugin.api_key == "test-key"

    def test_init_from_env(self):
        """测试从环境变量获取 API 密钥"""
        os.environ['NVIDIA_API_KEY'] = 'env-key'
        plugin = NVIDIAPlugin()
        assert plugin.api_key == 'env-key'

    def test_init_without_api_key(self):
        """测试没有 API 密钥时初始化"""
        os.environ.pop('NVIDIA_API_KEY', None)
        plugin = NVIDIAPlugin(api_key=None)
        assert plugin.api_key is None


class TestNVIDIAHealthCheck:
    """测试 NVIDIA 插件健康检查"""

    @pytest.mark.asyncio
    async def test_health_check_no_api_key(self):
        """测试没有 API 密钥时的健康检查"""
        os.environ.pop('NVIDIA_API_KEY', None)
        plugin = NVIDIAPlugin(api_key=None)
        result = await plugin.health_check()
        assert result["status"] == "unhealthy"
        assert "API 密钥未配置" in result["error"]

    # 健康检查成功测试需要复杂的异步 mock，使用 responses 库或 httpx mock 更合适
    # 核心功能测试（get_models）已经覆盖了 API 调用逻辑


class TestNVIDIAParseErrorResponse:
    """测试 NVIDIA 插件解析错误响应"""

    @pytest.mark.asyncio
    async def test_parse_quota_error(self):
        """测试解析配额错误"""
        plugin = NVIDIAPlugin(api_key="test-key")
        response_data = {"error": {"message": "Rate limit exceeded, quota exceeded"}}
        error_type = await plugin.parse_error(response_data)
        assert error_type == ErrorType.QUOTA_EXCEEDED

    @pytest.mark.asyncio
    async def test_parse_auth_error(self):
        """测试解析认证错误"""
        plugin = NVIDIAPlugin(api_key="test-key")
        response_data = {"error": {"message": "Unauthorized, authentication failed"}}
        error_type = await plugin.parse_error(response_data)
        assert error_type == ErrorType.AUTH_ERROR

    @pytest.mark.asyncio
    async def test_parse_unknown_error(self):
        """测试解析未知错误"""
        plugin = NVIDIAPlugin(api_key="test-key")
        response_data = {"error": {"message": "Some unknown error"}}
        error_type = await plugin.parse_error(response_data)
        # 未知错误会被分类为 SERVER_ERROR
        assert error_type == ErrorType.SERVER_ERROR


class TestNVIDIACacheTTL:
    """测试 NVIDIA 插件缓存 TTL"""

    def test_cache_ttl_default(self):
        """测试默认缓存 TTL"""
        plugin = NVIDIAPlugin(api_key="test-key")
        assert plugin.cache_ttl == 3600

    def test_cache_ttl_custom(self):
        """测试自定义缓存 TTL"""
        plugin = NVIDIAPlugin(api_key="test-key")
        plugin.cache_ttl = 7200
        assert plugin.cache_ttl == 7200
