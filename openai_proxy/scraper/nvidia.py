"""
NVIDIA免费模型专用爬虫

从build.nvidia.com抓取免费预览模型列表。
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional
from playwright.async_api import Page

from openai_proxy.scraper.base import WebScraper

logger = logging.getLogger(__name__)


class NVIDIAModelScraper(WebScraper):
    """
    NVIDIA免费模型专用爬虫

    从build.nvidia.com页面提取免费模型（需要处理JS渲染）

    使用示例:
        scraper = NVIDIAModelScraper(
            scrape_url="https://build.nvidia.com/models?filters=nimType%3Anim_type_preview",
            free_model_count=10,
            timeout=60,
            headless=True
        )
        models = await scraper.scrape()
    """

    def __init__(
        self,
        scrape_url: str,
        free_model_count: int = 10,
        **kwargs
    ):
        """
        初始化NVIDIA模型爬虫

        Args:
            scrape_url: 爬虫目标URL（必填）
            free_model_count: 返回的免费模型数量，默认10
            **kwargs: 传递给父类WebScraper的参数
        """
        super().__init__(url=scrape_url, **kwargs)

        self.free_model_count = free_model_count
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
        从网页提取模型数据（通过DOM解析）

        Args:
            page: Playwright页面对象

        Returns:
            免费模型列表（按人气排序）
        """
        logger.info("从网页提取模型数据...")

        try:
            # 等待页面加载完成
            await page.wait_for_load_state('domcontentloaded', timeout=self.timeout)
            await asyncio.sleep(3)  # 等待动态内容渲染

            # 从页面链接中提取所有模型ID
            models_data = await page.evaluate("""
                () => {
                    const modelSet = new Set();
                    const models = [];

                    // 从所有链接中提取模型ID
                    const links = document.querySelectorAll('a[href]');
                    links.forEach(link => {
                        const href = link.getAttribute('href');
                        // 匹配 /publisher/model-name 模式
                        const match = href.match(/^\\/([^\\/]+)\\/([^\\/\\?]+)/);
                        if (match) {
                            const publisher = match[1];
                            const modelName = match[2];

                            // 排除非模型路径
                            const excludePaths = ['explore', 'blueprints', 'models', 'api',
                                                'community', '_next', 'chat', 'image',
                                                'video', 'audio', 'embeddings'];

                            if (!excludePaths.includes(publisher)) {
                                const modelId = `${publisher}/${modelName}`;
                                if (!modelSet.has(modelId)) {
                                    modelSet.add(modelId);
                                    models.push({
                                        model_id: modelId,
                                        model_name: modelName,
                                        owner: publisher,
                                        rank: models.length + 1
                                    });
                                }
                            }
                        }
                    });

                    return models;
                }
            """)

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

    async def _intercept_api_calls(self, page: Page) -> Optional[List[Dict]]:
        """拦截页面的API调用以获取模型数据"""
        # 注意：这个方法需要在load_page之前设置路由拦截
        # 由于架构限制，这里简化处理，实际使用时可能需要调整
        logger.debug("API拦截策略：需要在页面加载前设置路由")
        return None

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
            # 尝试多种选择器
            selectors = [
                "[data-testid*='model-card']",
                ".model-card",
                "article[role='article']",
                "[class*='ModelCard']",
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

    async def _extract_model_from_card(self, card_element, rank: int) -> Optional[Dict]:
        """从单个模型卡片元素提取信息"""
        try:
            # 尝试提取模型ID（通常在链接或data属性中）
            model_id = await card_element.evaluate("""
                (el) => {
                    // 尝试从各种位置提取模型ID
                    const link = el.querySelector('a[href*="/models/"]');
                    if (link) {
                        const href = link.getAttribute('href');
                        const match = href.match(/\\/models\\/([^\\/]+)/);
                        if (match) return match[1];
                    }

                    // 尝试从data属性
                    return el.getAttribute('data-model-id') ||
                           el.getAttribute('data-testid') ||
                           el.querySelector('[data-model-id]')?.getAttribute('data-model-id');
                }
            """)

            if not model_id:
                return None

            # 提取模型名称
            model_name = await card_element.evaluate("""
                (el) => {
                    // 尝试从标题元素提取
                    const title = el.querySelector('h1, h2, h3, [class*="title"], [class*="name"]');
                    return title ? title.textContent.trim() : null;
                }
            """)

            return {
                "model_id": model_id,
                "model_name": model_name or model_id,
                "owner": model_id.split("/")[0] if "/" in model_id else "unknown",
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
                    if obj and isinstance(obj[0], dict) and 'id' in obj[0]:
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
                        "model_id": m.get("id"),
                        "model_name": m.get("name") or m.get("id"),
                        "owner": m.get("id", "").split("/")[0] if m.get("id") else "unknown",
                        "rank": i + 1
                    }
                    for i, m in enumerate(models_list)
                    if m.get("id")
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

            # 验证ID格式（应包含 `/`）
            if "/" not in model_id:
                logger.warning(f"跳过无效模型ID: {model_id}")
                continue

            seen_ids.add(model_id)
            validated_models.append(model)

        # 2. 按排名排序
        validated_models.sort(key=lambda x: x.get("rank", 999999))

        # 3. 截取指定数量
        result = validated_models[:self.free_model_count]

        logger.info(f"处理后返回 {len(result)} 个模型（总共 {len(validated_models)} 个）")

        return result
