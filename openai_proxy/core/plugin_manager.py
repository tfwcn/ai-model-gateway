import os
import re
import logging
from typing import List, Dict, Any
import importlib
import asyncio
import inspect

logger = logging.getLogger(__name__)

class PluginManager:
    """插件管理器 - 负责加载和执行插件"""

    def __init__(self):
        """初始化插件管理器"""
        self._plugins = {}  # 存储插件实例，key为平台名称

    @staticmethod
    def resolve_env_vars(value: str) -> str:
        """
        解析并替换字符串中的环境变量占位符 ${VAR_NAME}

        Args:
            value: 包含环境变量占位符的字符串

        Returns:
            替换后的字符串，如果环境变量不存在则保留原占位符
        """
        if not isinstance(value, str):
            return value

        def replace_match(match):
            var_name = match.group(1)
            return os.getenv(var_name, match.group(0))

        # 使用正则表达式匹配 ${VAR_NAME} 格式的占位符
        pattern = r'\$\{([^}]+)\}'
        return re.sub(pattern, replace_match, value)

    async def create_plugin_instance(self, plugin_config: Dict[str, Any]) -> Any:
        """
        创建插件实例（不获取模型列表）

        Args:
            plugin_config: 插件配置字典

        Returns:
            插件实例
        """
        try:
            plugin_code = plugin_config.get('code')
            if not plugin_code:
                logger.warning("插件配置缺少 code 字段")
                return None

            # 动态导入插件模块
            module = importlib.import_module(plugin_code)

            # 尝试不同的可能的类名格式
            plugin_module_name = plugin_code.split('.')[-1]
            possible_class_names = [
                plugin_module_name.capitalize() + 'Plugin',
                plugin_module_name.title().replace(' ', '') + 'Plugin',
                plugin_module_name.upper() + 'Plugin',
                'OpenRouterPlugin',
                'ModelScopePlugin',
                'NVIDIAPlugin',
            ]

            plugin_class = None
            for class_name in possible_class_names:
                if hasattr(module, class_name):
                    plugin_class = getattr(module, class_name)
                    break

            if plugin_class is None:
                logger.error(f"插件 {plugin_code} 缺少对应的插件类，尝试了以下类名: {possible_class_names}")
                return None

            # 从环境变量获取API密钥
            platform_name = plugin_module_name.upper()
            api_key = os.getenv(f"{platform_name}_API_KEY")

            # 获取缓存超时配置
            cache_timeout = plugin_config.get('cache_timeout', 300)

            # 检查是否已有插件实例，如果有则复用
            if plugin_module_name in self._plugins:
                logger.debug(f"复用已有的插件实例: {plugin_code}")
                return self._plugins[plugin_module_name]

            # 创建新的插件实例
            logger.debug(f"创建新的插件实例: {plugin_code}")
            logger.debug(f"plugin_config keys: {list(plugin_config.keys()) if plugin_config else 'None'}")
            if plugin_config and 'args' in plugin_config:
                logger.debug(f"plugin_config args keys: {list(plugin_config['args'].keys())}")
            logger.debug(f"调用 plugin_class with api_key={api_key is not None}, cache_ttl={cache_timeout}, plugin_config type={type(plugin_config)}")
            plugin_instance = plugin_class(api_key=api_key, cache_ttl=cache_timeout, plugin_config=plugin_config)
            # 存储插件实例
            self._plugins[plugin_module_name] = plugin_instance
            logger.info(f"✓ 插件实例 {plugin_code} 已创建")
            return plugin_instance

        except Exception as e:
            logger.error(f"创建插件实例 {plugin_code} 时发生错误: {e}")
            return None

    async def load_plugin_models(self, plugin_config: Dict[str, Any]) -> List[str]:
        """
        加载插件并获取模型列表

        Args:
            plugin_config: 插件配置字典

        Returns:
            模型ID列表
        """
        try:
            plugin_code = plugin_config.get('code')
            if not plugin_code:
                logger.warning("插件配置缺少 code 字段")
                return []

            # 动态导入插件模块
            module = importlib.import_module(plugin_code)

            # 获取API密钥（从环境变量或配置中）
            api_key = None

            # 尝试不同的可能的类名格式
            plugin_module_name = plugin_code.split('.')[-1]
            possible_class_names = [
                plugin_module_name.capitalize() + 'Plugin',  # 默认格式：modelscope -> ModelscopePlugin
                plugin_module_name.title().replace(' ', '') + 'Plugin',  # modelscope -> ModelScopePlugin
                plugin_module_name.upper() + 'Plugin',  # nvidia -> NVIDIAPlugin
                'OpenRouterPlugin',  # 特殊处理 OpenRouter
                'ModelScopePlugin',  # 特殊处理 ModelScope
                'NVIDIAPlugin',  # 特殊处理 NVIDIA
            ]

            plugin_class = None
            plugin_class_name = None

            for class_name in possible_class_names:
                if hasattr(module, class_name):
                    plugin_class = getattr(module, class_name)
                    plugin_class_name = class_name
                    break

            if plugin_class is None:
                logger.error(f"插件 {plugin_code} 缺少对应的插件类，尝试了以下类名: {possible_class_names}")
                return []

            # 从环境变量获取API密钥
            platform_name = plugin_module_name.upper()
            api_key = os.getenv(f"{platform_name}_API_KEY")

            # 获取缓存超时配置，默认300秒
            # 注意：cache_timeout 从插件配置顶层读取，不在 args 中
            cache_timeout = plugin_config.get('cache_timeout', 300)

            # 检查是否已有插件实例，如果有则复用（保留状态如 initial_scrape_completed）
            if plugin_module_name in self._plugins:
                logger.debug(f"复用已有的插件实例: {plugin_code}")
                plugin_instance = self._plugins[plugin_module_name]
            else:
                # 创建新的插件实例
                # 每个不同的 plugin_config 都会创建新实例，实现缓存隔离
                logger.debug(f"创建新的插件实例: {plugin_code}")
                plugin_instance = plugin_class(api_key=api_key, cache_ttl=cache_timeout, plugin_config=plugin_config)
                # 存储插件实例（使用模块名作为key）
                self._plugins[plugin_module_name] = plugin_instance

            # 调用异步方法
            models = await plugin_instance.get_models(plugin_config)

            # 将模型对象转换为字符串列表
            model_ids = []
            for model in models:
                if hasattr(model, 'model_id'):
                    model_ids.append(model.model_id)
                else:
                    model_ids.append(str(model))

            logger.info(f"插件 {plugin_code} 成功加载 {len(model_ids)} 个模型")
            return model_ids

        except Exception as e:
            logger.error(f"加载插件 {plugin_code} 时发生错误: {e}")
            return []

    def get_plugin(self, platform_name: str) -> Any:
        """
        获取指定平台的插件实例

        Args:
            platform_name: 平台名称（如 'nvidia', 'openrouter'）

        Returns:
            插件实例，如果不存在则返回 None
        """
        return self._plugins.get(platform_name)
