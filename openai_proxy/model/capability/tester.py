"""
工具调用能力测试器

负责测试模型的工具调用能力，包括：
- 单个模型测试（非流式和流式）
- 并行测试框架（Semaphore 控制并发）
- 错误处理和退避重试机制
"""

import asyncio
import logging
from typing import Dict, List, Optional, Tuple
import aiohttp
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class ToolCapabilityTester:
    """
    工具调用能力测试器

    测试模型是否支持工具调用功能，支持非流式和流式两种模式。

    使用示例:
        tester = ToolCapabilityTester(
            base_url="https://api-inference.modelscope.cn/v1",
            api_key="your-api-key"
        )

        # 测试单个模型
        result = await tester.test_single_model("deepseek-ai/DeepSeek-V4-Flash")

        # 并行测试多个模型
        results = await tester.test_models_concurrently(models, max_concurrent=10)
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: int = 5,
        max_retries: int = 3
    ):
        """
        初始化测试器

        Args:
            base_url: API 基础URL
            api_key: API 密钥
            timeout: 单个测试超时时间（秒）
            max_retries: 最大重试次数（针对限流）
        """
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries

    async def test_single_model(self, model_id: str, platform: str = "") -> Optional[bool]:
        """
        测试单个模型的工具调用能力

        同时测试非流式和流式两种模式，只要有一种模式支持即认为支持。

        Args:
            model_id: 模型ID
            platform: 平台名称（用于日志）

        Returns:
            True表示支持工具调用，False表示不支持，None表示测试失败
        """
        platform_str = f" [{platform}]" if platform else ""
        logger.debug(f"测试模型{platform_str}: {model_id}")

        # 先测试非流式
        non_streaming_result = await self._test_non_streaming(model_id)
        if non_streaming_result is True:
            logger.debug(f"✓ 模型{platform_str} {model_id} 支持工具调用（非流式）")
            return True

        # 如果非流式不支持或失败，测试流式
        streaming_result = await self._test_streaming(model_id)
        if streaming_result is True:
            logger.debug(f"✓ 模型{platform_str} {model_id} 支持工具调用（流式）")
            return True

        # 两种模式都不支持
        if non_streaming_result is False and streaming_result is False:
            logger.debug(f"✗ 模型{platform_str} {model_id} 不支持工具调用")
            return False

        # 至少有一个测试失败
        logger.warning(f"⚠ 模型{platform_str} {model_id} 测试结果不确定，视为不支持")
        return False

    async def _test_non_streaming(self, model_id: str) -> Optional[bool]:
        """
        测试非流式工具调用能力

        Args:
            model_id: 模型ID

        Returns:
            True/False/None
        """
        for attempt in range(self.max_retries):
            try:
                async with aiohttp.ClientSession() as session:
                    headers = {
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}"
                    }

                    payload = {
                        "model": model_id,
                        "messages": [{"role": "user", "content": "test"}],
                        "tools": [{
                            "type": "function",
                            "function": {
                                "name": "_test_tool",
                                "description": "Test tool capability",
                                "parameters": {"type": "object", "properties": {}}
                            }
                        }],
                        "tool_choice": "auto",
                        "max_tokens": 10,
                        "temperature": 0,
                        "stream": False
                    }

                    async with session.post(
                        f"{self.base_url}/chat/completions",
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=self.timeout)
                    ) as response:
                        # 处理特殊状态码
                        if response.status == 429:
                            # API 限流，退避重试
                            if attempt < self.max_retries - 1:
                                wait_time = 2 ** attempt  # 指数退避: 1s, 2s, 4s
                                logger.warning(f"API 限流，{wait_time}秒后重试 (尝试 {attempt + 1}/{self.max_retries})")
                                await asyncio.sleep(wait_time)
                                continue
                            else:
                                logger.error(f"API 限流，已达最大重试次数")
                                return None

                        elif response.status == 401:
                            logger.error(f"API 密钥无效 (401)")
                            raise Exception("API 密钥无效，请检查配置")

                        elif response.status == 404:
                            logger.warning(f"模型不存在 (404): {model_id}")
                            return False

                        elif response.status != 200:
                            error_text = await response.text()
                            logger.error(f"测试请求失败 ({response.status}): {error_text}")
                            return None

                        # 解析响应
                        data = await response.json()
                        choices = data.get("choices", [])
                        if not choices:
                            logger.warning(f"响应中没有choices字段")
                            return False

                        message = choices[0].get("message", {})
                        tool_calls = message.get("tool_calls")

                        # 检查是否有工具调用
                        if tool_calls and len(tool_calls) > 0:
                            return True
                        else:
                            return False

            except asyncio.TimeoutError:
                logger.warning(f"测试超时 ({self.timeout}秒): {model_id}")
                return None

            except Exception as e:
                if "API 密钥无效" in str(e):
                    raise
                logger.error(f"测试出错: {e}", exc_info=True)
                return None

        return None

    async def _test_streaming(self, model_id: str) -> Optional[bool]:
        """
        测试流式工具调用能力

        Args:
            model_id: 模型ID

        Returns:
            True/False/None
        """
        for attempt in range(self.max_retries):
            try:
                async with aiohttp.ClientSession() as session:
                    headers = {
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}"
                    }

                    payload = {
                        "model": model_id,
                        "messages": [{"role": "user", "content": "test"}],
                        "tools": [{
                            "type": "function",
                            "function": {
                                "name": "_test_tool",
                                "description": "Test tool capability",
                                "parameters": {"type": "object", "properties": {}}
                            }
                        }],
                        "tool_choice": "auto",
                        "max_tokens": 10,
                        "temperature": 0,
                        "stream": True
                    }

                    async with session.post(
                        f"{self.base_url}/chat/completions",
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=self.timeout)
                    ) as response:
                        # 处理特殊状态码（与非流式相同）
                        if response.status == 429:
                            if attempt < self.max_retries - 1:
                                wait_time = 2 ** attempt
                                logger.warning(f"API 限流（流式），{wait_time}秒后重试")
                                await asyncio.sleep(wait_time)
                                continue
                            else:
                                logger.error(f"API 限流（流式），已达最大重试次数")
                                return None

                        elif response.status == 401:
                            logger.error(f"API 密钥无效 (401)")
                            raise Exception("API 密钥无效，请检查配置")

                        elif response.status == 404:
                            logger.warning(f"模型不存在 (404): {model_id}")
                            return False

                        elif response.status != 200:
                            error_text = await response.text()
                            logger.error(f"流式测试请求失败 ({response.status}): {error_text}")
                            return None

                        # 读取流式响应，检查是否有 tool_calls
                        has_tool_calls = False
                        async for line in response.content:
                            line = line.decode('utf-8').strip()
                            if line.startswith('data: '):
                                data_str = line[6:]
                                if data_str == '[DONE]':
                                    break

                                try:
                                    import json
                                    chunk = json.loads(data_str)
                                    choices = chunk.get("choices", [])
                                    if choices:
                                        delta = choices[0].get("delta", {})
                                        tool_calls = delta.get("tool_calls")
                                        if tool_calls and len(tool_calls) > 0:
                                            has_tool_calls = True
                                            break
                                except json.JSONDecodeError:
                                    continue

                        return has_tool_calls

            except asyncio.TimeoutError:
                logger.warning(f"流式测试超时 ({self.timeout}秒): {model_id}")
                return None

            except Exception as e:
                if "API 密钥无效" in str(e):
                    raise
                logger.error(f"流式测试出错: {e}", exc_info=True)
                return None

        return None

    async def test_models_concurrently(
        self,
        models: List[str],
        max_concurrent: int = 10,
        platform: str = ""
    ) -> Dict[str, bool]:
        """
        并行测试多个模型的工具调用能力

        Args:
            models: 模型ID列表
            max_concurrent: 最大并发数
            platform: 平台名称（用于日志）

        Returns:
            模型ID到能力状态的映射 {model_id: supports_tools}
        """
        if not models:
            return {}

        platform_str = f" [{platform}]" if platform else ""
        logger.info(f"开始并行测试{platform_str}: {len(models)} 个模型 (最大并发: {max_concurrent})")

        semaphore = asyncio.Semaphore(max_concurrent)
        results = {}
        completed = 0
        total = len(models)

        async def test_with_semaphore(model_id: str) -> Tuple[str, Optional[bool]]:
            nonlocal completed
            async with semaphore:
                result = await self.test_single_model(model_id, platform)
                completed += 1
                progress = f"{completed}/{total}"
                
                if result is True:
                    logger.info(f"[{progress}] ✓ {model_id} 支持工具调用")
                elif result is False:
                    logger.debug(f"[{progress}] ✗ {model_id} 不支持工具调用")
                else:
                    logger.warning(f"[{progress}] ⚠ {model_id} 测试失败")
                
                return (model_id, result)

        # 创建所有测试任务
        tasks = [test_with_semaphore(model_id) for model_id in models]

        # 等待所有任务完成
        task_results = await asyncio.gather(*tasks, return_exceptions=True)

        # 处理结果
        for task_result in task_results:
            if isinstance(task_result, Exception):
                logger.error(f"测试任务异常: {task_result}")
                continue

            model_id, result = task_result
            # 只保留确认支持的模型（result为True）
            # result为None（测试失败）或False（不支持）的模型都过滤掉
            if result is True:
                results[model_id] = True
            else:
                # 记录被过滤的模型
                status = "测试失败" if result is None else "不支持"
                logger.warning(f"过滤掉模型 {model_id} ({status})")

        supported_count = len(results)
        logger.info(f"并行测试完成{platform_str}: {supported_count}/{total} 个模型支持工具调用")

        return results
