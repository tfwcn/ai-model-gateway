"""
OpenRouter免费模型专用爬虫

从openrouter.ai/models页面抓取免费模型列表。
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional
from playwright.async_api import Page

from openai_proxy.scraper.base import WebScraper

logger = logging.getLogger(__name__)


class OpenRouterModelScraper(WebScraper):
    """
    OpenRouter免费模型专用爬虫

    从openrouter.ai/models页面提取免费模型（需要处理JS渲染）

    使用示例:
        scraper = OpenRouterModelScraper(
            scrape_url="https://openrouter.ai/models?fmt=cards&input_modalities=text%2Cimage&max_price=0&order=most-popular&output_modalities=text",
            timeout=60,
            headless=True
        )
        models = await scraper.scrape()
    """

    def __init__(
        self,
        scrape_url: str,
        max_models: int = 50,
        **kwargs
    ):
        """
        初始化OpenRouter模型爬虫

        Args:
            scrape_url: 爬虫目标URL（必填）
            max_models: 返回的最大模型数量，默认50
            **kwargs: 传递给父类WebScraper的参数
        """
        super().__init__(url=scrape_url, **kwargs)

        self.max_models = max_models
        self.scrape_url = scrape_url

    async def extract_data(self, page: Page) -> List[Dict[str, Any]]:
        """
        从网页提取模型数据

        Args:
            page: Playwright页面对象

        Returns:
            免费模型列表（按人气排序）
        """
        return await self._extract_from_webpage(page)

    async def _extract_from_webpage(self, page: Page) -> List[Dict[str, Any]]:
        """
        从网页提取模型数据（通过DOM解析和JavaScript评估）

        Args:
            page: Playwright页面对象

        Returns:
            免费模型列表（按人气排序）
        """
        logger.info("从OpenRouter网页提取模型数据...")

        try:
            # 等待页面加载完成
            await page.wait_for_load_state('domcontentloaded', timeout=self.timeout)
            await asyncio.sleep(3)  # 等待动态内容渲染

            # 尝试多种方法提取模型数据
            models_data = None

            # 方法1: 尝试从页面脚本中提取嵌入的JSON数据
            models_data = await self._extract_embedded_json(page)

            # 方法2: 如果方法1失败，尝试从DOM元素中提取
            if not models_data:
                models_data = await self._parse_dom_elements(page)

            # 方法3: 如果都失败，尝试从链接中提取
            if not models_data:
                models_data = await self._extract_from_links(page)

            if not models_data:
                logger.warning("未能从页面提取到模型数据")
                return []

            logger.info(f"从页面提取到 {len(models_data)} 个模型")

            # 处理并过滤模型
            result = self._process_and_filter_models(models_data)

            logger.info(f"✓ 成功从网页提取 {len(result)} 个免费模型")
            return result

        except Exception as e:
            logger.error(f"网页提取失败: {e}", exc_info=True)
            return []

    async def _extract_embedded_json(self, page: Page) -> Optional[List[Dict]]:
        """从页面嵌入的JSON脚本中提取数据"""
        try:
            # 尝试查找Next.js或其他框架的嵌入数据
            json_data = await page.evaluate("""
                () => {
                    // 查找常见的嵌入式JSON
                    const scripts = document.querySelectorAll('script[type="application/json"], script#__NEXT_DATA__, script#_NITRO_DATA');
                    for (const script of scripts) {
                        try {
                            const data = JSON.parse(script.textContent);
                            // 检查是否包含模型数据
                            if (data && (data.models || data.props || data.initialState)) {
                                return data;
                            }
                        } catch (e) {
                            continue;
                        }
                    }
                    return null;
                }
            """)

            if json_data:
                # 解析JSON数据结构，提取模型列表
                return self._parse_json_structure(json_data)

            return None

        except Exception as e:
            logger.debug(f"嵌入JSON提取失败: {e}")
            return None

    async def _parse_dom_elements(self, page: Page) -> Optional[List[Dict]]:
        """通过DOM解析提取模型卡片信息"""
        try:
            # 尝试多种选择器来定位模型卡片
            selectors = [
                "[data-testid*='model-card']",
                ".model-card",
                "article[role='article']",
                "[class*='ModelCard']",
                "[class*='model-card']",
                "div[class*='card']",
            ]

            models = []
            for selector in selectors:
                cards = await page.query_selector_all(selector)
                if cards:
                    logger.debug(f"使用选择器 '{selector}' 找到 {len(cards)} 个卡片")

                    for i, card in enumerate(cards):
                        model_info = await self._extract_model_from_card(card, i + 1)
                        if model_info:
                            models.append(model_info)

                    if models:
                        break

            return models if models else None

        except Exception as e:
            logger.debug(f"DOM解析失败: {e}")
            return None

    async def _extract_from_links(self, page: Page) -> Optional[List[Dict]]:
        """从页面链接中提取模型ID"""
        try:
            models_data = await page.evaluate("""
                () => {
                    const modelSet = new Set();
                    const models = [];

                    // 从所有链接中提取模型ID
                    const links = document.querySelectorAll('a[href]');
                    links.forEach(link => {
                        const href = link.getAttribute('href');
                        // 匹配 /publisher/model-name 模式（排除常见非模型路径）
                        const match = href.match(/^\\/([^\\/]+)\\/([^\\/\\?]+)/);
                        if (match) {
                            const publisher = match[1];
                            const modelName = match[2];

                            // 排除非模型路径
                            const excludePaths = ['explore', 'playground', 'docs', 'api',
                                                'community', '_next', 'chat', 'image',
                                                'video', 'audio', 'embeddings', 'pricing',
                                                'labs', 'models', 'rankings', 'apps',
                                                'enterprise', 'compare'];

                            if (!excludePaths.includes(publisher) && !modelName.startsWith('_')) {
                                const modelId = `${publisher}/${modelName}`;
                                if (!modelSet.has(modelId)) {
                                    modelSet.add(modelId);
                                    models.push({
                                        model_id: modelId,
                                        model_name: modelName,
                                        rank: models.length + 1
                                    });
                                }
                            }
                        }
                    });

                    return models;
                }
            """)

            return models_data if models_data else None

        except Exception as e:
            logger.debug(f"链接提取失败: {e}")
            return None

    async def _extract_model_from_card(self, card_element, rank: int) -> Optional[Dict]:
        """从单个模型卡片元素提取信息"""
        try:
            # 尝试提取模型ID（通常在链接或data属性中）
            model_info = await card_element.evaluate("""
                (el) => {
                    let modelId = null;
                    let modelName = null;

                    // 尝试从链接提取模型ID
                    const link = el.querySelector('a[href*="/models/"]');
                    if (link) {
                        const href = link.getAttribute('href');
                        const match = href.match(/\\/models\\/([^\\/\\?]+)/);
                        if (match) {
                            modelId = match[1];
                        }
                    }

                    // 尝试从data属性
                    if (!modelId) {
                        modelId = el.getAttribute('data-model-id') ||
                                 el.getAttribute('data-testid') ||
                                 el.querySelector('[data-model-id]')?.getAttribute('data-model-id');
                    }

                    // 提取模型名称
                    const title = el.querySelector('h1, h2, h3, h4, [class*="title"], [class*="name"]');
                    if (title) {
                        modelName = title.textContent.trim();
                    }

                    if (!modelName && modelId) {
                        modelName = modelId;
                    }

                    return {
                        model_id: modelId,
                        model_name: modelName
                    };
                }
            """)

            if not model_info or not model_info.get('model_id'):
                return None

            return {
                "model_id": model_info['model_id'],
                "model_name": model_info['model_name'] or model_info['model_id'],
                "rank": rank
            }

        except Exception as e:
            logger.debug(f"提取卡片信息失败: {e}")
            return None

    def _parse_json_structure(self, json_data: Dict) -> Optional[List[Dict]]:
        """解析嵌入的JSON数据结构，提取模型列表"""
        try:
            # 递归查找包含模型列表的结构
            def find_models(obj, depth=0):
                if depth > 5:  # 限制递归深度
                    return None

                if isinstance(obj, dict):
                    # 检查是否包含模型数组
                    for key in ['models', 'data', 'items']:
                        if key in obj and isinstance(obj[key], list):
                            return obj[key]

                    # 递归搜索
                    for value in obj.values():
                        result = find_models(value, depth + 1)
                        if result:
                            return result

                elif isinstance(obj, list):
                    # 检查列表中的元素是否是模型对象
                    if obj and isinstance(obj[0], dict) and ('id' in obj[0] or 'slug' in obj[0]):
                        return obj

                    # 递归搜索
                    for item in obj:
                        result = find_models(item, depth + 1)
                        if result:
                            return result

                return None

            models_list = find_models(json_data)
            if models_list:
                # 转换为标准格式
                return [
                    {
                        "model_id": m.get("id") or m.get("slug"),
                        "model_name": m.get("name") or m.get("id") or m.get("slug"),
                        "rank": i + 1
                    }
                    for i, m in enumerate(models_list)
                    if m.get("id") or m.get("slug")
                ]

            return None

        except Exception as e:
            logger.debug(f"JSON结构解析失败: {e}")
            return None

    def _process_and_filter_models(self, models: List[Dict]) -> List[Dict]:
        """
        处理并过滤模型列表

        Args:
            models: 原始模型列表

        Returns:
            处理后的免费模型列表（已去重、验证、排序、截取）
        """
        # 1. 验证和清理数据
        validated_models = []
        seen_ids = set()

        for model in models:
            model_id = model.get("model_id")

            # 跳过无效ID
            if not model_id or not isinstance(model_id, str):
                continue

            # 跳过重复ID
            if model_id in seen_ids:
                continue

            seen_ids.add(model_id)
            validated_models.append(model)

        # 2. 按排名排序
        validated_models.sort(key=lambda x: x.get("rank", 999999))

        # 3. 截取指定数量
        result = validated_models[:self.max_models]

        logger.info(f"处理后返回 {len(result)} 个模型（总共 {len(validated_models)} 个）")

        return result
