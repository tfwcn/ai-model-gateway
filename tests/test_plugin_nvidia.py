"""NVIDIA 插件单元测试"""

import pytest
import os
import asyncio
import time
from unittest.mock import patch, MagicMock, AsyncMock
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 设置测试环境变量
os.environ['NVIDIA_API_KEY'] = 'test_nvidia_key'

from plugin.nvidia import NVIDIAPlugin, NVIDIAModel
from openai_proxy.utils.error_classifier import ErrorType


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


class TestNVIDIACacheFunctions:
    """测试 NVIDIA 插件缓存功能"""

    def test_cache_valid(self):
        """测试有效缓存"""
        plugin = NVIDIAPlugin(api_key="test-key")
        plugin.models_cache = [NVIDIAModel(model_id="model1", model_name="Model 1")]
        plugin.last_cache_time = time.time()
        assert plugin._is_cache_valid()

    def test_cache_expired(self):
        """测试过期缓存"""
        plugin = NVIDIAPlugin(api_key="test-key")
        plugin.models_cache = [NVIDIAModel(model_id="model1", model_name="Model 1")]
        plugin.last_cache_time = time.time() - 7200  # 2 小时前
        assert not plugin._is_cache_valid()

    def test_cache_empty(self):
        """测试空缓存"""
        plugin = NVIDIAPlugin(api_key="test-key")
        assert not plugin._is_cache_valid()


class TestNVIDIAParseModelInfo:
    """测试 NVIDIA 插件解析模型信息"""

    def test_parse_basic_model(self):
        """测试解析基本模型信息"""
        plugin = NVIDIAPlugin(api_key="test-key")
        model_info = {"id": "model-1", "name": "Model 1"}
        model = plugin._parse_model_info(model_info)
        assert model.model_id == "model-1"
        assert model.model_name == "Model 1"
        assert model.capabilities == ["text"]

    def test_parse_model_with_context_length(self):
        """测试解析带上下文长度的模型"""
        plugin = NVIDIAPlugin(api_key="test-key")
        model_info = {"id": "model-1", "context_length": 4096}
        model = plugin._parse_model_info(model_info)
        assert model.context_window == 4096

    def test_parse_model_with_max_tokens(self):
        """测试解析带 max_tokens 的模型"""
        plugin = NVIDIAPlugin(api_key="test-key")
        model_info = {"id": "model-1", "max_tokens": 8192}
        model = plugin._parse_model_info(model_info)
        assert model.context_window == 8192

    def test_parse_model_with_capabilities(self):
        """测试解析带能力的模型"""
        plugin = NVIDIAPlugin(api_key="test-key")
        model_info = {"id": "model-1", "capabilities": ["text", "image"]}
        model = plugin._parse_model_info(model_info)
        assert model.capabilities == ["text", "image"]

    def test_parse_model_without_id(self):
        """测试解析没有 ID 的模型"""
        plugin = NVIDIAPlugin(api_key="test-key")
        model_info = {"name": "Model 1"}
        model = plugin._parse_model_info(model_info)
        assert model is None


class TestNVIDIATilterFreeModels:
    """测试 NVIDIA 插件过滤免费模型"""

    def test_filter_nvidia_models(self):
        """测试过滤 NVIDIA 官方模型"""
        plugin = NVIDIAPlugin(api_key="test-key")
        models = [
            NVIDIAModel(model_id="nvidia/llama-3.1-8b-instruct", model_name="Llama 3.1"),
            NVIDIAModel(model_id="other/model", model_name="Other")
        ]
        free_models = plugin._filter_free_models(models)
        assert len(free_models) == 1
        assert free_models[0].model_id == "nvidia/llama-3.1-8b-instruct"

    def test_filter_microsoft_phi_models(self):
        """测试过滤 Microsoft Phi 系列模型"""
        plugin = NVIDIAPlugin(api_key="test-key")
        models = [
            NVIDIAModel(model_id="microsoft/phi-3-mini", model_name="Phi-3"),
            NVIDIAModel(model_id="other/model", model_name="Other")
        ]
        free_models = plugin._filter_free_models(models)
        assert len(free_models) == 1
        assert free_models[0].model_id == "microsoft/phi-3-mini"

    def test_filter_google_gemma_models(self):
        """测试过滤 Google Gemma 系列模型"""
        plugin = NVIDIAPlugin(api_key="test-key")
        models = [
            NVIDIAModel(model_id="google/gemma-2b", model_name="Gemma 2B"),
            NVIDIAModel(model_id="other/model", model_name="Other")
        ]
        free_models = plugin._filter_free_models(models)
        assert len(free_models) == 1
        assert free_models[0].model_id == "google/gemma-2b"

    def test_filter_meta_llama_3_2_models(self):
        """测试过滤 Meta Llama 3.2 系列模型"""
        plugin = NVIDIAPlugin(api_key="test-key")
        models = [
            NVIDIAModel(model_id="meta/llama-3.2-1b", model_name="Llama 3.2 1B"),
            NVIDIAModel(model_id="other/model", model_name="Other")
        ]
        free_models = plugin._filter_free_models(models)
        assert len(free_models) == 1
        assert free_models[0].model_id == "meta/llama-3.2-1b"

    def test_filter_mistral_models(self):
        """测试过滤 Mistral 模型"""
        plugin = NVIDIAPlugin(api_key="test-key")
        models = [
            NVIDIAModel(model_id="mistralai/mistral-7b", model_name="Mistral 7B"),
            NVIDIAModel(model_id="other/model", model_name="Other")
        ]
        free_models = plugin._filter_free_models(models)
        assert len(free_models) == 1
        assert free_models[0].model_id == "mistralai/mistral-7b"

    def test_filter_cohere_models(self):
        """测试过滤 Cohere 模型"""
        plugin = NVIDIAPlugin(api_key="test-key")
        models = [
            NVIDIAModel(model_id="cohere/command-r-35b", model_name="Command R"),
            NVIDIAModel(model_id="other/model", model_name="Other")
        ]
        free_models = plugin._filter_free_models(models)
        assert len(free_models) == 1
        assert free_models[0].model_id == "cohere/command-r-35b"

    def test_filter_mixed_models(self):
        """测试过滤混合模型列表"""
        plugin = NVIDIAPlugin(api_key="test-key")
        models = [
            NVIDIAModel(model_id="nvidia/llama-3.1-8b", model_name="Llama 3.1"),
            NVIDIAModel(model_id="microsoft/phi-3-mini", model_name="Phi-3"),
            NVIDIAModel(model_id="google/gemma-2b", model_name="Gemma"),
            NVIDIAModel(model_id="paid/expensive-model", model_name="Expensive")
        ]
        free_models = plugin._filter_free_models(models)
        assert len(free_models) == 3

    def test_filter_empty_models(self):
        """测试过滤空模型列表"""
        plugin = NVIDIAPlugin(api_key="test-key")
        models = []
        free_models = plugin._filter_free_models(models)
        assert len(free_models) == 0

    def test_filter_no_matches(self):
        """测试没有匹配免费模型的情况"""
        plugin = NVIDIAPlugin(api_key="test-key")
        models = [
            NVIDIAModel(model_id="paid/model1", model_name="Paid 1"),
            NVIDIAModel(model_id="paid/model2", model_name="Paid 2")
        ]
        free_models = plugin._filter_free_models(models)
        assert len(free_models) == 0


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


class TestNVIDIAGetModels:
    """测试 NVIDIA 插件获取模型"""

    @pytest.mark.asyncio
    async def test_get_models_no_api_key(self):
        """测试没有 API 密钥时获取模型"""
        os.environ.pop('NVIDIA_API_KEY', None)
        plugin = NVIDIAPlugin(api_key=None)
        models = await plugin.get_models()
        assert models == []

    @pytest.mark.asyncio
    async def test_get_models_with_cache(self):
        """测试使用缓存获取模型"""
        plugin = NVIDIAPlugin(api_key="test-key")
        plugin.models_cache = [NVIDIAModel(model_id="cached-model", model_name="Cached Model")]
        plugin.last_cache_time = time.time()
        models = await plugin.get_models()
        assert len(models) == 1
        assert models[0].model_id == "cached-model"

    @pytest.mark.asyncio
    async def test_get_models_fetch_success(self):
        """测试成功获取模型"""
        plugin = NVIDIAPlugin(api_key="test-key")
        with patch.object(plugin, '_fetch_models_from_api', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = [
                NVIDIAModel(model_id="model1", model_name="Model 1"),
                NVIDIAModel(model_id="model2", model_name="Model 2")
            ]
            models = await plugin.get_models()
            assert len(models) == 2
            mock_fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_models_with_new_config(self):
        """测试使用新配置获取模型列表"""
        plugin = NVIDIAPlugin(api_key="test-key")
        
        plugin_config = {
            "args": {
                "model_list_method": "GET",
                "request_params": {"nim_type": "anim_type_preview"}
            }
        }
        
        mock_response = {
            'data': [
                {'id': 'model1', 'name': 'Model 1'},
                {'id': 'model2', 'name': 'Model 2'}
            ]
        }
        
        with patch.object(plugin, '_make_api_request', new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response
            
            result = await plugin.get_models(plugin_config)
            
            assert len(result) == 2
            assert result[0].model_id == 'model1'
            assert result[1].model_id == 'model2'

    @pytest.mark.asyncio
    async def test_get_models_fetch_error_with_cache(self):
        """测试获取失败但有缓存"""
        plugin = NVIDIAPlugin(api_key="test-key")
        plugin.models_cache = [NVIDIAModel(model_id="cached-model", model_name="Cached Model")]
        plugin.last_cache_time = time.time()
        
        with patch.object(plugin, '_fetch_models_from_api', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.side_effect = Exception("API error")
            models = await plugin.get_models()
            # 应该返回缓存数据
            assert len(models) == 1
            assert models[0].model_id == "cached-model"


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