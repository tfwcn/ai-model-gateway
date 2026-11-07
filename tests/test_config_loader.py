"""配置加载器单元测试"""

import pytest
import os
import yaml
from unittest.mock import patch, MagicMock, mock_open
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai_proxy.core.config_loader import ConfigLoader


@pytest.mark.asyncio
class TestConfigLoaderLoadConfig:
    """测试 ConfigLoader.load_config 方法"""

    async def test_load_config_with_plugin(self):
        """测试加载带插件配置的模型配置"""
        config_data = {
            'openrouter': {
                'baseUrl': 'https://openrouter.ai/api/v1',
                'apiKey': '${OPENROUTER_API_KEY}',
                'plugin': {
                    'code': 'plugin.openrouter',
                    'cache_timeout': 300,
                    'args': {
                        'model_list_method': 'GET',
                        'request_params': {'max_price': 0}
                    }
                },
                'enabled': True,
                'timeout': 20,
                'weight': 1
            }
        }

        os.environ['OPENROUTER_API_KEY'] = 'test_api_key'

        with patch('builtins.open', mock_open(read_data=yaml.dump(config_data))):
            with patch('os.path.exists') as mock_exists:
                mock_exists.return_value = True

                loader = ConfigLoader('test.yaml')
                # Mock 实例的 plugin_manager
                with patch.object(loader.plugin_manager, 'load_plugin_models') as mock_plugin_mgr:
                    mock_plugin_mgr.return_value = ['model1:free', 'model2:free']

                    result = await loader.load_config()

                    assert 'openrouter' in result
                    assert len(result['openrouter']) == 2

    async def test_load_config_without_plugin(self):
        """测试加载不带插件的静态模型配置"""
        config_data = {
            'nvidia': {
                'baseUrl': 'https://integrate.api.nvidia.com/v1',
                'apiKey': 'test-key',
                'models': ['model1', 'model2'],
                'enabled': True,
                'timeout': 20,
                'weight': 1
            }
        }

        with patch('builtins.open', mock_open(read_data=yaml.dump(config_data))):
            with patch('os.path.exists') as mock_exists:
                mock_exists.return_value = True

                loader = ConfigLoader('test.yaml')
                result = await loader.load_config()

                assert 'nvidia' in result
                assert len(result['nvidia']) == 2
                assert result['nvidia'][0].model == 'model1'
                assert result['nvidia'][1].model == 'model2'

    async def test_load_config_with_mixed_models(self):
        """测试加载混合插件和静态模型配置"""
        config_data = {
            'openrouter': {
                'baseUrl': 'https://openrouter.ai/api/v1',
                'apiKey': 'test-key',
                'plugin': {
                    'code': 'plugin.openrouter',
                    'cache_timeout': 300,
                    'args': {
                        'model_list_method': 'GET'
                    }
                },
                'models': ['static-model'],
                'enabled': True
            }
        }

        with patch('builtins.open', mock_open(read_data=yaml.dump(config_data))):
            with patch('os.path.exists') as mock_exists:
                mock_exists.return_value = True

                loader = ConfigLoader('test.yaml')
                with patch.object(loader.plugin_manager, 'load_plugin_models') as mock_plugin_mgr:
                    mock_plugin_mgr.return_value = ['plugin-model:free']

                    result = await loader.load_config()

                    # 插件模型 + 静态模型
                    assert len(result['openrouter']) == 2
                    assert result['openrouter'][0].model == 'plugin-model:free'
                    assert result['openrouter'][1].model == 'static-model'

    async def test_load_config_env_var_resolution(self):
        """测试配置中环境变量解析"""
        os.environ['TEST_API_KEY'] = 'resolved_key_123'

        config_data = {
            'testplatform': {
                'baseUrl': 'https://test.api.com/v1',
                'apiKey': '${TEST_API_KEY}',
                'models': ['model1'],
                'enabled': True
            }
        }

        with patch('builtins.open', mock_open(read_data=yaml.dump(config_data))):
            with patch('os.path.exists') as mock_exists:
                mock_exists.return_value = True

                loader = ConfigLoader('test.yaml')
                result = await loader.load_config()

                assert 'testplatform' in result
                assert result['testplatform'][0].api_key == 'resolved_key_123'

    async def test_load_config_invalid_platform(self):
        """测试无效平台配置被跳过"""
        config_data = {
            'invalid_platform': {
                'baseUrl': None,  # 缺少 baseUrl
                'models': ['model1']
            }
        }

        with patch('builtins.open', mock_open(read_data=yaml.dump(config_data))):
            with patch('os.path.exists') as mock_exists:
                mock_exists.return_value = True

                loader = ConfigLoader('test.yaml')
                result = await loader.load_config()

                # 无效配置应被跳过
                assert 'invalid_platform' not in result

    async def test_load_config_empty_models(self):
        """测试空模型列表被跳过"""
        config_data = {
            'empty_platform': {
                'baseUrl': 'https://test.api.com/v1',
                'apiKey': 'test-key',
                'models': [],  # 空模型列表
                'enabled': True
            }
        }

        with patch('builtins.open', mock_open(read_data=yaml.dump(config_data))):
            with patch('os.path.exists') as mock_exists:
                mock_exists.return_value = True

                loader = ConfigLoader('test.yaml')
                result = await loader.load_config()

                # 空模型列表应被跳过
                assert 'empty_platform' not in result

    async def test_load_config_default_values(self):
        """测试配置默认值"""
        config_data = {
            'minimal_platform': {
                'baseUrl': 'https://test.api.com/v1',
                'apiKey': 'test-key',
                'models': ['model1']
                # 缺少 timeout, weight, enabled 等
            }
        }

        with patch('builtins.open', mock_open(read_data=yaml.dump(config_data))):
            with patch('os.path.exists') as mock_exists:
                mock_exists.return_value = True

                loader = ConfigLoader('test.yaml')
                result = await loader.load_config()

                model = result['minimal_platform'][0]
                assert model.timeout == 30  # 默认值
                assert model.weight == 1  # 默认值
                assert model.enabled is True  # 默认值

    async def test_model_level_overrides(self):
        """模型条目可写成对象，按字段覆盖平台级配置"""
        config_data = {
            'example-platform': {
                'baseUrl': 'https://api.example.com/v1',
                'apiKey': 'test-key',
                'timeout': 120,
                'retry_count': 2,
                'connect_timeout': 5,
                'first_chunk_timeout': 20,
                'models': [
                    'fast-model',
                    {
                        'name': 'slow-model',
                        'first_chunk_timeout': 45,
                        'connect_timeout': 8,
                        'retry_count': 1,
                    },
                ],
            }
        }

        with patch('builtins.open', mock_open(read_data=yaml.dump(config_data))):
            with patch('os.path.exists') as mock_exists:
                mock_exists.return_value = True

                loader = ConfigLoader('test.yaml')
                result = await loader.load_config()

                fast, slow = result['example-platform']
                # 字符串写法：完整继承平台级配置
                assert (fast.model, fast.first_chunk_timeout, fast.connect_timeout, fast.retry_count) == ('fast-model', 20, 5, 2)
                # 对象写法：逐字段覆盖，未覆盖的仍继承平台级
                assert (slow.model, slow.timeout) == ('slow-model', 120)
                assert slow.first_chunk_timeout == 45
                assert slow.connect_timeout == 8
                assert slow.retry_count == 1

    async def test_model_entry_rejects_connection_overrides(self):
        """模型级别不允许覆盖连接信息，baseUrl/apiKey 始终来自平台"""
        config_data = {
            'stable': {
                'baseUrl': 'https://real.api.com/v1',
                'apiKey': 'real-key',
                'models': [
                    {'name': 'm1', 'baseUrl': 'https://other.api.com/v1', 'apiKey': 'other-key', 'timeout': 45},
                ],
            }
        }

        with patch('builtins.open', mock_open(read_data=yaml.dump(config_data))):
            with patch('os.path.exists') as mock_exists:
                mock_exists.return_value = True

                loader = ConfigLoader('test.yaml')
                result = await loader.load_config()

                model = result['stable'][0]
                assert model.base_url == 'https://real.api.com/v1'
                assert model.api_key == 'real-key'
                assert model.timeout == 45

    async def test_plugin_and_static_models_deduplicated(self):
        """插件模型与静态模型同名时不重复，且静态覆盖生效"""
        config_data = {
            'openrouter': {
                'baseUrl': 'https://openrouter.ai/api/v1',
                'apiKey': 'test-key',
                'plugin': {'code': 'plugin.openrouter', 'args': {'model_list_method': 'GET'}},
                'models': [{'name': 'model1:free', 'first_chunk_timeout': 30}],
            }
        }

        with patch('builtins.open', mock_open(read_data=yaml.dump(config_data))):
            with patch('os.path.exists') as mock_exists:
                mock_exists.return_value = True

                loader = ConfigLoader('test.yaml')
                with patch.object(loader.plugin_manager, 'load_plugin_models') as mock_plugin_mgr:
                    mock_plugin_mgr.return_value = ['model1:free', 'model2:free']
                    result = await loader.load_config()

                models = result['openrouter']
                assert [m.model for m in models] == ['model1:free', 'model2:free']
                assert models[0].first_chunk_timeout == 30

    async def test_blacklist_applies_to_object_entries(self):
        """黑名单对对象写法的模型条目同样生效"""
        config_data = {
            'platform': {
                'baseUrl': 'https://test.api.com/v1',
                'apiKey': 'test-key',
                'models': [{'name': 'keep/model'}, {'name': 'drop/model'}],
                'blacklist': ['drop/*'],
            }
        }

        with patch('builtins.open', mock_open(read_data=yaml.dump(config_data))):
            with patch('os.path.exists') as mock_exists:
                mock_exists.return_value = True

                loader = ConfigLoader('test.yaml')
                result = await loader.load_config()

                assert [m.model for m in result['platform']] == ['keep/model']


class TestConfigLoaderIntegration:
    """配置加载器集成测试"""

    @pytest.mark.asyncio
    async def test_full_config_load(self):
        """测试完整配置加载流程"""
        config_data = {
            'settings': {
                'cache': {'enabled': True, 'ttl': 300}
            },
            'openrouter': {
                'baseUrl': 'https://openrouter.ai/api/v1',
                'apiKey': '${OPENROUTER_API_KEY}',
                'plugin': {
                    'code': 'plugin.openrouter',
                    'cache_timeout': 300,
                    'args': {
                        'model_list_method': 'GET',
                        'request_params': {'max_price': 0}
                    }
                },
                'timeout': 20,
                'weight': 1,
                'enabled': True
            },
            'nvidia': {
                'baseUrl': 'https://integrate.api.nvidia.com/v1',
                'apiKey': '${NVIDIA_API_KEY}',
                'models': ['nvidia-model1'],
                'timeout': 20,
                'weight': 1,
                'enabled': True
            }
        }

        os.environ['OPENROUTER_API_KEY'] = 'or_key'
        os.environ['NVIDIA_API_KEY'] = 'nv_key'

        with patch('builtins.open', mock_open(read_data=yaml.dump(config_data))):
            with patch('os.path.exists') as mock_exists:
                mock_exists.return_value = True

                loader = ConfigLoader('test.yaml')
                with patch.object(loader.plugin_manager, 'load_plugin_models') as mock_plugin_mgr:
                    mock_plugin_mgr.return_value = ['or-model:free']

                    # 测试模型配置加载
                    models = await loader.load_config()
                    assert 'openrouter' in models
                    assert 'nvidia' in models
                    assert len(models['openrouter']) == 1
                    assert len(models['nvidia']) == 1
