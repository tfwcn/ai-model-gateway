"""插件管理器单元测试"""

import pytest
import os
from unittest.mock import patch, MagicMock, AsyncMock
import types
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai_proxy.core.plugin_manager import PluginManager


class TestPluginManagerResolveEnvVars:
    """测试 PluginManager.resolve_env_vars 静态方法"""

    def test_resolve_single_env_var(self):
        """测试解析单个环境变量"""
        os.environ['TEST_VAR'] = 'test_value'
        result = PluginManager.resolve_env_vars('${TEST_VAR}')
        assert result == 'test_value'

    def test_resolve_multiple_env_vars(self):
        """测试解析多个环境变量"""
        os.environ['VAR1'] = 'value1'
        os.environ['VAR2'] = 'value2'
        result = PluginManager.resolve_env_vars('${VAR1} and ${VAR2}')
        assert result == 'value1 and value2'

    def test_resolve_nonexistent_env_var(self):
        """测试解析不存在的环境变量 - 保留原占位符"""
        result = PluginManager.resolve_env_vars('${NONEXISTENT_VAR}')
        assert result == '${NONEXISTENT_VAR}'

    def test_resolve_mixed_env_vars(self):
        """测试混合存在和不存在的环境变量"""
        os.environ['EXIST_VAR'] = 'exist_value'
        result = PluginManager.resolve_env_vars('${EXIST_VAR} and ${NONEXIST_VAR}')
        assert result == 'exist_value and ${NONEXIST_VAR}'

    def test_resolve_no_env_vars(self):
        """测试没有环境变量的字符串"""
        result = PluginManager.resolve_env_vars('no variables here')
        assert result == 'no variables here'

    def test_resolve_empty_string(self):
        """测试空字符串"""
        result = PluginManager.resolve_env_vars('')
        assert result == ''

    def test_resolve_non_string_input(self):
        """测试非字符串输入"""
        result = PluginManager.resolve_env_vars(123)
        assert result == 123

        result = PluginManager.resolve_env_vars(None)
        assert result is None

    def test_resolve_env_var_with_default_syntax(self):
        """测试带默认值语法的环境变量（注意：当前实现不支持默认值语法）"""
        os.environ['TEST_VAR'] = 'test_value'
        # 当前实现只支持 ${VAR_NAME} 格式
        result = PluginManager.resolve_env_vars('${TEST_VAR}')
        assert result == 'test_value'


@pytest.mark.asyncio
class TestPluginManagerLoadPluginModels:
    """测试 PluginManager.load_plugin_models 异步方法"""

    async def test_load_plugin_success(self):
        """测试成功加载插件"""
        plugin_config = {
            'code': 'plugin.openrouter',
            'args': {'category': 'free'}
        }

        # 创建一个简单的 mock 插件类
        class MockPlugin:
            def __init__(self, api_key=None, cache_ttl=300, plugin_config=None):
                self.api_key = api_key
                self.cache_ttl = cache_ttl
                self.plugin_config = plugin_config or {}
            
            async def get_models(self, config):
                return ['model1:free', 'model2:free']
            
            async def health_check(self, config=None):
                return {"status": "healthy"}
            
            async def parse_error(self, response_data):
                return None

        with patch('importlib.import_module') as mock_import:
            # 使用真实的模块对象而不是 MagicMock
            mock_module = types.ModuleType('plugin.openrouter')
            mock_module.OpenRouterPlugin = MockPlugin
            mock_import.return_value = mock_module

            plugin_manager = PluginManager()
            result = await plugin_manager.load_plugin_models(plugin_config)

            assert len(result) == 2
            assert result == ['model1:free', 'model2:free']

    async def test_load_plugin_missing_code(self):
        """测试缺少 code 字段的配置"""
        plugin_config = {'args': {'category': 'free'}}

        plugin_manager = PluginManager()
        result = await plugin_manager.load_plugin_models(plugin_config)

        assert result == []

    async def test_load_plugin_empty_code(self):
        """测试 code 字段为空的配置"""
        plugin_config = {'code': '', 'args': {}}

        plugin_manager = PluginManager()
        result = await plugin_manager.load_plugin_models(plugin_config)

        assert result == []

    async def test_load_plugin_missing_plugin_class(self):
        """测试插件模块缺少插件类"""
        plugin_config = {
            'code': 'plugin.invalid',
            'args': {}
        }

        mock_module = MagicMock()
        # 模拟模块没有插件类
        delattr(mock_module, 'InvalidPlugin')

        with patch('importlib.import_module') as mock_import:
            mock_import.return_value = mock_module

            plugin_manager = PluginManager()
            result = await plugin_manager.load_plugin_models(plugin_config)

            assert result == []

    async def test_load_plugin_exception(self):
        """测试插件加载异常"""
        plugin_config = {
            'code': 'plugin.nonexistent',
            'args': {}
        }

        with patch('importlib.import_module') as mock_import:
            mock_import.side_effect = ImportError("Module not found")

            plugin_manager = PluginManager()
            result = await plugin_manager.load_plugin_models(plugin_config)

            assert result == []

    async def test_load_plugin_empty_return(self):
        """测试插件返回空列表"""
        plugin_config = {
            'code': 'plugin.openrouter',
            'args': {}
        }

        class MockPlugin:
            def __init__(self, api_key=None, cache_ttl=300, plugin_config=None):
                pass
            
            async def get_models(self, config):
                return []
            
            async def health_check(self, config=None):
                return {"status": "healthy"}
            
            async def parse_error(self, response_data):
                return None

        with patch('importlib.import_module') as mock_import:
            # 使用真实的模块对象而不是 MagicMock
            mock_module = types.ModuleType('plugin.openrouter')
            mock_module.OpenRouterPlugin = MockPlugin
            mock_import.return_value = mock_module

            plugin_manager = PluginManager()
            result = await plugin_manager.load_plugin_models(plugin_config)

            assert result == []
            assert isinstance(result, list)

    async def test_load_plugin_with_full_config(self):
        """测试使用完整配置加载插件"""
        plugin_config = {
            'code': 'plugin.openrouter',
            'args': {
                'category': 'programming',
                'input_modalities': 'text',
                'output_modalities': 'text',
                'cache_timeout': 3600
            }
        }

        class MockPlugin:
            def __init__(self, api_key=None, cache_ttl=300, plugin_config=None):
                pass
            
            async def get_models(self, config):
                return ['model1:free']
            
            async def health_check(self, config=None):
                return {"status": "healthy"}
            
            async def parse_error(self, response_data):
                return None

        with patch('importlib.import_module') as mock_import:
            # 使用真实的模块对象而不是 MagicMock
            mock_module = types.ModuleType('plugin.openrouter')
            mock_module.OpenRouterPlugin = MockPlugin
            mock_import.return_value = mock_module

            plugin_manager = PluginManager()
            result = await plugin_manager.load_plugin_models(plugin_config)

            assert len(result) == 1

    async def test_load_plugin_cache_timeout_from_top_level(self):
        """测试 cache_timeout 从顶层读取而非 args"""
        plugin_config = {
            'code': 'plugin.openrouter',
            'cache_timeout': 600,  # 应该在顶层
            'args': {
                'model_list_method': 'GET'
            }
        }

        # 使用一个列表来捕获 cache_ttl 参数
        captured_cache_ttl = []
        
        class MockPlugin:
            def __init__(self, api_key=None, cache_ttl=300, plugin_config=None):
                captured_cache_ttl.append(cache_ttl)
            
            async def get_models(self, config):
                return ['model1:free']
            
            async def health_check(self, config=None):
                return {"status": "healthy"}
            
            async def parse_error(self, response_data):
                return None

        with patch('importlib.import_module') as mock_import:
            # 使用真实的模块对象而不是 MagicMock
            mock_module = types.ModuleType('plugin.openrouter')
            mock_module.OpenRouterPlugin = MockPlugin
            mock_import.return_value = mock_module

            plugin_manager = PluginManager()
            result = await plugin_manager.load_plugin_models(plugin_config)

            assert len(captured_cache_ttl) == 1
            assert captured_cache_ttl[0] == 600

    async def test_load_plugin_cache_timeout_default_when_missing(self):
        """测试 cache_timeout 缺失时使用默认值 300"""
        plugin_config = {
            'code': 'plugin.openrouter',
            'args': {
                'model_list_method': 'GET'
            }
        }

        # 使用一个列表来捕获 cache_ttl 参数
        captured_cache_ttl = []
        
        class MockPlugin:
            def __init__(self, api_key=None, cache_ttl=300, plugin_config=None):
                captured_cache_ttl.append(cache_ttl)
            
            async def get_models(self, config):
                return ['model1:free']
            
            async def health_check(self, config=None):
                return {"status": "healthy"}
            
            async def parse_error(self, response_data):
                return None

        with patch('importlib.import_module') as mock_import:
            # 使用真实的模块对象而不是 MagicMock
            mock_module = types.ModuleType('plugin.openrouter')
            mock_module.OpenRouterPlugin = MockPlugin
            mock_import.return_value = mock_module

            plugin_manager = PluginManager()
            result = await plugin_manager.load_plugin_models(plugin_config)

            assert len(captured_cache_ttl) == 1
            assert captured_cache_ttl[0] == 300


class TestPluginManagerIntegration:
    """插件管理器集成测试"""

    def test_resolve_env_vars_in_config(self):
        """测试配置中的环境变量解析"""
        os.environ['API_KEY'] = 'test_api_key_123'

        config_with_env = '${API_KEY}'
        resolved = PluginManager.resolve_env_vars(config_with_env)

        assert resolved == 'test_api_key_123'