"""
OpenRouter 官方 API 客户端

通过官方 /models 接口获取免费模型列表，相比网页爬虫更稳定高效。
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


class OpenRouterAPIClient:
    """
    OpenRouter 官方 API 客户端

    调用官方 `GET /api/v1/models` 接口获取全部模型，再根据定价过滤出免费模型。
    相比 Playwright 网页爬虫：无需启动浏览器、无页面结构依赖、速度快、更稳定。

    使用示例:
        client = OpenRouterAPIClient(api_key="sk-xxx", max_models=10)
        models = await client.fetch_free_models()
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://openrouter.ai/api/v1",
        max_models: int = 50,
        timeout: int = 30,
    ):
        """
        初始化 OpenRouter API 客户端

        Args:
            api_key: API 密钥（可选，官方模型列表接口不强制要求）
            base_url: API 基础URL，默认官方地址
            max_models: 返回的最大免费模型数量
            timeout: 请求超时时间（秒）
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_models = max_models
        self.timeout = timeout

    async def fetch_free_models(self) -> List[Dict[str, Any]]:
        """
        获取免费模型列表

        从官方模型列表接口获取数据，过滤出定价为 0 的免费模型。

        Returns:
            免费模型列表，每个元素包含 model_id / model_name / rank 字段，
            与网页爬虫返回格式保持一致，方便上层无缝切换。
        """
        url = f"{self.base_url}/models"
        headers = {
            "Accept": "application/json",
            "User-Agent": "openai-proxy-plugin/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout)
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"获取模型列表失败: HTTP {response.status}: {error_text}")
                        return []

                    data = await response.json()
                    models = data.get("data", [])

                    # 过滤免费模型：prompt 定价为 0
                    free_models: List[Dict[str, Any]] = []
                    for model in models:
                        pricing = model.get("pricing") or {}
                        try:
                            prompt_price = float(pricing.get("prompt", 1) or 1)
                        except (TypeError, ValueError):
                            prompt_price = 1.0

                        if prompt_price != 0:
                            continue

                        model_id = model.get("id")
                        if not model_id:
                            continue

                        free_models.append({
                            "model_id": model_id,
                            "model_name": model.get("name") or model_id,
                            "context_length": model.get("context_length"),
                            "rank": len(free_models) + 1,
                        })

                    # 截取指定数量
                    result = free_models[:self.max_models]

                    logger.info(
                        f"✓ 通过官方 API 获取到 {len(free_models)} 个免费模型"
                        f"（返回前 {len(result)} 个）"
                    )
                    return result

        except asyncio.TimeoutError:
            logger.error(f"请求超时（{self.timeout}秒）")
            return []
        except Exception as e:
            logger.error(f"官方 API 请求失败: {type(e).__name__}: {e}")
            return []