"""
配置加载器 - 负责读取配置文件并构建一份完整的运行时快照

这里不再长期持有配置状态：
- `load_snapshot()` 返回 `ConfigSnapshot`，调用方（ConfigManager）决定何时提交
- `load_config()` 只是取快照里的模型表，保留给只关心模型表的调用方
- `load_platforms_only()` 只返回平台结构，用于启动插件调度器

这样解析失败时不会把当前生效的配置改到一半。
"""
import fnmatch
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

import yaml

from openai_proxy.core.model_entry import ModelEntry
from openai_proxy.core.plugin_manager import PluginManager
from openai_proxy.models import ModelConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConfigSnapshot:
    """一份完整的运行时配置快照

    构建完成后只读；需要变化时由 `ConfigManager` 构建新快照整体替换。
    """

    models: Dict[str, List[ModelConfig]] = field(default_factory=dict)
    all_aliases: Dict[str, List[str]] = field(default_factory=dict)
    audit: Dict[str, Any] = field(default_factory=dict)
    passthrough: Dict[str, Any] = field(default_factory=dict)
    # 平台结构（含插件配置）：启动调度器与判断「是否需要重启」时使用
    platform_configs: Dict[str, Any] = field(default_factory=dict)
    # 配置文件内容的哈希，与配置管理接口的 version 语义一致
    version: str = ""

    @property
    def plugin_signature(self) -> Dict[str, Any]:
        """所有平台的 plugin 配置：结构变化无法靠进程内重载完全生效"""
        signature: Dict[str, Any] = {}
        for name, config in self.platform_configs.items():
            plugin = config.get("plugin") if isinstance(config, dict) else None
            signature[name] = plugin
        return signature


class ConfigLoader:
    """配置加载器 - 负责加载和解析配置文件"""

    def __init__(self, config_file: str = "models.yaml"):
        self.config_file = config_file
        self.plugin_manager = PluginManager()

    async def load_snapshot(self) -> ConfigSnapshot:
        """构建一份完整快照（模型、别名、audit、passthrough、平台结构）"""
        return await self._load(load_models=True)

    async def load_config(self) -> Dict[str, List[ModelConfig]]:
        """只取模型表（保留给只需要模型的调用方）"""
        return (await self.load_snapshot()).models

    async def load_platforms_only(self) -> Dict[str, Any]:
        """
        仅加载平台列表（不加载模型）

        用于在爬虫完成前获取平台配置，避免重复加载。

        Returns:
            平台配置字典，键为平台名称，值为平台配置
        """
        return (await self._load(load_models=False)).platform_configs

    async def _load(self, load_models: bool) -> ConfigSnapshot:
        """读取并解析配置，返回快照；任何异常都会向上抛出，由调用方保留旧快照"""
        text, config_data = self._read_config()

        raw_aliases = config_data.pop("all_aliases", {}) or {}
        # 顶层审计段：不属于平台配置
        raw_audit = config_data.pop("audit", {}) or {}
        # 顶层透传段：同样不是平台配置
        raw_passthrough = config_data.pop("passthrough", {}) or {}

        aliases = self._parse_aliases(raw_aliases)
        audit = raw_audit if isinstance(raw_audit, dict) else {}
        passthrough = raw_passthrough if isinstance(raw_passthrough, dict) else {}

        models: Dict[str, List[ModelConfig]] = {}
        platform_configs: Dict[str, Any] = {}

        for platform_name, platform_config in config_data.items():
            if not isinstance(platform_config, dict):
                continue

            base_url = platform_config.get("baseUrl")
            # 解析并替换配置中的环境变量占位符
            config_api_key = platform_config.get("apiKey")
            if isinstance(config_api_key, str):
                resolved_api_key = self.plugin_manager.resolve_env_vars(config_api_key)
            else:
                resolved_api_key = config_api_key

            # 优先从环境变量获取API密钥，格式为 {PLATFORM_NAME}_API_KEY
            env_api_key = os.getenv(f"{platform_name.upper()}_API_KEY")
            api_key = env_api_key if env_api_key else resolved_api_key

            # 检查是否有插件配置
            plugin_config = platform_config.get("plugin")
            plugin_models_list = []
            static_models_list = []
            model_entries: List[ModelEntry] = []

            if load_models and plugin_config:
                logger.info(f"平台 {platform_name} 检测到插件配置: {plugin_config.get('code')}")
                # 使用插件动态获取模型列表
                plugin_models_list = await self.plugin_manager.load_plugin_models(plugin_config)
                logger.info(f"平台 {platform_name} 插件返回 {len(plugin_models_list)} 个模型")
                # 获取静态配置的模型列表（如果有），按模型名合并去重（静态覆盖优先）
                static_models_list = platform_config.get("models", [])
                plugin_entries = ModelEntry.parse_all(plugin_models_list)
                static_entries = ModelEntry.parse_all(static_models_list)
                model_entries = ModelEntry.merge(plugin_entries, static_entries)
                logger.info(
                    f"平台 {platform_name} 总共 {len(model_entries)} 个模型 "
                    f"(插件: {len(plugin_entries)}, 静态: {len(static_entries)})"
                )
            elif plugin_config and not load_models:
                # 不加载模型时，也需要创建插件实例（以便后续启动调度器）
                logger.info(f"平台 {platform_name} 检测到插件配置，创建插件实例（不加载模型）")
                # 创建插件实例但不调用 get_models
                await self.plugin_manager.create_plugin_instance(plugin_config)
                static_models_list = platform_config.get("models", [])
                model_entries = []
            elif load_models:
                # 使用静态配置的模型列表
                static_models_list = platform_config.get("models", [])
                model_entries = ModelEntry.parse_all(static_models_list)
            else:
                # 不加载模型，只记录平台存在
                model_entries = []

            # 应用黑名单过滤
            blacklist = platform_config.get("blacklist", [])
            if blacklist and load_models and model_entries:
                filtered = [
                    entry for entry in model_entries
                    if not any(fnmatch.fnmatch(entry.name, p) for p in blacklist)
                ]
                removed = len(model_entries) - len(filtered)
                if removed > 0:
                    logger.info(f"平台 {platform_name} 黑名单过滤: 移除 {removed} 个模型")
                model_entries = filtered

            timeout = platform_config.get("timeout", 30)
            weight = platform_config.get("weight", 1)
            enabled = platform_config.get("enabled", True)
            quota_period = platform_config.get("quota_period")  # 支持 quota_period 配置
            retry_count = platform_config.get("retry_count", 3)  # 单个模型重试次数
            stream_probe_timeout = platform_config.get("stream_probe_timeout")  # 流式探测超时（可选）
            connect_timeout = platform_config.get("connect_timeout")  # 连接建立超时（可选）
            first_chunk_timeout = platform_config.get("first_chunk_timeout")  # 首块预算（可选）

            # 如果有插件配置且返回了模型，或者静态配置有模型，才添加该平台
            # 注意：如果只有插件配置但返回空列表，仍然添加平台（让定时任务去填充）
            has_plugin = plugin_config is not None
            has_static_models = static_models_list and len(static_models_list) > 0

            # 检查是否是爬虫模式（插件配置中包含 scrape_url）
            is_scraper_mode = False
            if plugin_config:
                plugin_args = plugin_config.get("args", {})
                is_scraper_mode = "scrape_url" in plugin_args

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

            # 平台结构同时用于「启动调度器」和「判断插件是否变化」
            platform_configs[platform_name] = {
                "baseUrl": base_url,
                "apiKey": api_key,
                "plugin": plugin_config,
                "blacklist": blacklist,
                "timeout": timeout,
                "weight": weight,
                "enabled": enabled,
                "quota_period": quota_period,
                "retry_count": retry_count,
                "stream_probe_timeout": stream_probe_timeout,
                "connect_timeout": connect_timeout,
                "first_chunk_timeout": first_chunk_timeout,
            }

            if load_models:
                models[platform_name] = []
                # 平台级默认参数；模型条目可按字段覆盖
                platform_settings = dict(
                    timeout=timeout,
                    weight=weight,
                    enabled=enabled,
                    quota_period=quota_period,
                    retry_count=retry_count,
                    stream_probe_timeout=stream_probe_timeout,
                    connect_timeout=connect_timeout,
                    first_chunk_timeout=first_chunk_timeout,
                )
                for entry in model_entries:
                    settings = {**platform_settings, **entry.overrides}
                    model_config = ModelConfig(
                        name=f"{platform_name}|{entry.name.replace('/', '-')}",
                        api_key=api_key,
                        base_url=base_url,
                        model=entry.name,
                        **settings,
                    )
                    models[platform_name].append(model_config)

        if load_models:
            logger.info(f"成功加载配置文件: {self.config_file}")
            logger.info(f"可用模型组: {list(models.keys())}")
            for platform, platform_models in models.items():
                model_names = [m.name for m in platform_models]
                logger.info(f"平台 [{platform}] 共 {len(platform_models)} 个模型:")
                for index, name in enumerate(model_names, 1):
                    logger.info(f"  {index}. {name}")
            logger.info(f"总计加载 {sum(len(items) for items in models.values())} 个模型")

        return ConfigSnapshot(
            models=models,
            all_aliases=aliases,
            audit=audit,
            passthrough=passthrough,
            platform_configs=platform_configs,
            version=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )

    # ---------- 内部 ----------

    def _read_config(self) -> Tuple[str, Dict[str, Any]]:
        """读取文件文本与解析结果；文件不存在或格式错误时抛出异常"""
        if not os.path.exists(self.config_file):
            error_msg = f"配置文件不存在: {self.config_file}"
            logger.error(error_msg)
            raise FileNotFoundError(error_msg)

        try:
            with open(self.config_file, "r", encoding="utf-8") as handle:
                text = handle.read()
            if self.config_file.endswith(".yaml") or self.config_file.endswith(".yml"):
                loaded = yaml.safe_load(text)
            else:
                loaded = json.loads(text)
        except Exception as exc:
            logger.error(f"加载配置文件失败: {exc}")
            raise

        return text, loaded if isinstance(loaded, dict) else {}

    @staticmethod
    def _parse_aliases(raw_aliases: Any) -> Dict[str, List[str]]:
        """别名段的形状归一化：非列表取值直接当作空列表"""
        if not isinstance(raw_aliases, dict):
            return {}
        return {
            key: value if isinstance(value, list) else []
            for key, value in raw_aliases.items()
        }
