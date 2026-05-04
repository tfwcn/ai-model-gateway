"""ModelScope 平台插件 - 动态获取可用模型列表"""

import asyncio
import aiohttp
import logging
import os
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
import json
import time
from datetime import datetime, timezone

from openai_proxy.utils.error_classifier import ErrorClassifier, ErrorType
from openai_proxy.core.base_plugin import BasePlugin
from openai_proxy.scraper.modelscope import ModelScopeModelScraper
from openai_proxy.model.cache import ModelCacheManager
from openai_proxy.scraper.scheduled import ScheduledScraper
from openai_proxy.model.capability.cache import CapabilityCacheManager
from openai_proxy.model.capability.tester import ToolCapabilityTester

logger = logging.getLogger(__name__)


@dataclass
class ModelScopeModel:
    """ModelScope 模型信息"""
    model_id: str
    model_name: str
    context_window: Optional[int] = None
    capabilities: List[str] = None
    support_inference: Optional[str] = None  # ModelScope 免费模型标识字段

    def __post_init__(self):
        if self.capabilities is None:
            self.capabilities = ["text"]


class ModelScopePlugin(BasePlugin):
    """ModelScope 平台插件"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        cache_ttl: int = 3600,
        scrape_url: Optional[str] = None,
        max_models: int = 50,
        scraper_timeout: int = 60,
        headless: bool = True,
        plugin_config: Optional[Dict[str, Any]] = None,
        **kwargs
    ):
        """
        初始化 ModelScope 插件

        Args:
            api_key: API 密钥，如果为 None 则从环境变量获取
            base_url: API 基础URL，如果为 None 则使用默认值
            cache_ttl: 缓存有效期（秒），默认3600秒（1小时）
            scrape_url: 爬虫URL，用于从网页抓取免费模型
            max_models: 最大模型数量，默认50
            scraper_timeout: 爬虫超时时间（秒），默认60秒
            headless: 是否使用无头模式运行浏览器，默认True
            plugin_config: 完整的插件配置字典（可选）
            **kwargs: 其他插件特定参数
        """
        # 设置默认值
        if base_url is None:
            base_url = "https://modelscope.cn/api/v1"

        # 调用父类初始化
        super().__init__(
            api_key=api_key or os.getenv("MODELSCOPE_API_KEY"),
            base_url=base_url,
            cache_ttl=cache_ttl,
            plugin_config=plugin_config,
            **kwargs
        )

        # 爬虫配置 - 优先使用参数，其次使用 plugin_config
        self.scrape_url = scrape_url or self.get_plugin_arg('scrape_url')
        self.max_models = max_models if max_models != 50 else self.get_plugin_arg('max_models', 50)
        self.scraper_timeout = scraper_timeout if scraper_timeout != 60 else self.get_plugin_arg('scraper_timeout', 60)
        self.headless = self.get_plugin_arg('headless', headless)

        # 定时任务配置 - 使用统一的 get_plugin_arg 方法
        self.cache_file = self.get_plugin_arg('cache_file', 'data/modelscope_free_models.json')
        self.schedule_cron = self.get_plugin_arg('schedule_cron', '0 2 * * *')
        self.enable_scheduled_task = self.get_plugin_arg('enable_scheduled_task', True)

        # 工具调用能力测试配置 - 使用统一的 get_plugin_arg 方法
        self.enable_tool_capability_test = self.get_plugin_arg('enable_tool_capability_test', True)
        self.max_concurrent_tests = self.get_plugin_arg('max_concurrent_tests', 10)
        self.test_timeout_seconds = self.get_plugin_arg('test_timeout_seconds', 5)

        if not self.api_key and not self.scrape_url:
            logger.warning("ModelScope API 密钥和爬虫URL都未配置，插件将无法工作")

        # 初始化缓存管理器
        self.cache_manager = ModelCacheManager(cache_file=self.cache_file)
        
        # 初始化工具调用能力缓存管理器
        self.capability_cache = CapabilityCacheManager(cache_file="data/tool_capability.json")
        
        # 初始化工具调用能力测试器
        if self.api_key:
            self.capability_tester = ToolCapabilityTester(
                base_url="https://api-inference.modelscope.cn/v1",
                api_key=self.api_key,
                timeout=self.test_timeout_seconds
            )
        else:
            self.capability_tester = None
            logger.warning("API 密钥未配置，工具调用能力测试将不可用")

        # 初始化爬虫和调度器
        self.scraper: Optional[ModelScopeModelScraper] = None
        self.scheduler: Optional[ScheduledScraper] = None

        # 标记是否已完成首次爬虫任务
        self.initial_scrape_completed: bool = False

        self._init_scraper()
        if self.enable_scheduled_task:
            self._init_scheduler()

    def _init_scraper(self) -> None:
        """初始化ModelScope模型爬虫"""
        try:
            if not self.scrape_url:
                logger.warning("scrape_url 未配置，爬虫功能将不可用")
                return

            logger.info(f"✓ 使用配置的爬虫URL: {self.scrape_url}")

            self.scraper = ModelScopeModelScraper(
                scrape_url=self.scrape_url,
                max_models=self.max_models,
                timeout=self.scraper_timeout,
                headless=self.headless
            )
            logger.info(f"✓ ModelScope网页爬虫已初始化 (max_models={self.max_models})")
        except Exception as e:
            logger.error(f"初始化爬虫失败: {e}")
            raise

    def _init_scheduler(self) -> None:
        """初始化定时任务调度器"""
        try:
            async def scrape_task():
                """包装爬虫任务"""
                await self._run_scraper_and_cache()

            self.scheduler = ScheduledScraper(
                scrape_func=scrape_task,
                cron_expression=self.schedule_cron,
                run_on_start=True,
                timezone="Asia/Shanghai"
            )
            logger.info(f"✓ 定时调度器已初始化 (cron='{self.schedule_cron}')")
        except Exception as e:
            logger.error(f"初始化调度器失败: {e}")

    async def _run_scraper_and_cache(self) -> None:
        """执行爬虫并缓存结果"""
        if not self.scraper:
            logger.warning("爬虫未初始化")
            # 即使爬虫未初始化，也标记为已完成（避免无限等待）
            if not self.initial_scrape_completed:
                self.initial_scrape_completed = True
                logger.warning("爬虫未初始化，标记为已完成（降级模式）")
            return

        try:
            logger.info("开始执行爬虫任务...")
            models_data = await self.scraper.scrape()

            if models_data:
                # 转换为 ModelScopeModel 对象
                models = [
                    ModelScopeModel(
                        model_id=m['model_id'],
                        model_name=m.get('model_name', m['model_id']),
                        capabilities=["text"]
                    )
                    for m in models_data
                ]

                # 保存至缓存
                metadata = {
                    "fetch_time": time.time(),
                    "total_count": len(models),
                    "returned_count": len(models),
                    "source": "web_scraper"
                }

                success = self.cache_manager.save(
                    models=[m.__dict__ for m in models],
                    metadata=metadata,
                    success=True
                )

                if success:
                    logger.info(f"✓ 爬虫任务完成，缓存 {len(models)} 个模型")
                    # 更新内存缓存
                    self.update_cache(models)
                else:
                    logger.error("✗ 保存缓存失败")
            else:
                logger.warning("爬虫返回空结果")

            # 无论成功与否，都标记为已完成（避免无限等待）
            if not self.initial_scrape_completed:
                self.initial_scrape_completed = True
                if models_data:
                    logger.info("✓ 首次爬虫任务完成，后续将使用最新数据")
                else:
                    logger.warning("⚠ 首次爬虫任务完成但未获取到模型数据")

        except Exception as e:
            error_msg = f"爬虫任务异常: {type(e).__name__}: {e}"
            logger.error(error_msg, exc_info=True)

            # 记录错误
            self.cache_manager.save(
                models=[],
                metadata={"error": error_msg},
                success=False
            )

            # 即使失败，也标记为已完成（避免无限等待）
            if not self.initial_scrape_completed:
                self.initial_scrape_completed = True
                logger.warning("⚠ 首次爬虫任务失败，但已标记为完成以避免无限等待")
            raise

    async def health_check(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        健康检查

        Args:
            config: 插件配置

        Returns:
            健康检查结果
        """
        if not self.scrape_url:
            return {
                "status": "unhealthy",
                "error": "爬虫URL未配置",
                "response_time_ms": 0
            }

        # 简单检查爬虫URL是否可访问
        start_time = time.time()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.scrape_url, timeout=10) as response:
                    response_time = (time.time() - start_time) * 1000

                    if response.status == 200:
                        return {
                            "status": "healthy",
                            "response_time_ms": int(response_time),
                            "last_check": time.time()
                        }
                    else:
                        return {
                            "status": "unhealthy",
                            "error": f"HTTP {response.status}",
                            "response_time_ms": int(response_time)
                        }

        except asyncio.TimeoutError:
            return {
                "status": "timeout",
                "timeout_ms": 10000,
                "response_time_ms": int((time.time() - start_time) * 1000)
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "error": str(e),
                "response_time_ms": int((time.time() - start_time) * 1000)
            }

    async def get_models(self, plugin_config: Dict[str, Any] = None) -> List[ModelScopeModel]:
        """
        获取模型列表（仅通过爬虫方式）

        Args:
            plugin_config: 插件配置

        Returns:
            模型列表
        """
        plugin_config = plugin_config or {}

        # 验证配置
        warnings = self._validate_request_config(plugin_config)
        for warning in warnings:
            logger.warning(f"ModelScope 配置警告: {warning}")

        # 检查是否已完成首次爬虫
        if not self.initial_scrape_completed:
            logger.warning("首次爬虫任务尚未完成，尝试从缓存加载模型...")
            # 尝试从文件缓存加载（作为降级方案）
            if self.cache_manager.is_valid():
                try:
                    cache_data = self.cache_manager.load()
                    if cache_data and cache_data.get('models'):
                        cached_models = cache_data['models']
                        logger.info(f"✓ 从文件缓存加载 {len(cached_models)} 个免费模型")

                        # 转换为 ModelScopeModel 对象
                        models = [
                            ModelScopeModel(
                                model_id=m['model_id'],
                                model_name=m.get('model_name', m['model_id']),
                                capabilities=["text"]
                            )
                            for m in cached_models
                        ]
                        # 应用能力过滤
                        return await self._filter_by_capability(models)
                except Exception as e:
                    logger.error(f"从缓存加载模型失败: {e}")
            
            logger.warning("缓存中没有可用模型，返回空列表")
            return []

        # 从内存缓存获取模型（爬虫完成后已更新到内存）
        if self.models_cache and self.is_cache_valid():
            logger.debug(f"从内存缓存返回 {len(self.models_cache)} 个模型")
            # 应用能力过滤
            return await self._filter_by_capability(self.models_cache)

        # 如果内存缓存不可用，尝试从文件缓存加载（作为降级方案）
        if self.cache_manager.is_valid():
            try:
                cache_data = self.cache_manager.load()
                if cache_data and cache_data.get('models'):
                    cached_models = cache_data['models']
                    logger.info(f"✓ 从文件缓存加载 {len(cached_models)} 个免费模型（降级方案）")

                    # 转换为 ModelScopeModel 对象
                    models = [
                        ModelScopeModel(
                            model_id=m['model_id'],
                            model_name=m.get('model_name', m['model_id']),
                            capabilities=["text"]
                        )
                        for m in cached_models
                    ]

                    # 更新内部缓存
                    self.update_cache(models)
                    # 应用能力过滤
                    return await self._filter_by_capability(models)
            except Exception as e:
                logger.warning(f"从文件缓存加载失败: {e}")

        # 最后的降级方案：返回空列表
        logger.warning("无法获取模型列表，返回空列表")
        return []

    async def _filter_by_capability(self, models: List[ModelScopeModel]) -> List[ModelScopeModel]:
        """
        根据工具调用能力过滤模型列表

        Args:
            models: 原始模型列表

        Returns:
            过滤后的模型列表（只包含支持工具调用的模型）
        """
        # 如果禁用能力测试，返回所有模型
        if not self.enable_tool_capability_test:
            logger.debug("工具调用能力测试已禁用，返回所有模型")
            return models

        # 如果没有测试器，返回所有模型并记录警告
        if not self.capability_tester:
            logger.warning("能力测试器未初始化，跳过能力测试")
            return models

        if not models:
            return models

        logger.info(f"开始对 {len(models)} 个 ModelScope 模型进行工具调用能力测试")

        # 提取模型 ID 列表
        model_ids = [m.model_id for m in models]

        # 加载现有缓存
        cache_data = self.capability_cache.load() or {"models": {}}
        cached_capabilities = cache_data.get("models", {})

        # 识别需要测试的模型（不在缓存中的）
        untested_model_ids = [mid for mid in model_ids if mid not in cached_capabilities]

        if untested_model_ids:
            logger.info(f"发现 {len(untested_model_ids)} 个未测试的模型，开始测试...")

            # 并行测试新模型
            new_results = await self.capability_tester.test_models_concurrently(
                models=untested_model_ids,
                max_concurrent=self.max_concurrent_tests,
                platform="modelscope"
            )

            # 构建新的能力数据
            new_capabilities = {}
            for model_id, supports_tools in new_results.items():
                new_capabilities[model_id] = {
                    "supports_tools": supports_tools,
                    "tested_at": datetime.now(timezone.utc).isoformat(),
                    "platform": "modelscope"
                }

            # 保存新结果到缓存
            if new_capabilities:
                self.capability_cache.save(new_capabilities, platform="modelscope")

            # 合并缓存
            cached_capabilities.update(new_capabilities)
        else:
            logger.debug(f"所有 {len(model_ids)} 个模型都已在缓存中，跳过测试")

        # 过滤模型：只保留支持工具调用的
        filtered_models = []
        for model in models:
            capability = cached_capabilities.get(model.model_id)
            if capability and capability.get("supports_tools"):
                filtered_models.append(model)
            else:
                logger.debug(f"过滤掉模型: {model.model_id} (不支持工具调用或未测试)")

        logger.info(
            f"能力测试完成: {len(filtered_models)}/{len(models)} 个 ModelScope 模型支持工具调用"
        )

        return filtered_models

    async def _get_models_from_scraper(self) -> List[ModelScopeModel]:
        """
        使用爬虫从网页获取免费模型列表

        Returns:
            模型列表
        """
        logger.info(f"使用爬虫从 {self.scrape_url} 获取免费模型...")

        try:
            # 创建爬虫实例
            scraper = ModelScopeModelScraper(
                scrape_url=self.scrape_url,
                max_models=self.max_models,
                timeout=self.scraper_timeout,
                headless=self.headless
            )

            # 执行爬虫
            models_data = await scraper.scrape()

            if not models_data:
                logger.warning("爬虫未获取到模型数据")
                return []

            # 转换为 ModelScopeModel 对象
            models = []
            for model_data in models_data:
                model_id = model_data.get('model_id')
                model_name = model_data.get('model_name', model_id)

                if model_id:
                    models.append(ModelScopeModel(
                        model_id=model_id,
                        model_name=model_name,
                        capabilities=["text"]
                    ))

            logger.info(f"✓ 从爬虫获取到 {len(models)} 个免费模型")
            return models

        except Exception as e:
            logger.error(f"爬虫执行失败: {e}", exc_info=True)
            raise

    async def parse_error(self, response_data: Any) -> ErrorType:
        """
        解析错误响应

        Args:
            response_data: 响应数据

        Returns:
            错误类型
        """
        try:
            if isinstance(response_data, dict):
                error_info = response_data.get("error", {})
                if isinstance(error_info, dict):
                    error_message = error_info.get("message", "").lower()

                    # 检查配额相关错误
                    if any(keyword in error_message for keyword in ['quota', 'rate limit', '超出配额']):
                        return ErrorType.QUOTA_EXCEEDED

                    # 检查认证相关错误
                    if any(keyword in error_message for keyword in ['authentication', 'unauthorized', '认证失败']):
                        return ErrorType.AUTH_ERROR

            # 使用通用错误分类
            return ErrorClassifier.classify_by_response(500, str(response_data))

        except Exception:
            return ErrorType.UNKNOWN_ERROR

    async def start_scheduler(self, wait_for_initial: bool = True) -> None:
        """
        启动定时任务调度器

        Args:
            wait_for_initial: 是否等待初始爬虫任务完成，默认True
                             这样可以确保服务启动时使用最新的模型数据

        应在服务启动时调用此方法。
        """
        if self.scheduler:
            try:
                # 先启动调度器（这会触发初始爬虫任务）
                await self.scheduler.start(wait_for_initial=False)

                # 如果需要等待初始任务完成，手动等待
                if wait_for_initial:
                    logger.info("等待初始爬虫任务完成...")
                    # 等待 initial_scrape_completed 标志被设置
                    # 考虑到爬虫可能需要重试，增加等待时间
                    max_wait_time = 180  # 最多等待180秒（3分钟）
                    waited = 0
                    while not self.initial_scrape_completed and waited < max_wait_time:
                        await asyncio.sleep(0.5)
                        waited += 0.5

                    if self.initial_scrape_completed:
                        logger.info("✓ 初始爬虫任务已完成")
                    else:
                        logger.warning(f"⚠ 等待初始爬虫任务超时（{max_wait_time}秒），但将继续启动")

                logger.info("✓ ModelScope爬虫调度器已启动")
            except Exception as e:
                logger.error(f"启动调度器失败: {e}", exc_info=True)
        else:
            logger.debug("调度器未初始化（可能未启用爬虫功能）")

    async def stop_scheduler(self) -> None:
        """
        停止定时任务调度器

        应在服务关闭时调用此方法。
        """
        if self.scheduler:
            try:
                await self.scheduler.stop(wait=True, timeout=5)
                logger.info("✓ ModelScope爬虫调度器已停止")
            except Exception as e:
                logger.error(f"停止调度器失败: {e}")
