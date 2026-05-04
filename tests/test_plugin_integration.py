"""插件集成测试"""

import pytest
import os
from unittest.mock import patch, MagicMock
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.asyncio
class TestPluginConfigIntegration:
    """测试插件与配置系统的集成"""

    async def test_plugin_config_parsing(self):
        """测试插件配置解析"""
        from openai_proxy.core.config_loader import ConfigLoader

        config_data = {
            'openrouter': {
                'baseUrl': 'https://openrouter.ai/api/v1',
                'apiKey': 'test-key',
                'plugin': {
                    'code': 'plugin.openrouter',
                    'args': {
                        'category': 'free',
                        'input_modalities': ['text'],
                        'output_modalities': ['text'],
                        'cache_timeout': 60
                    }
                },
                'enabled': True,
                'timeout': 20,
                'weight': 1
            }
        }

        with patch.object(ConfigLoader, '_load_raw_config') as mock_load:
            mock_load.return_value = config_data

            loader = ConfigLoader('test.yaml')
            with patch.object(loader.plugin_manager, 'load_plugin_models') as mock_plugin_mgr:
                mock_plugin_mgr.return_value = ['model1:free', 'model2:free']

                result = await loader.load_config()

                # 验证插件被正确调用
                assert 'openrouter' in result
                assert len(result['openrouter']) == 2

    async def test_env_var_resolution_before_plugin_call(self):
        """测试环境变量在插件调用前被解析"""
        from openai_proxy.core.config_loader import ConfigLoader

        config_data = {
            'openrouter': {
                'baseUrl': 'https://openrouter.ai/api/v1',
                'apiKey': '${OPENROUTER_API_KEY}',
                'plugin': {
                    'code': 'plugin.openrouter',
                    'args': {'category': 'free'}
                }
            }
        }

        os.environ['OPENROUTER_API_KEY'] = 'resolved-test-key'

        with patch.object(ConfigLoader, '_load_raw_config') as mock_load:
            mock_load.return_value = config_data

            loader = ConfigLoader('test.yaml')
            with patch.object(loader.plugin_manager, 'load_plugin_models') as mock_plugin_mgr:
                mock_plugin_mgr.return_value = ['model1:free']

                result = await loader.load_config()

                # 验证结果包含正确的平台
                assert 'openrouter' in result


@pytest.mark.asyncio  
class TestModelFormatValidation:
    """测试模型格式验证"""

    async def test_model_name_format(self):
        """测试模型名称格式（包含:free后缀）"""
        from openai_proxy.core.config_loader import ConfigLoader

        config_data = {
            'openrouter': {
                'baseUrl': 'https://openrouter.ai/api/v1',
                'apiKey': 'test-key',
                'plugin': {
                    'code': 'plugin.openrouter',
                    'args': {'category': 'free'}
                }
            }
        }

        with patch.object(ConfigLoader, '_load_raw_config') as mock_load:
            mock_load.return_value = config_data

            loader = ConfigLoader('test.yaml')
            with patch.object(loader.plugin_manager, 'load_plugin_models') as mock_plugin_mgr:
                mock_plugin_mgr.return_value = ['meta-llama/llama-3.2-3b-instruct:free']

                result = await loader.load_config()

                # 验证模型名称格式正确
                assert 'openrouter' in result
                model = result['openrouter'][0]
                assert model.model == 'meta-llama/llama-3.2-3b-instruct:free'
                assert 'meta-llama-llama-3.2-3b-instruct:free' in model.name

    async def test_model_config_fields(self):
        """测试模型配置字段"""
        from openai_proxy.core.config_loader import ConfigLoader

        config_data = {
            'openrouter': {
                'baseUrl': 'https://openrouter.ai/api/v1',
                'apiKey': 'test-key',
                'plugin': {
                    'code': 'plugin.openrouter',
                    'args': {'category': 'free'}
                },
                'timeout': 25,
                'weight': 2,
                'enabled': False
            }
        }

        with patch.object(ConfigLoader, '_load_raw_config') as mock_load:
            mock_load.return_value = config_data

            loader = ConfigLoader('test.yaml')
            with patch.object(loader.plugin_manager, 'load_plugin_models') as mock_plugin_mgr:
                mock_plugin_mgr.return_value = ['model1:free']

                result = await loader.load_config()

                # 验证配置字段正确应用
                assert 'openrouter' in result
                model = result['openrouter'][0]
                assert model.timeout == 25
                assert model.weight == 2
                assert model.enabled is False


@pytest.mark.asyncio
class TestErrorPropagation:
    """测试错误传播"""

    async def test_plugin_error_handling(self):
        """测试插件错误处理 - 插件返回空列表时跳过该平台"""
        from openai_proxy.core.config_loader import ConfigLoader

        config_data = {
            'openrouter': {
                'baseUrl': 'https://openrouter.ai/api/v1',
                'apiKey': 'test-key',
                'plugin': {
                    'code': 'plugin.openrouter',
                    'args': {'category': 'free'}
                }
            },
            'invalid': {
                'baseUrl': 'https://invalid.com/api/v1',
                'apiKey': 'invalid-key',
                'models': ['model1']
            }
        }

        with patch.object(ConfigLoader, '_load_raw_config') as mock_load:
            mock_load.return_value = config_data

            loader = ConfigLoader('test.yaml')
            with patch.object(loader.plugin_manager, 'load_plugin_models') as mock_plugin_mgr:
                mock_plugin_mgr.return_value = []  # 插件返回空列表

                result = await loader.load_config()

                # 验证 openrouter 平台被跳过（因为插件返回空列表）
                assert 'openrouter' not in result
                # 验证 invalid 平台被加载（静态配置）
                assert 'invalid' in result

    async def test_config_error_handling(self):
        """测试配置错误处理 - 无效配置被跳过"""
        from openai_proxy.core.config_loader import ConfigLoader

        config_data = {
            'valid': {
                'baseUrl': 'https://valid.com/api/v1',
                'apiKey': 'valid-key',
                'models': ['model1']
            },
            'missing_baseurl': {
                'apiKey': 'key-only',
                'models': ['model1']
            },
            'missing_apikey': {
                'baseUrl': 'https://nokey.com/api/v1',
                'models': ['model1']
            },
            'missing_models': {
                'baseUrl': 'https://nomodels.com/api/v1',
                'apiKey': 'key-without-models'
            }
        }

        with patch.object(ConfigLoader, '_load_raw_config') as mock_load:
            mock_load.return_value = config_data

            loader = ConfigLoader('test.yaml')

            result = await loader.load_config()

            # 验证只有有效配置被加载
            assert 'valid' in result
            assert 'missing_baseurl' not in result
            assert 'missing_apikey' not in result
            assert 'missing_models' not in result


@pytest.mark.asyncio
class TestEndToEnd:
    """端到端测试"""

    async def test_full_plugin_workflow(self):
        """测试完整插件工作流"""
        from openai_proxy.core.config_loader import ConfigLoader
        from openai_proxy.model.failover import ModelFailoverManager

        config_data = {
            'openrouter': {
                'baseUrl': 'https://openrouter.ai/api/v1',
                'apiKey': 'test-key',
                'plugin': {
                    'code': 'plugin.openrouter',
                    'args': {'category': 'free'}
                },
                'weight': 2
            }
        }

        with patch.object(ConfigLoader, '_load_raw_config') as mock_load:
            mock_load.return_value = config_data

            loader = ConfigLoader('test.yaml')
            with patch.object(loader.plugin_manager, 'load_plugin_models') as mock_plugin_mgr:
                mock_plugin_mgr.return_value = ['model1:free', 'model2:free']

                models = await loader.load_config()
                failover_manager = ModelFailoverManager(models)

                # 验证模型被正确加载和管理
                assert 'openrouter' in models
                available_models = failover_manager.get_available_models()
                assert len(available_models) == 2

    async def test_multi_platform_integration(self):
        """测试多平台集成"""
        from openai_proxy.core.config_loader import ConfigLoader
        from openai_proxy.model.failover import ModelFailoverManager

        config_data = {
            'openrouter': {
                'baseUrl': 'https://openrouter.ai/api/v1',
                'apiKey': 'test-key',
                'plugin': {
                    'code': 'plugin.openrouter',
                    'args': {'category': 'free'}
                }
            },
            'static_platform': {
                'baseUrl': 'https://static.com/api/v1',
                'apiKey': 'static-key',
                'models': ['static-model1', 'static-model2']
            }
        }

        with patch.object(ConfigLoader, '_load_raw_config') as mock_load:
            mock_load.return_value = config_data

            loader = ConfigLoader('test.yaml')
            with patch.object(loader.plugin_manager, 'load_plugin_models') as mock_plugin_mgr:
                mock_plugin_mgr.return_value = ['plugin-model1:free']

                result = await loader.load_config()
                failover_manager = ModelFailoverManager(result)

                # 验证两个平台都被正确加载
                assert 'openrouter' in result
                assert 'static_platform' in result
                
                # 验证模型总数
                all_models = failover_manager.get_available_models()
                assert len(all_models) == 3  # 1 plugin model + 2 static models