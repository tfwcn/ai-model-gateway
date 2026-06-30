import asyncio
import json
import logging
import time
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime

import aiohttp
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from openai_proxy.models import ModelConfig
from openai_proxy.model.state import ModelStateManager
from openai_proxy.model.error_classifier import ErrorClassifier, ClassifiedError
from openai_proxy.utils.tool_call_converter import ToolCallConverter
from openai_proxy.utils.streaming_tool_call_buffer import StreamingToolCallBuffer

logger = logging.getLogger(__name__)


class ModelFailoverManager:
    """模型故障转移管理器 - 负责模型选择、调用和故障转移"""

    def __init__(self, models: Dict[str, List[ModelConfig]], all_aliases: Optional[Dict[str, List[str]]] = None):
        self.models = models
        self.all_aliases = all_aliases or {}
        self.session: Optional[aiohttp.ClientSession] = None
        self.model_state_manager = ModelStateManager()

    def _parse_model_selector(self, model_selector: str) -> Tuple[str, Optional[str]]:
        """
        解析模型选择器，支持三种格式：
        1. "all" - 所有平台所有模型
        2. "{platform}" - 指定平台的所有模型
        3. "{platform}|{model_name}" - 指定平台的指定模型

        Returns:
            Tuple[str, Optional[str]]: (platform_or_all, model_name_or_none)
            - 如果是 "all"，返回 ("all", None)
            - 如果是 "{platform}"，返回 ("{platform}", None)
            - 如果是 "{platform}|{model_name}"，返回 ("{platform}", "{model_name}")
        """
        if model_selector == "all":
            return ("all", None)

        parts = model_selector.split("|", 1)
        if len(parts) == 2:
            platform = parts[0].strip()
            model_part = parts[1].strip()
            return (platform, model_part)

        return (model_selector, None)

    async def get_session(self) -> aiohttp.ClientSession:
        """获取HTTP会话"""
        if self.session is None or self.session.closed:
            # 移除total超时，避免影响流式响应
            self.session = aiohttp.ClientSession(
                headers={"Content-Type": "application/json"}
            )
        else:
            pass
        return self.session

    def _has_valid_content(self, response_data: Any) -> bool:
        """
        检查响应数据是否包含有效的 content 字段

        Args:
            response_data: API 响应的 JSON 数据

        Returns:
            bool: 如果包含有效的 content 字段返回 True，否则返回 False
        """
        try:
            if not isinstance(response_data, dict):
                return False

            # 检查 choices 数组是否存在且非空
            choices = response_data.get("choices")
            if not choices or not isinstance(choices, list) or len(choices) == 0:
                return False

            # 检查第一个 choice 是否包含 message 或 delta
            first_choice = choices[0]
            if not isinstance(first_choice, dict):
                return False

            # 对于普通响应，检查 message.content 或 message.tool_calls
            if "message" in first_choice:
                message = first_choice["message"]
                if isinstance(message, dict):
                    # 检查 tool_calls（工具调用也是有效响应，优先级最高）
                    if "tool_calls" in message and message["tool_calls"]:
                        return True

                    # 检查 content
                    if "content" in message:
                        content = message["content"]
                        # content 必须是非None值，如果是字符串则不能是空字符串
                        if content is not None:
                            if isinstance(content, str):
                                if len(content.strip()) > 0:
                                    return True
                            else:
                                return True  # 非字符串类型（如数字、布尔值等）认为有效

                    # 检查 reasoning_content（某些模型如 minimax 使用此字段）
                    if "reasoning_content" in message:
                        reasoning_content = message["reasoning_content"]
                        if reasoning_content is not None:
                            if isinstance(reasoning_content, str):
                                return len(reasoning_content.strip()) > 0
                            else:
                                return True

            # 对于流式响应的 chunk，检查 delta.content 或 delta.tool_calls
            if "delta" in first_choice:
                delta = first_choice["delta"]
                if isinstance(delta, dict):
                    # 检查 content
                    if "content" in delta:
                        content = delta["content"]
                        # content 必须是非None值，如果是字符串则不能是空字符串
                        if content is not None:
                            if isinstance(content, str):
                                return len(content.strip()) > 0
                            else:
                                return True  # 非字符串类型认为有效

                    # 检查 tool_calls（工具调用也是有效响应）
                    if "tool_calls" in delta and delta["tool_calls"]:
                        return True

            # 如果既没有 message 也没有 delta，或者没有 content 字段
            return False

        except Exception as e:
            pass
            return False

    async def call_model_stream(self, model_config: ModelConfig, request_data: Dict[str, Any]) -> tuple[bool, Any]:
        """
        调用单个模型的流式响应 - 完全参数透传

        Returns:
            tuple[bool, Any]: (是否成功, 响应数据或错误信息)
        """
        session = await self.get_session()
        url = f"{model_config.base_url.rstrip('/')}/chat/completions"

        # 准备请求数据 - 完全透传，只替换必要的字段
        request_body = request_data.copy()
        request_body["model"] = model_config.model  # 替换为实际的模型名称

        headers = {
            "Authorization": f"Bearer {model_config.api_key}",
            "Content-Type": "application/json"
        }

        # 记录请求详情（但不记录敏感信息如API密钥）
        safe_request_data = request_data.copy()
        if "messages" in safe_request_data and len(str(safe_request_data["messages"])) > 200:
            safe_request_data["messages"] = f"[{len(safe_request_data['messages'])} messages, truncated]"

        try:
            start_time = time.time()
            logger.info(f"调用模型: {model_config.name} ({model_config.model}) - 流式")

            # 流式响应 - 设置精确的超时控制
            # connect: 连接建立超时
            # sock_connect: socket连接超时
            # total: None 表示流式传输无总时间限制
            # sock_read: 每次读取间隔超时，覆盖 TTFB + 流式间隙
            response = await session.post(
                url,
                json=request_body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(
                    connect=model_config.timeout,      # 连接建立超时
                    sock_connect=model_config.timeout,  # socket连接超时
                    total=None,                        # 无总超时限制
                    sock_read=model_config.timeout     # TTFB + 流式间隙兜底
                )
            )
            elapsed_time = time.time() - start_time

            # 读取第一个字节，检查是否是 JSON 错误响应
            try:
                first_byte = await asyncio.wait_for(
                    response.content.read(1),
                    timeout=5
                )

                # 如果首字节是 {，说明可能是 JSON 错误响应（SSE 以 "data:" 开头）
                if first_byte and first_byte.decode('utf-8', errors='replace').strip() == '{':
                    rest = await asyncio.wait_for(
                        response.content.read(4096),
                        timeout=10
                    )
                    chunk = first_byte + rest
                    chunk_str = chunk.decode('utf-8', errors='replace')
                    try:
                        json_data = json.loads(chunk_str)
                        if isinstance(json_data, dict) and ("error" in json_data or "errors" in json_data):
                            error_msg = f"流式响应返回错误: {chunk_str}"
                            logger.warning(error_msg)
                            response.close()
                            return False, error_msg
                    except (json.JSONDecodeError, ValueError):
                        pass
                    # 不是错误 JSON，当作普通数据透传
                    first_chunk_for_wrapper = chunk
                else:
                    first_chunk_for_wrapper = first_byte

                # 创建一个包装对象包含原始响应和预读取的数据
                class StreamResponseWrapper:
                    def __init__(self, original_response, preloaded_data):
                        self.original_response = original_response
                        self.preloaded_data = preloaded_data
                        self.first_chunk_sent = False
                        # 【新增】初始化工具调用缓冲器（如果启用）
                        self.tool_call_buffer = StreamingToolCallBuffer() if model_config.enable_tool_call_conversion else None

                    async def __aiter__(self):
                        """使用iter_any()读取数据块，由上层按行处理"""
                        if self.preloaded_data and not self.first_chunk_sent:
                            self.first_chunk_sent = True
                            yield self.preloaded_data

                        # 使用iter_any()读取原始数据块
                        async for chunk in self.original_response.content.iter_any():
                            if chunk:
                                yield chunk

                wrapped_response = StreamResponseWrapper(response, first_chunk_for_wrapper)
                return True, wrapped_response

            except asyncio.TimeoutError:
                # 首块数据读取超时，可能连接不稳定
                error_msg = f"模型 {model_config.name} 首块数据读取超时"
                logger.warning(error_msg)
                response.close()
                return False, error_msg
            except Exception as e:

                # 如果检查失败，假设是正常的流式响应
                return True, response

        except asyncio.TimeoutError:
            elapsed_time = time.time() - start_time
            # 使用错误分类器进行分类
            classified_error = ErrorClassifier.classify_timeout_error(
                model_config.name, elapsed_time, model_config.timeout
            )
            logger.warning(classified_error.message)
            logger.info(f"错误分类结果: {ErrorClassifier.get_error_summary(classified_error)}")
            return False, classified_error
        except aiohttp.ClientError as e:
            elapsed_time = time.time() - start_time
            # 使用错误分类器分类连接错误
            classified_error = ErrorClassifier.classify_connection_error(e, model_config.name)
            logger.warning(classified_error.message)
            logger.info(f"错误分类结果: {ErrorClassifier.get_error_summary(classified_error)}")
            return False, classified_error
        except Exception as e:
            elapsed_time = time.time() - start_time
            # 使用错误分类器分类未知错误
            classified_error = ErrorClassifier.classify_unknown_error(e, model_config.name, elapsed_time)
            logger.warning(classified_error.message)

            logger.info(f"错误分类结果: {ErrorClassifier.get_error_summary(classified_error)}")
            return False, classified_error

    async def call_model_non_stream(self, model_config: ModelConfig, request_data: Dict[str, Any]) -> tuple[bool, Any]:
        """
        调用单个模型的非流式响应 - 完全参数透传

        Returns:
            tuple[bool, Any]: (是否成功, 响应数据或错误信息)
        """
        session = await self.get_session()
        url = f"{model_config.base_url.rstrip('/')}/chat/completions"

        # 准备请求数据 - 完全透传，只替换必要的字段
        request_body = request_data.copy()
        request_body["model"] = model_config.model  # 替换为实际的模型名称

        headers = {
            "Authorization": f"Bearer {model_config.api_key}",
            "Content-Type": "application/json"
        }

        # 记录请求详情（但不记录敏感信息如API密钥）
        safe_request_data = request_data.copy()
        if "messages" in safe_request_data and len(str(safe_request_data["messages"])) > 200:
            safe_request_data["messages"] = f"[{len(safe_request_data['messages'])} messages, truncated]"

        try:
            start_time = time.time()
            logger.info(f"调用模型: {model_config.name} ({model_config.model}) - 非流式")

            # 普通响应 - 保持总超时行为
            async with session.post(
                url,
                json=request_body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=model_config.timeout)
            ) as response:
                elapsed_time = time.time() - start_time

                if response.status == 200:
                    result = await response.json()

                    # 确保响应中包含 usage 字段，如果缺失则添加默认值
                    if "usage" not in result or result["usage"] is None:
                        result["usage"] = {
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": 0
                        }
                    else:
                        # 确保 usage 中的 token 字段不为 null
                        if result["usage"].get("prompt_tokens") is None:
                            result["usage"]["prompt_tokens"] = 0
                        if result["usage"].get("completion_tokens") is None:
                            result["usage"]["completion_tokens"] = 0
                        if result["usage"].get("total_tokens") is None:
                            result["usage"]["total_tokens"] = (
                                result["usage"].get("prompt_tokens", 0) +
                                result["usage"].get("completion_tokens", 0)
                            )

                    # 检查响应中是否包含 content 字段
                    if self._has_valid_content(result):
                        # 【新增】工具调用格式转换（如果启用）
                        if model_config.enable_tool_call_conversion:
                            try:
                                choices = result.get("choices", [])
                                if choices and len(choices) > 0:
                                    first_choice = choices[0]
                                    if "message" in first_choice and isinstance(first_choice["message"], dict):
                                        message = first_choice["message"]
                                        content = message.get("content", "")
                                        existing_tool_calls = message.get("tool_calls", [])

                                        # 当 tool_calls 为空且 content 非空时，尝试转换
                                        if (not existing_tool_calls or len(existing_tool_calls) == 0) and content:
                                            converted_tool_calls, remaining_content = ToolCallConverter.convert_to_standard_format(
                                                content=content,
                                                existing_tool_calls=existing_tool_calls
                                            )

                                            if converted_tool_calls and len(converted_tool_calls) > 0:
                                                # 转换成功，更新 message
                                                message["tool_calls"] = converted_tool_calls
                                                message["content"] = remaining_content
                                                logger.info(
                                                    f"Tool call converted successfully | "
                                                    f"model={model_config.name} | "
                                                    f"tool_calls_count={len(converted_tool_calls)}"
                                                )
                            except Exception as e:
                                logger.warning(
                                    f"Tool call conversion failed, forwarding as-is | "
                                    f"model={model_config.name} | error={str(e)}"
                                )
                        else:
                            logger.debug(f"Tool call conversion disabled for model {model_config.name}")

                        return True, result
                    else:
                        error_msg = f"模型 {model_config.name} 返回的响应缺少有效的 content 字段"
                        logger.warning(error_msg)
                        # 使用错误分类器进行分类
                        classified_error = ErrorClassifier.classify_invalid_response(
                            model_config.name, error_msg
                        )
                        logger.info(f"错误分类结果: {ErrorClassifier.get_error_summary(classified_error)}")
                        return False, classified_error
                else:
                    error_text = await response.text()
                    logger.warning(f"模型 {model_config.name} 返回错误: {response.status} - {error_text}")

                    # 使用错误分类器进行分类
                    classified_error = ErrorClassifier.classify_http_error(
                        response.status, error_text, model_config.name
                    )

                    logger.info(f"错误分类结果: {ErrorClassifier.get_error_summary(classified_error)}")

                    return False, classified_error

        except asyncio.TimeoutError:
            elapsed_time = time.time() - start_time
            # 使用错误分类器进行分类
            classified_error = ErrorClassifier.classify_timeout_error(
                model_config.name, elapsed_time, model_config.timeout
            )
            logger.warning(classified_error.message)
            logger.info(f"错误分类结果: {ErrorClassifier.get_error_summary(classified_error)}")
            return False, classified_error
        except aiohttp.ClientError as e:
            elapsed_time = time.time() - start_time
            # 使用错误分类器分类连接错误
            classified_error = ErrorClassifier.classify_connection_error(e, model_config.name)
            logger.warning(classified_error.message)
            logger.info(f"错误分类结果: {ErrorClassifier.get_error_summary(classified_error)}")
            return False, classified_error
        except Exception as e:
            elapsed_time = time.time() - start_time
            # 使用错误分类器分类未知错误
            classified_error = ErrorClassifier.classify_unknown_error(e, model_config.name, elapsed_time)
            logger.warning(classified_error.message)

            logger.info(f"错误分类结果: {ErrorClassifier.get_error_summary(classified_error)}")
            return False, classified_error

    async def _try_single_model_with_retry(
        self,
        model_config: ModelConfig,
        request_data: Dict[str, Any],
        is_stream: bool,
    ) -> Dict[str, Any]:
        max_retries = model_config.retry_count
        """
        尝试单个模型调用，支持基于错误类型的智能重试

        Args:
            model_config: 模型配置
            request_data: 请求数据
            is_stream: 是否为流式调用
            max_retries: 最大重试次数，默认3次

        Returns:
            Dict: {"success": bool, "data": Any, "error": str}
        """
        for attempt in range(1, max_retries + 1):
            if is_stream:
                success, result = await self.call_model_stream(model_config, request_data)
            else:
                success, result = await self.call_model_non_stream(model_config, request_data)

            if success:
                logger.info(f"模型 {model_config.name} 调用成功（{'流式' if is_stream else '非流式'}）")
                return {"success": True, "data": result, "error": None}

            # 根据错误类型决定是否重试
            if isinstance(result, ClassifiedError):
                classified_error = result
                error_summary = ErrorClassifier.get_error_summary(classified_error)
                logger.warning(
                    f"模型 {model_config.name} 第 {attempt}/{max_retries} 次尝试失败: {error_summary}"
                )

                # 如果错误分类建议禁用模型，则禁用
                if classified_error.should_disable_model:
                    if model_config.quota_period is not None:
                        logger.warning(
                            f"模型 {model_config.name} 被标记为周期内用完（错误类型: {classified_error.category.value}）"
                        )
                        await self.model_state_manager.disable_model_for_period(model_config)
                    # 配置了 quota_period 的模型禁用后不再重试
                    return {"success": False, "error": classified_error.message, "data": None}

                # 如果错误不可重试，直接返回失败
                if not classified_error.is_retryable:
                    return {"success": False, "error": classified_error.message, "data": None}

                # 可重试的错误，等待延迟后继续
                if attempt < max_retries:
                    delay = classified_error.retry_delay_seconds
                    logger.info(f"将在 {delay:.1f} 秒后进行第 {attempt + 1} 次重试...")
                    await asyncio.sleep(delay)
            else:
                # 兼容旧版本的字符串错误
                error_msg = str(result)
                logger.warning(f"模型 {model_config.name} 第 {attempt}/{max_retries} 次尝试失败: {error_msg}")
                if model_config.quota_period is not None:
                    logger.warning(f"模型 {model_config.name} 失败，标记为周期内用完...")
                    await self.model_state_manager.disable_model_for_period(model_config)
                    return {"success": False, "error": error_msg, "data": None}

                # 旧版字符串错误，等待后重试
                if attempt < max_retries:
                    await asyncio.sleep(2)

        # 所有重试均失败
        last_error = str(result.message) if isinstance(result, ClassifiedError) else str(result)
        logger.error(f"模型 {model_config.name} 在 {max_retries} 次尝试后仍失败: {last_error}")
        return {"success": False, "error": last_error, "data": None}

    async def _try_platform_models_non_stream(self, platform_name: str, platform_models: List[ModelConfig], request_data: Dict[str, Any]) -> Dict[str, Any]:
        """尝试平台内的模型（非流式），支持轮询机制"""
        # 过滤启用且当前周期内可用的模型
        available_models = []
        for model_config in platform_models:
            if model_config.enabled and await self.model_state_manager.is_model_available(model_config):
                available_models.append(model_config)

        if not available_models:

            return {"success": False, "error": f"平台 {platform_name} 无可用模型", "data": None}

        # 故障转移模式：总是从索引0开始尝试（最高优先级）
        models_to_try = available_models  # 保持原始顺序，索引0为首选

        # 按顺序尝试每个模型
        for i, model_config in enumerate(models_to_try):

            success, result = await self.call_model_non_stream(model_config, request_data)

            if success:
                logger.info(f"模型 {model_config.name} 调用成功（非流式）")

                return {"success": True, "data": result, "error": None}
            else:
                # 根据错误分类决定是否禁用模型
                if isinstance(result, ClassifiedError):
                    classified_error = result
                    error_summary = ErrorClassifier.get_error_summary(classified_error)
                    logger.warning(f"模型 {model_config.name} 失败: {error_summary}")

                    # 如果错误分类建议禁用模型，则禁用
                    if classified_error.should_disable_model:
                        if model_config.quota_period is not None:
                            # 配置了 quota_period，持久化禁用
                            logger.warning(f"模型 {model_config.name} 被标记为周期内用完（错误类型: {classified_error.category.value}）")
                            await self.model_state_manager.disable_model_for_period(model_config)
                        else:
                            # 未配置 quota_period，临时禁用（仅在本次请求的剩余尝试中）
                            pass
                    else:
                        pass
                else:
                    # 兼容旧版本的字符串错误
                    logger.warning(f"模型 {model_config.name} 失败: {str(result)}")
                    if model_config.quota_period is not None:
                        logger.warning(f"模型 {model_config.name} 失败，标记为周期内用完...")
                        await self.model_state_manager.disable_model_for_period(model_config)
                    else:
                        pass

                # 如果是最后一个模型，返回错误
                if i == len(models_to_try) - 1:
                    error_message = str(result.message) if isinstance(result, ClassifiedError) else str(result)
                    return {"success": False, "error": error_message, "data": None}
                else:
                    pass

        return {"success": False, "error": "未知错误", "data": None}

    async def _try_platform_models_stream(self, platform_name: str, platform_models: List[ModelConfig], request_data: Dict[str, Any]) -> Dict[str, Any]:
        """尝试平台内的模型（流式），支持轮询机制"""
        # 过滤启用且当前周期内可用的模型
        available_models = []
        for model_config in platform_models:
            if model_config.enabled and await self.model_state_manager.is_model_available(model_config):
                available_models.append(model_config)

        if not available_models:

            return {"success": False, "error": f"平台 {platform_name} 无可用模型", "data": None}

        # 故障转移模式：总是从索引0开始尝试（最高优先级）
        models_to_try = available_models  # 保持原始顺序，索引0为首选

        # 按顺序尝试每个模型
        for i, model_config in enumerate(models_to_try):

            success, result = await self.call_model_stream(model_config, request_data)

            if success:
                logger.info(f"模型 {model_config.name} 调用成功（流式）")

                return {"success": True, "data": result, "error": None}
            else:
                # 根据错误分类决定是否禁用模型
                if isinstance(result, ClassifiedError):
                    classified_error = result
                    error_summary = ErrorClassifier.get_error_summary(classified_error)
                    logger.warning(f"模型 {model_config.name} 失败: {error_summary}")

                    # 如果错误分类建议禁用模型，则禁用
                    if classified_error.should_disable_model:
                        if model_config.quota_period is not None:
                            # 配置了 quota_period，持久化禁用
                            logger.warning(f"模型 {model_config.name} 被标记为周期内用完（错误类型: {classified_error.category.value}）")
                            await self.model_state_manager.disable_model_for_period(model_config)
                        else:
                            # 未配置 quota_period，临时禁用（仅在本次请求的剩余尝试中）
                            pass
                    else:
                        pass
                else:
                    # 兼容旧版本的字符串错误
                    logger.warning(f"模型 {model_config.name} 失败: {str(result)}")
                    if model_config.quota_period is not None:
                        logger.warning(f"模型 {model_config.name} 失败，标记为周期内用完...")
                        await self.model_state_manager.disable_model_for_period(model_config)
                    else:
                        pass

                # 如果是最后一个模型，返回错误
                if i == len(models_to_try) - 1:
                    error_message = str(result.message) if isinstance(result, ClassifiedError) else str(result)
                    return {"success": False, "error": error_message, "data": None}
                else:
                    pass

        return {"success": False, "error": "未知错误", "data": None}

    def _collect_alias_models(self, alias_name: str) -> Dict[str, List[ModelConfig]]:
        """解析别名，收集所有匹配的模型并按平台分组

        Args:
            alias_name: 别名名称

        Returns:
            按平台分组的模型字典 {platform: [ModelConfig, ...]}
        """
        selectors = self.all_aliases.get(alias_name, [])
        result: Dict[str, List[ModelConfig]] = {}
        seen: set = set()

        for selector in selectors:
            if selector == "all":
                for platform, platform_models in self.models.items():
                    for mc in platform_models:
                        key = mc.name
                        if key not in seen:
                            result.setdefault(platform, []).append(mc)
                            seen.add(key)
            elif "|" in selector:
                platform, model_part = selector.split("|", 1)
                platform = platform.strip()
                model_part = model_part.strip()
                if platform in self.models:
                    for mc in self.models[platform]:
                        if mc.name == model_part or mc.model == model_part:
                            key = mc.name
                            if key not in seen:
                                result.setdefault(platform, []).append(mc)
                                seen.add(key)
            else:
                platform = selector.strip()
                if platform in self.models:
                    for mc in self.models[platform]:
                        key = mc.name
                        if key not in seen:
                            result.setdefault(platform, []).append(mc)
                            seen.add(key)

        return result

    async def _try_platforms_by_weight(self, platforms: Dict[str, List[ModelConfig]], request_data: Dict[str, Any], is_stream: bool) -> Dict[str, Any]:
        """按权重降序尝试各平台的模型

        Args:
            platforms: 按平台分组的模型字典
            request_data: 请求数据
            is_stream: 是否为流式

        Returns:
            调用结果
        """
        platform_weights = {}
        for platform_name, platform_models in platforms.items():
            if platform_models:
                platform_weights[platform_name] = platform_models[0].weight
            else:
                platform_weights[platform_name] = 0

        platforms_to_try = sorted(
            platforms.keys(),
            key=lambda x: (-platform_weights.get(x, 0), list(platforms.keys()).index(x))
        )

        last_error = None
        for platform_name in platforms_to_try:
            if is_stream:
                result = await self._try_platform_models_stream(platform_name, platforms[platform_name], request_data)
            else:
                result = await self._try_platform_models_non_stream(platform_name, platforms[platform_name], request_data)
            if result["success"]:
                return result
            last_error = result["error"]

        return {"success": False, "error": last_error, "data": None}

    async def chat_completion_non_stream(self, request_data: Dict[str, Any]) -> Any:
        """
        执行非流式聊天完成请求，支持自动重试切换模型 - 完全参数透传
        实现按权重排序的故障转移机制：平台按照weight字段从高到低排序（weight值越大优先级越高）

        支持三种模型选择器格式：
        1. "all" - 所有平台所有模型
        2. "{platform}" - 指定平台的所有模型
        3. "{platform}|{model_name}" - 指定平台的指定模型
        4. "{alias}" - 模型组别名（取并集）
        """
        # 处理可选参数的默认值
        model_selector = request_data.get("model", "all")
        messages = request_data.get("messages")

        # 验证必要参数
        if not messages:
            raise HTTPException(status_code=400, detail="messages参数是必需的")

        # 解析模型选择器
        platform_or_all, model_name = self._parse_model_selector(model_selector)

        # 检查是否是模型组别名
        if model_selector in self.all_aliases:
            platforms = self._collect_alias_models(model_selector)
            if not platforms:
                raise HTTPException(status_code=400, detail=f"别名 '{model_selector}' 未匹配到任何模型")

            result = await self._try_platforms_by_weight(platforms, request_data, is_stream=False)
            if result["success"]:
                return result["data"]
            raise HTTPException(status_code=500, detail=f"别名 '{model_selector}' 所有模型都不可用: {result['error']}")

        if platform_or_all == "all":
            # 模式1: all - 所有平台所有模型
            all_platforms = list(self.models.keys())

            if not all_platforms:
                logger.error("DEBUG: 无可用模型配置")
                raise HTTPException(status_code=400, detail="无可用模型配置")

            # 创建平台权重映射
            platform_weights = {}
            for platform_name in all_platforms:
                # 获取平台的第一个模型来获取weight（同一平台内所有模型weight相同）
                if self.models[platform_name]:
                    platform_weights[platform_name] = self.models[platform_name][0].weight
                else:
                    platform_weights[platform_name] = 0

            # 按权重降序排序（权重高的优先），权重相同时保持原有顺序
            platforms_to_try = sorted(
                all_platforms,
                key=lambda x: (-platform_weights.get(x, 0), all_platforms.index(x))
            )

            # 尝试每个平台
            last_error = None
            for platform_name in platforms_to_try:
                platform_models = self.models[platform_name]
                result = await self._try_platform_models_non_stream(platform_name, platform_models, request_data)
                if result["success"]:
                    return result["data"]
                else:
                    last_error = result["error"]

            logger.error(f"所有平台都失败了，最后错误: {last_error}")
            raise HTTPException(status_code=500, detail=f"所有模型都不可用: {last_error}")
        else:
            # 模式2或模式3: 指定平台
            platform_name = platform_or_all
            if platform_name not in self.models or not self.models[platform_name]:
                logger.error(f"DEBUG: 平台 '{platform_name}' 未配置或无可用模型")
                raise HTTPException(status_code=400, detail=f"平台 '{platform_name}' 未配置或无可用模型")

            if model_name is None:
                # 模式2: {platform} - 指定平台的所有模型
                platform_models = self.models[platform_name]
                result = await self._try_platform_models_non_stream(platform_name, platform_models, request_data)
                if result["success"]:
                    return result["data"]
                else:
                    logger.error(f"指定平台 '{platform_name}' 所有模型都失败了: {result['error']}")
                    raise HTTPException(status_code=500, detail=f"平台 '{platform_name}' 所有模型都不可用: {result['error']}")
            else:
                # 模式3: {platform}|{model_name} - 指定平台的指定模型
                platform_models = self.models[platform_name]
                target_model = None
                for model_config in platform_models:
                    if model_config.name == model_name or model_config.model == model_name:
                        target_model = model_config
                        break

                if target_model is None:
                    logger.error(f"DEBUG: 模型 '{model_name}' 在平台 '{platform_name}' 中未找到")
                    raise HTTPException(status_code=400, detail=f"模型 '{model_name}' 在平台 '{platform_name}' 中未找到")

                # 模式3: 指定单个模型，使用带重试的方法调用
                retry_result = await self._try_single_model_with_retry(
                    target_model, request_data, is_stream=False
                )
                if retry_result["success"]:
                    logger.info(f"模型 {target_model.name} 调用成功（非流式）")
                    return retry_result["data"]
                else:
                    error_message = retry_result["error"]
                    logger.error(f"模型 '{model_name}' 调用失败: {error_message}")
                    raise HTTPException(status_code=500, detail=f"模型 '{model_name}' 调用失败: {error_message}")

    async def chat_completion_stream(self, request_data: Dict[str, Any]) -> Any:
        """
        执行流式聊天完成请求，支持自动重试切换模型 - 完全参数透传
        实现按权重排序的故障转移机制：平台按照weight字段从高到低排序（weight值越大优先级越高）
        注意：流式请求必须在开始传输前完成所有模型选择，不能在流式过程中切换

        支持三种模型选择器格式：
        1. "all" - 所有平台所有模型
        2. "{platform}" - 指定平台的所有模型
        3. "{platform}|{model_name}" - 指定平台的指定模型
        4. "{alias}" - 模型组别名（取并集）
        """
        # 处理可选参数的默认值
        model_selector = request_data.get("model", "all")
        messages = request_data.get("messages")

        # 验证必要参数
        if not messages:
            raise HTTPException(status_code=400, detail="messages参数是必需的")

        # 解析模型选择器
        platform_or_all, model_name = self._parse_model_selector(model_selector)

        # 检查是否是模型组别名
        if model_selector in self.all_aliases:
            platforms = self._collect_alias_models(model_selector)
            if not platforms:
                raise HTTPException(status_code=400, detail=f"别名 '{model_selector}' 未匹配到任何模型")

            result = await self._try_platforms_by_weight(platforms, request_data, is_stream=True)
            if result["success"]:
                return result["data"]
            raise HTTPException(status_code=500, detail=f"别名 '{model_selector}' 所有模型都不可用: {result['error']}")

        if platform_or_all == "all":
            # 模式1: all - 所有平台所有模型
            all_platforms = list(self.models.keys())

            if not all_platforms:
                logger.error("DEBUG: 无可用模型配置")
                raise HTTPException(status_code=400, detail="无可用模型配置")

            # 创建平台权重映射
            platform_weights = {}
            for platform_name in all_platforms:
                # 获取平台的第一个模型来获取weight（同一平台内所有模型weight相同）
                if self.models[platform_name]:
                    platform_weights[platform_name] = self.models[platform_name][0].weight
                else:
                    platform_weights[platform_name] = 0

            # 按权重降序排序（权重高的优先），权重相同时保持原有顺序
            platforms_to_try = sorted(
                all_platforms,
                key=lambda x: (-platform_weights.get(x, 0), all_platforms.index(x))
            )

            # 尝试每个平台
            last_error = None
            for platform_name in platforms_to_try:
                platform_models = self.models[platform_name]
                result = await self._try_platform_models_stream(platform_name, platform_models, request_data)
                if result["success"]:
                    return result["data"]
                else:
                    last_error = result["error"]

            logger.error(f"所有平台都失败了，最后错误: {last_error}")
            raise HTTPException(status_code=500, detail=f"所有模型都不可用: {last_error}")
        else:
            # 模式2或模式3: 指定平台
            platform_name = platform_or_all
            if platform_name not in self.models or not self.models[platform_name]:
                logger.error(f"DEBUG: 平台 '{platform_name}' 未配置或无可用模型")
                raise HTTPException(status_code=400, detail=f"平台 '{platform_name}' 未配置或无可用模型")

            if model_name is None:
                # 模式2: {platform} - 指定平台的所有模型
                platform_models = self.models[platform_name]
                result = await self._try_platform_models_stream(platform_name, platform_models, request_data)
                if result["success"]:
                    return result["data"]
                else:
                    logger.error(f"指定平台 '{platform_name}' 所有模型都失败了: {result['error']}")
                    raise HTTPException(status_code=500, detail=f"平台 '{platform_name}' 所有模型都不可用: {result['error']}")
            else:
                # 模式3: {platform}|{model_name} - 指定平台的指定模型
                platform_models = self.models[platform_name]
                target_model = None
                for model_config in platform_models:
                    if model_config.name == model_name or model_config.model == model_name:
                        target_model = model_config
                        break

                if target_model is None:
                    logger.error(f"DEBUG: 模型 '{model_name}' 在平台 '{platform_name}' 中未找到")
                    raise HTTPException(status_code=400, detail=f"模型 '{model_name}' 在平台 '{platform_name}' 中未找到")

                # 模式3: 指定单个模型，使用带重试的方法调用
                retry_result = await self._try_single_model_with_retry(
                    target_model, request_data, is_stream=True
                )
                if retry_result["success"]:
                    logger.info(f"模型 {target_model.name} 调用成功（流式）")
                    return retry_result["data"]
                else:
                    error_message = retry_result["error"]
                    logger.error(f"模型 '{model_name}' 调用失败: {error_message}")
                    raise HTTPException(status_code=500, detail=f"模型 '{model_name}' 调用失败: {error_message}")

    async def close(self):
        """关闭会话"""
        if self.session and not self.session.closed:
            await self.session.close()
