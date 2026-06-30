import os
import json
import fnmatch
import yaml
import logging
from typing import Dict, List, Any
from openai_proxy.models import ModelConfig
from openai_proxy.core.plugin_manager import PluginManager

logger = logging.getLogger(__name__)


class ConfigLoader:
    """配置加载器 - 负责加载和解析配置文件"""

    def __init__(self, config_file: str = "models.yaml"):
        self.config_file = config_file
        self.plugin_manager = PluginManager()
        self.all_aliases: Dict[str, List[str]] = {}

    async def load_config(self) -> Dict[str, List[ModelConfig]]:
        """加载模型配置"""
        return await self._load_config(load_models=True)

    async def load_platforms_only(self) -> Dict[str, Any]:
        """
        仅加载平台列表（不加载模型）

        用于在爬虫完成前获取平台配置，避免重复加载。

        Returns:
            平台配置字典，键为平台名称，值为平台配置
        """
        return await self._load_config(load_models=False)

    async def _load_config(self, load_models: bool = True) -> Dict[str, List[ModelConfig]]:
        """加载模型配置"""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    if self.config_file.endswith('.yaml') or self.config_file.endswith('.yml'):
                        config_data = yaml.safe_load(f)
                    else:
                        config_data = json.load(f)

                # 解析新格式的配置
                raw_aliases = config_data.pop('all_aliases', {}) or {}
                if isinstance(raw_aliases, dict):
                    self.all_aliases = {
                        k: v if isinstance(v, list) else []
                        for k, v in raw_aliases.items()
                    }
                else:
                    self.all_aliases = {}
                models = {}
                for platform_name, platform_config in config_data.items():
                    if not isinstance(platform_config, dict):
                        continue

                    base_url = platform_config.get('baseUrl')
                    # 解析并替换配置中的环境变量占位符
                    config_api_key = platform_config.get('apiKey')
                    if isinstance(config_api_key, str):
                        resolved_api_key = self.plugin_manager.resolve_env_vars(config_api_key)
                    else:
                        resolved_api_key = config_api_key

                    # 优先从环境变量获取API密钥，格式为 {PLATFORM_NAME}_API_KEY
                    env_api_key = os.getenv(f"{platform_name.upper()}_API_KEY")
                    api_key = env_api_key if env_api_key else resolved_api_key

                    # 检查是否有插件配置
                    plugin_config = platform_config.get('plugin')
                    plugin_models_list = []
                    static_models_list = []

                    if load_models and plugin_config:
                        logger.info(f"平台 {platform_name} 检测到插件配置: {plugin_config.get('code')}")
                        # 使用插件动态获取模型列表
                        plugin_models_list = await self.plugin_manager.load_plugin_models(plugin_config)
                        logger.info(f"平台 {platform_name} 插件返回 {len(plugin_models_list)} 个模型")
                        # 获取静态配置的模型列表（如果有）
                        static_models_list = platform_config.get('models', [])
                        # 合并模型列表：插件模型在前，静态模型在后
                        models_list = plugin_models_list + static_models_list
                        logger.info(f"平台 {platform_name} 总共 {len(models_list)} 个模型 (插件: {len(plugin_models_list)}, 静态: {len(static_models_list)})")
                    elif plugin_config and not load_models:
                        # 不加载模型时，也需要创建插件实例（以便后续启动调度器）
                        logger.info(f"平台 {platform_name} 检测到插件配置，创建插件实例（不加载模型）")
                        # 创建插件实例但不调用 get_models
                        await self.plugin_manager.create_plugin_instance(plugin_config)
                        static_models_list = platform_config.get('models', [])
                        models_list = []
                    elif load_models:
                        # 使用静态配置的模型列表
                        static_models_list = platform_config.get('models', [])
                        models_list = static_models_list
                    else:
                        # 不加载模型，只记录平台存在
                        models_list = []

                    # 应用黑名单过滤
                    blacklist = platform_config.get('blacklist', [])
                    if blacklist and load_models and models_list:
                        filtered = [
                            m for m in models_list
                            if not any(fnmatch.fnmatch(m, p) for p in blacklist)
                        ]
                        removed = len(models_list) - len(filtered)
                        if removed > 0:
                            logger.info(f"平台 {platform_name} 黑名单过滤: 移除 {removed} 个模型")
                        models_list = filtered

                    timeout = platform_config.get('timeout', 30)
                    weight = platform_config.get('weight', 1)
                    enabled = platform_config.get('enabled', True)
                    quota_period = platform_config.get('quota_period')  # 支持 quota_period 配置
                    retry_count = platform_config.get('retry_count', 3)  # 单个模型重试次数

                    # 如果有插件配置且返回了模型，或者静态配置有模型，才添加该平台
                    # 注意：如果只有插件配置但返回空列表，仍然添加平台（让定时任务去填充）
                    has_plugin = plugin_config is not None
                    has_static_models = static_models_list and len(static_models_list) > 0
                    has_plugin_models = plugin_models_list and len(plugin_models_list) > 0

                    # 检查是否是爬虫模式（插件配置中包含 scrape_url）
                    is_scraper_mode = False
                    if plugin_config:
                        plugin_args = plugin_config.get('args', {})
                        is_scraper_mode = 'scrape_url' in plugin_args

                    # 跳过条件：既没有插件也没有静态模型，或者有插件但API密钥/base_url缺失
                    # 爬虫模式不需要 API 密钥
                    if not base_url:
                        logger.warning(f"跳过无效的平台配置（缺少base_url）: {platform_name}")
                        continue

                    # 非爬虫模式需要 API 密钥
                    if not is_scraper_mode and not api_key:
                        logger.warning(f"跳过无效的平台配置（缺少api_key）: {platform_name}")
                        continue

                    # 如果没有任何模型来源，但有插件配置，仍然创建平台（让定时任务填充）
                    if not has_plugin and not has_static_models:
                        logger.warning(f"跳过无效的平台配置（没有模型来源）: {platform_name}")
                        continue

                    # 当 load_models=False 时，存储平台配置而不是空列表
                    if load_models:
                        models[platform_name] = []
                        for model_name in models_list:
                            model_config = ModelConfig(
                                name=f"{platform_name}|{model_name.replace('/', '-')}",
                                api_key=api_key,
                                base_url=base_url,
                                model=model_name,
                                timeout=timeout,
                                weight=weight,
                                enabled=enabled,
                                quota_period=quota_period,
                                retry_count=retry_count
                            )
                            models[platform_name].append(model_config)
                    else:
                        # 存储平台配置信息，以便后续启动调度器
                        models[platform_name] = {
                            'baseUrl': base_url,
                            'apiKey': api_key,
                            'plugin': plugin_config,
                            'timeout': timeout,
                            'weight': weight,
                            'enabled': enabled,
                            'quota_period': quota_period,
                            'retry_count': retry_count
                        }

                # 只在加载模型时打印详细日志
                if load_models:
                    logger.info(f"成功加载配置文件: {self.config_file}")
                    logger.info(f"可用模型组: {list(models.keys())}")

                    # 打印每个平台的详细模型列表
                    for platform, platform_models in models.items():
                        model_names = [m.name for m in platform_models]
                        logger.info(f"平台 [{platform}] 共 {len(platform_models)} 个模型:")
                        for i, name in enumerate(model_names, 1):
                            logger.info(f"  {i}. {name}")

                    # 统计总模型数
                    total_models = sum(len(m) for m in models.values())
                    logger.info(f"总计加载 {total_models} 个模型")

                return models

            except Exception as e:
                logger.error(f"加载配置文件失败: {e}")
                raise  # 直接抛出异常，不再创建默认配置
        else:
            error_msg = f"配置文件不存在: {self.config_file}"
            logger.error(error_msg)
            raise FileNotFoundError(error_msg)
