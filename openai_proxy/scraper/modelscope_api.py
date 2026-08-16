"""
ModelScope 官方 API 客户端

通过官方 /models 接口获取可用推理模型列表，相比网页爬虫更稳定高效。
ModelScope 的 API 推理服务返回的模型即免费推理模型。
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


class ModelScopeAPIClient:
    """
    ModelScope 官方 API 客户端

    调用官方 `GET /v1/models` 接口获取可用推理模型列表。
    ModelScope 的 API-Inference 服务中，接口返回的模型即为可免费调用的模型。

    使用示例:
        client = ModelScopeAPIClient(api_key="ms-xxx", max_models=10)
        models = await client.fetch_free_models()
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://api-inference.modelscope.cn/v1",
        max_models: int = 50,
        timeout: int = 30,
    ):
        """
        初始化 ModelScope API 客户端

        Args:
            api_key: API 密钥（可选）
            base_url: API 基础URL，默认官方推理地址
            max_models: 返回的最大模型数量
            timeout: 请求超时时间（秒）
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_models = max_models
        self.timeout = timeout

    async def fetch_free_models(self) -> List[Dict[str, Any]]:
        """
        获取可用推理模型列表

        Returns:
            模型列表，每个元素包含 model_id / model_name / rank 字段，
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

                    result: List[Dict[str, Any]] = []
                    for model in models:
                        model_id = model.get("id")
                        if not model_id:
                            continue

                        result.append({
                            "model_id": model_id,
                            "model_name": model_id.split("/")[-1] if "/" in model_id else model_id,
                            "rank": len(result) + 1,
                        })

                    # 截取指定数量
                    result = result[:self.max_models]

                    logger.info(
                        f"✓ 通过官方 API 获取到 {len(models)} 个可用推理模型"
                        f"（返回前 {len(result)} 个）"
                    )
                    return result

        except asyncio.TimeoutError:
            logger.error(f"请求超时（{self.timeout}秒）")
            return []
        except Exception as e:
            logger.error(f"官方 API 请求失败: {type(e).__name__}: {e}")
            return []