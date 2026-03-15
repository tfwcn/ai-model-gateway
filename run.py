#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenAI代理服务 - 支持多平台模型自动切换重试
"""

import os
import json
import yaml
import asyncio
import logging
import re
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime, timedelta
import time
from contextlib import asynccontextmanager

import aiohttp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# 加载环境变量（支持 .env 文件）
from dotenv import load_dotenv
load_dotenv()

# 配置日志 - 通过环境变量 DEBUG_LOGS 控制日志级别
log_level = logging.DEBUG if os.getenv('DEBUG_LOGS', '').lower() in ('true', '1', 'yes') else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger(__name__)

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


@dataclass
class ModelConfig:
    """模型配置"""
    name: str
    api_key: str
    base_url: str
    model: str
    timeout: int = 30
    weight: int = 1  # 权重，用于负载均衡（可选）
    enabled: bool = True
    quota_period: Optional[str] = None  # 额度刷新周期: "daily", "weekly", "monthly", "hourly" (保留字段名但不再使用)


class ModelStateManager:
    """模型状态管理器 - 跟踪模型在当前周期内的可用性"""
    
    def __init__(self):
        # 存储模型失效时间: {model_name: expiry_time}
        self.disabled_models = {}
        self.lock = asyncio.Lock()
    
    def _get_period_expiry(self, period: str) -> datetime:
        """获取当前周期的结束时间"""
        now = datetime.now()
        
        if period == "hourly":
            return (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        elif period == "daily":
            return (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
        elif period == "weekly":
            # 周一为每周开始，周日结束
            days_until_sunday = 6 - now.weekday()
            week_start = (now - timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            return week_start + timedelta(days=7)
        elif period == "monthly":
            if now.month == 12:
                return datetime(now.year + 1, 1, 1)
            else:
                return datetime(now.year, now.month + 1, 1)
        else:
            # 默认为每日
            return (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
    
    async def disable_model_for_period(self, model_config: ModelConfig):
        """标记模型在当前周期内已用完"""
        if not model_config.quota_period:
            return  # 无周期限制，不标记
        
        async with self.lock:
            expiry_time = self._get_period_expiry(model_config.quota_period)
            self.disabled_models[model_config.name] = expiry_time
            logger.debug(f"DEBUG: 模型 {model_config.name} 已被标记为周期内用完，失效时间: {expiry_time}")
    
    async def is_model_available(self, model_config: ModelConfig) -> bool:
        """检查模型是否可用（未被标记为周期内用完）"""
        if not model_config.quota_period:
            return True  # 无周期限制，始终可用
        
        async with self.lock:
            # 清理过期的禁用记录
            now = datetime.now()
            expired_models = []
            for model_name, expiry_time in self.disabled_models.items():
                if now >= expiry_time:
                    expired_models.append(model_name)
            
            for model_name in expired_models:
                del self.disabled_models[model_name]
                logger.debug(f"DEBUG: 模型 {model_name} 的禁用状态已过期，重新启用")
            
            is_available = model_config.name not in self.disabled_models
            logger.debug(f"DEBUG: 模型 {model_config.name} 可用性检查结果: {is_available}")
            return is_available


class ModelRoundRobinManager:
    """模型轮询管理器 - 跟踪每个平台的轮询位置"""
    
    def __init__(self):
        # 存储每个平台的轮询索引: {platform_name: current_index}
        self.round_robin_index = {}
        self.lock = asyncio.Lock()
    
    async def get_next_model_index(self, platform_name: str, available_models_count: int) -> int:
        """获取平台的下一个模型索引"""
        if available_models_count <= 0:
            return 0
        
        async with self.lock:
            current_index = self.round_robin_index.get(platform_name, 0)
            # 确保索引在有效范围内
            next_index = current_index % available_models_count
            # 更新为下一个位置
            self.round_robin_index[platform_name] = (current_index + 1) % available_models_count
            return next_index
    
    async def reset_platform_index(self, platform_name: str):
        """重置平台的轮询索引"""
        async with self.lock:
            self.round_robin_index[platform_name] = 0


class OpenAIProxy:
    """OpenAI代理服务核心类"""
    
    def __init__(self, config_file: str = "models.yaml"):
        self.config_file = config_file
        self.models: Dict[str, List[ModelConfig]] = {}
        self.session: Optional[aiohttp.ClientSession] = None
        self.model_state_manager = ModelStateManager()  # 添加模型状态管理器
        self.round_robin_manager = ModelRoundRobinManager()  # 添加轮询管理器
        self.load_config()
    
    def load_config(self):
        """加载模型配置"""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    if self.config_file.endswith('.yaml') or self.config_file.endswith('.yml'):
                        config_data = yaml.safe_load(f)
                    else:
                        config_data = json.load(f)
                
                # 解析新格式的配置
                self.models = {}
                for platform_name, platform_config in config_data.items():
                    if not isinstance(platform_config, dict):
                        continue
                    
                    base_url = platform_config.get('baseUrl')
                    # 解析并替换配置中的环境变量占位符
                    config_api_key = platform_config.get('apiKey')
                    if isinstance(config_api_key, str):
                        resolved_api_key = resolve_env_vars(config_api_key)
                    else:
                        resolved_api_key = config_api_key
                    
                    # 优先从环境变量获取API密钥，格式为 {PLATFORM_NAME}_API_KEY
                    env_api_key = os.getenv(f"{platform_name.upper()}_API_KEY")
                    api_key = env_api_key if env_api_key else resolved_api_key
                    models_list = platform_config.get('models', [])
                    timeout = platform_config.get('timeout', 30)
                    weight = platform_config.get('weight', 1)
                    enabled = platform_config.get('enabled', True)
                    quota_period = platform_config.get('quota_period')  # 支持 quota_period 配置
                    
                    if not base_url or not api_key or not models_list:
                        logger.warning(f"跳过无效的平台配置: {platform_name}")
                        continue
                    
                    self.models[platform_name] = []
                    for model_name in models_list:
                        model_config = ModelConfig(
                            name=f"{platform_name}-{model_name.replace('/', '-')}",
                            api_key=api_key,
                            base_url=base_url,
                            model=model_name,
                            timeout=timeout,
                            weight=weight,
                            enabled=enabled,
                            quota_period=quota_period
                        )
                        self.models[platform_name].append(model_config)
                
                logger.info(f"成功加载配置文件: {self.config_file}")
                logger.info(f"可用模型组: {list(self.models.keys())}")
                # 添加调试日志显示所有加载的模型
                for platform, models in self.models.items():
                    logger.debug(f"DEBUG: 平台 {platform} 加载了 {len(models)} 个模型: {[m.name for m in models]}")
            except Exception as e:
                logger.error(f"加载配置文件失败: {e}")
                self._create_default_config()
        else:
            self._create_default_config()
    
    def _create_default_config(self):
        """创建默认配置文件"""
        default_config = {
            "modelscope": {
                "baseUrl": "https://api-inference.modelscope.cn/v1",
                "apiKey": "${MODELSCOPE_API_KEY}",  # 使用环境变量占位符
                "models": ["Qwen/Qwen3.5-397B-A17B", "ZhipuAI/GLM-5"],
                "timeout": 30,
                "weight": 1,
                "enabled": True,
                "quota_period": "daily"
            },
            "openai": {
                "baseUrl": "https://api.openai.com/v1", 
                "apiKey": "${OPENAI_API_KEY}",  # 使用环境变量占位符
                "models": ["gpt-4", "gpt-3.5-turbo"],
                "timeout": 30,
                "weight": 1,
                "enabled": False,
                "quota_period": "daily"
            }
        }
        
        # 创建YAML配置文件
        with open(self.config_file, 'w', encoding='utf-8') as f:
            yaml.dump(default_config, f, default_flow_style=False, allow_unicode=True, indent=2)
        
        logger.info(f"已创建默认配置文件: {self.config_file}")
        logger.info("请编辑配置文件并设置正确的API密钥和端点")
    
    async def get_session(self) -> aiohttp.ClientSession:
        """获取HTTP会话"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60),
                headers={"Content-Type": "application/json"}
            )
            logger.debug("DEBUG: 创建了新的HTTP会话")
        else:
            logger.debug("DEBUG: 复用现有的HTTP会话")
        return self.session
    
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
        
        logger.debug(f"DEBUG: 准备调用模型 {model_config.name} (流式)")
        logger.debug(f"DEBUG: 请求URL: {url}")
        logger.debug(f"DEBUG: 请求超时: {model_config.timeout}秒")
        logger.debug(f"DEBUG: 请求数据: {safe_request_data}")
        
        try:
            start_time = time.time()
            logger.info(f"调用模型: {model_config.name} ({model_config.model}) - 流式")
            
            # 流式响应
            response = await session.post(
                url, 
                json=request_body, 
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=model_config.timeout)
            )
            elapsed_time = time.time() - start_time
            logger.debug(f"DEBUG: 模型 {model_config.name} 流式请求完成，耗时: {elapsed_time:.2f}秒")
            
            # 读取第一个数据块来检查是否是错误响应或缺少content
            try:
                first_chunk = await response.content.read(1024)
                if first_chunk:
                    chunk_str = first_chunk.decode('utf-8', errors='replace')
                    # 检查是否是JSON错误格式（以{开头且包含"error"）
                    if chunk_str.strip().startswith('{'):
                        try:
                            json_data = json.loads(chunk_str)
                            # 检查是否包含错误信息
                            if isinstance(json_data, dict):
                                # 如果包含error字段，说明是错误响应
                                if "error" in json_data:
                                    error_msg = f"流式响应返回错误: {chunk_str}"
                                    logger.warning(error_msg)
                                    # 关闭响应以释放资源
                                    response.close()
                                    return False, error_msg
                                
                                # 如果不包含error字段，检查是否有有效的content字段
                                if not self._has_valid_content(json_data):
                                    error_msg = f"流式响应缺少有效的content字段: {chunk_str}"
                                    logger.warning(error_msg)
                                    # 关闭响应以释放资源
                                    response.close()
                                    return False, error_msg
                                    
                        except (json.JSONDecodeError, ValueError):
                            # 不是有效的JSON，可能是正常的流式数据（如SSE格式）
                            pass
                
                # 如果不是错误，创建一个包装对象包含原始响应和预读取的数据
                class StreamResponseWrapper:
                    def __init__(self, original_response, preloaded_data):
                        self.original_response = original_response
                        self.preloaded_data = preloaded_data
                        self.first_chunk_sent = False
                    
                    async def iter_any(self):
                        if self.preloaded_data and not self.first_chunk_sent:
                            self.first_chunk_sent = True
                            yield self.preloaded_data
                        async for chunk in self.original_response.content.iter_any():
                            yield chunk
                
                wrapped_response = StreamResponseWrapper(response, first_chunk)
                return True, wrapped_response
                
            except Exception as e:
                logger.debug(f"DEBUG: 检查流式响应时发生异常: {e}")
                # 如果检查失败，假设是正常的流式响应
                return True, response
        except asyncio.TimeoutError:
            elapsed_time = time.time() - start_time
            error_msg = f"模型 {model_config.name} 请求超时 (耗时: {elapsed_time:.2f}秒, 超时阈值: {model_config.timeout}秒)"
            logger.warning(error_msg)
            return False, error_msg
        except Exception as e:
            elapsed_time = time.time() - start_time
            error_msg = f"模型 {model_config.name} 调用异常: {str(e)} (耗时: {elapsed_time:.2f}秒)"
            logger.warning(error_msg)
            logger.debug(f"DEBUG: 异常详细信息: {repr(e)}", exc_info=True)
            return False, error_msg
    
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
        
        logger.debug(f"DEBUG: 准备调用模型 {model_config.name} (非流式)")
        logger.debug(f"DEBUG: 请求URL: {url}")
        logger.debug(f"DEBUG: 请求超时: {model_config.timeout}秒")
        logger.debug(f"DEBUG: 请求数据: {safe_request_data}")
        
        try:
            start_time = time.time()
            logger.info(f"调用模型: {model_config.name} ({model_config.model}) - 非流式")
            
            # 普通响应
            async with session.post(
                url, 
                json=request_body, 
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=model_config.timeout)
            ) as response:
                elapsed_time = time.time() - start_time
                logger.debug(f"DEBUG: 模型 {model_config.name} 请求完成，状态码: {response.status}, 耗时: {elapsed_time:.2f}秒")
                
                if response.status == 200:
                    result = await response.json()
                    logger.debug(f"DEBUG: 模型 {model_config.name} 返回成功响应")
                    
                    # 检查响应中是否包含 content 字段
                    if self._has_valid_content(result):
                        return True, result
                    else:
                        error_msg = f"模型 {model_config.name} 返回的响应缺少有效的 content 字段"
                        logger.warning(error_msg)
                        return False, error_msg
                else:
                    error_text = await response.text()
                    logger.warning(f"模型 {model_config.name} 返回错误: {response.status} - {error_text}")
                    
                    return False, f"HTTP {response.status}: {error_text}"
                    
        except asyncio.TimeoutError:
            elapsed_time = time.time() - start_time
            error_msg = f"模型 {model_config.name} 请求超时 (耗时: {elapsed_time:.2f}秒, 超时阈值: {model_config.timeout}秒)"
            logger.warning(error_msg)
            return False, error_msg
        except Exception as e:
            elapsed_time = time.time() - start_time
            error_msg = f"模型 {model_config.name} 调用异常: {str(e)} (耗时: {elapsed_time:.2f}秒)"
            logger.warning(error_msg)
            logger.debug(f"DEBUG: 异常详细信息: {repr(e)}", exc_info=True)
            return False, error_msg
    
    async def chat_completion_non_stream(self, request_data: Dict[str, Any]) -> Any:
        """
        执行非流式聊天完成请求，支持自动重试切换模型 - 完全参数透传
        实现平台内模型的轮询切换机制，只要模型调用失败就认为达到限额
        """
        logger.debug("DEBUG: 开始处理非流式聊天完成请求")
        logger.debug(f"DEBUG: 原始请求数据: {request_data}")
        
        # 处理可选参数的默认值
        model_group = request_data.get("model")
        messages = request_data.get("messages")
        
        # 验证必要参数
        if not messages:
            logger.error("DEBUG: 请求缺少messages参数")
            raise HTTPException(status_code=400, detail="messages参数是必需的")
        
        logger.debug(f"DEBUG: 请求模型组: {model_group}")
        logger.debug(f"DEBUG: 消息数量: {len(messages)}")
        
        if model_group is None or model_group == "all":
            # 遍历所有平台，按轮询顺序尝试
            all_platforms = list(self.models.keys())
            if not all_platforms:
                logger.error("DEBUG: 无可用模型配置")
                raise HTTPException(status_code=400, detail="无可用模型配置")
            
            # 尝试每个平台
            last_error = None
            for platform_name in all_platforms:
                platform_models = self.models[platform_name]
                result = await self._try_platform_models_non_stream(platform_name, platform_models, request_data)
                if result["success"]:
                    return result["data"]
                else:
                    last_error = result["error"]
            
            logger.error(f"所有平台都失败了，最后错误: {last_error}")
            raise HTTPException(status_code=500, detail=f"所有模型都不可用: {last_error}")
        else:
            # 指定特定平台
            if model_group not in self.models or not self.models[model_group]:
                logger.error(f"DEBUG: 模型组 '{model_group}' 未配置或无可用模型")
                raise HTTPException(status_code=400, detail=f"模型组 '{model_group}' 未配置或无可用模型")
            
            platform_models = self.models[model_group]
            result = await self._try_platform_models_non_stream(model_group, platform_models, request_data)
            if result["success"]:
                return result["data"]
            else:
                logger.error(f"指定平台 '{model_group}' 所有模型都失败了: {result['error']}")
                raise HTTPException(status_code=500, detail=f"模型组 '{model_group}' 所有模型都不可用: {result['error']}")
    
    async def chat_completion_stream(self, request_data: Dict[str, Any]) -> Any:
        """
        执行流式聊天完成请求，支持自动重试切换模型 - 完全参数透传
        实现平台内模型的轮询切换机制，只要模型调用失败就认为达到限额
        注意：流式请求必须在开始传输前完成所有模型选择，不能在流式过程中切换
        """
        logger.debug("DEBUG: 开始处理流式聊天完成请求")
        logger.debug(f"DEBUG: 原始请求数据: {request_data}")
        
        # 处理可选参数的默认值
        model_group = request_data.get("model")
        messages = request_data.get("messages")
        
        # 验证必要参数
        if not messages:
            logger.error("DEBUG: 请求缺少messages参数")
            raise HTTPException(status_code=400, detail="messages参数是必需的")
        
        logger.debug(f"DEBUG: 请求模型组: {model_group}")
        logger.debug(f"DEBUG: 消息数量: {len(messages)}")
        
        if model_group is None or model_group == "all":
            # 遍历所有平台，按轮询顺序尝试
            all_platforms = list(self.models.keys())
            if not all_platforms:
                logger.error("DEBUG: 无可用模型配置")
                raise HTTPException(status_code=400, detail="无可用模型配置")
            
            # 尝试每个平台
            last_error = None
            for platform_name in all_platforms:
                platform_models = self.models[platform_name]
                result = await self._try_platform_models_stream(platform_name, platform_models, request_data)
                if result["success"]:
                    return result["data"]
                else:
                    last_error = result["error"]
            
            logger.error(f"所有平台都失败了，最后错误: {last_error}")
            raise HTTPException(status_code=500, detail=f"所有模型都不可用: {last_error}")
        else:
            # 指定特定平台
            if model_group not in self.models or not self.models[model_group]:
                logger.error(f"DEBUG: 模型组 '{model_group}' 未配置或无可用模型")
                raise HTTPException(status_code=400, detail=f"模型组 '{model_group}' 未配置或无可用模型")
            
            platform_models = self.models[model_group]
            result = await self._try_platform_models_stream(model_group, platform_models, request_data)
            if result["success"]:
                return result["data"]
            else:
                logger.error(f"指定平台 '{model_group}' 所有模型都失败了: {result['error']}")
                raise HTTPException(status_code=500, detail=f"模型组 '{model_group}' 所有模型都不可用: {result['error']}")
    
    async def _try_platform_models_non_stream(self, platform_name: str, platform_models: List[ModelConfig], request_data: Dict[str, Any]) -> Dict[str, Any]:
        """尝试平台内的模型（非流式），支持轮询机制"""
        # 过滤启用且当前周期内可用的模型
        available_models = []
        for model_config in platform_models:
            if model_config.enabled and await self.model_state_manager.is_model_available(model_config):
                available_models.append(model_config)
        
        if not available_models:
            logger.debug(f"DEBUG: 平台 {platform_name} 无可用模型（非流式）")
            return {"success": False, "error": f"平台 {platform_name} 无可用模型", "data": None}
        
        # 总是使用轮询机制，无论是否配置了 quota_period
        start_index = await self.round_robin_manager.get_next_model_index(platform_name, len(available_models))
        models_to_try = []
        # 从轮询位置开始，循环遍历所有可用模型
        for i in range(len(available_models)):
            idx = (start_index + i) % len(available_models)
            models_to_try.append(available_models[idx])
        
        logger.debug(f"DEBUG: 平台 {platform_name} 有 {len(models_to_try)} 个模型待尝试（非流式）: {[m.name for m in models_to_try]}")
        logger.debug(f"DEBUG: 平台 {platform_name} 轮询机制: 启用")
        
        # 按顺序尝试每个模型
        for i, model_config in enumerate(models_to_try):
            logger.debug(f"DEBUG: 平台 {platform_name} 尝试第 {i+1}/{len(models_to_try)} 个模型（非流式）: {model_config.name}")
            
            success, result = await self.call_model_non_stream(model_config, request_data)
            
            if success:
                logger.info(f"模型 {model_config.name} 调用成功（非流式）")
                logger.debug(f"DEBUG: 成功返回结果，类型: {type(result)}")
                return {"success": True, "data": result, "error": None}
            else:
                # 只要模型调用失败，就认为达到限额，标记为周期内用完
                if model_config.quota_period is not None:
                    # 配置了 quota_period，持久化禁用
                    logger.warning(f"模型 {model_config.name} 失败，标记为周期内用完...")
                    await self.model_state_manager.disable_model_for_period(model_config)
                else:
                    # 未配置 quota_period，临时禁用（仅在本次请求的剩余尝试中）
                    logger.debug(f"DEBUG: 模型 {model_config.name} 失败，临时禁用（无quota_period配置）")
                
                # 如果是最后一个模型，返回错误
                if i == len(models_to_try) - 1:
                    return {"success": False, "error": str(result), "data": None}
                else:
                    logger.debug(f"DEBUG: 继续尝试下一个模型...")
        
        return {"success": False, "error": "未知错误", "data": None}
    
    async def _try_platform_models_stream(self, platform_name: str, platform_models: List[ModelConfig], request_data: Dict[str, Any]) -> Dict[str, Any]:
        """尝试平台内的模型（流式），支持轮询机制"""
        # 过滤启用且当前周期内可用的模型
        available_models = []
        for model_config in platform_models:
            if model_config.enabled and await self.model_state_manager.is_model_available(model_config):
                available_models.append(model_config)
        
        if not available_models:
            logger.debug(f"DEBUG: 平台 {platform_name} 无可用模型（流式）")
            return {"success": False, "error": f"平台 {platform_name} 无可用模型", "data": None}
        
        # 总是使用轮询机制，无论是否配置了 quota_period
        start_index = await self.round_robin_manager.get_next_model_index(platform_name, len(available_models))
        models_to_try = []
        # 从轮询位置开始，循环遍历所有可用模型
        for i in range(len(available_models)):
            idx = (start_index + i) % len(available_models)
            models_to_try.append(available_models[idx])
        
        logger.debug(f"DEBUG: 平台 {platform_name} 有 {len(models_to_try)} 个模型待尝试（流式）: {[m.name for m in models_to_try]}")
        logger.debug(f"DEBUG: 平台 {platform_name} 轮询机制: 启用")
        
        # 按顺序尝试每个模型
        for i, model_config in enumerate(models_to_try):
            logger.debug(f"DEBUG: 平台 {platform_name} 尝试第 {i+1}/{len(models_to_try)} 个模型（流式）: {model_config.name}")
            
            success, result = await self.call_model_stream(model_config, request_data)
            
            if success:
                logger.info(f"模型 {model_config.name} 调用成功（流式）")
                logger.debug(f"DEBUG: 成功返回结果，类型: {type(result)}")
                return {"success": True, "data": result, "error": None}
            else:
                # 只要模型调用失败，就认为达到限额，标记为周期内用完
                if model_config.quota_period is not None:
                    # 配置了 quota_period，持久化禁用
                    logger.warning(f"模型 {model_config.name} 失败，标记为周期内用完...")
                    await self.model_state_manager.disable_model_for_period(model_config)
                else:
                    # 未配置 quota_period，临时禁用（仅在本次请求的剩余尝试中）
                    logger.debug(f"DEBUG: 模型 {model_config.name} 失败，临时禁用（无quota_period配置）")
                
                # 如果是最后一个模型，返回错误
                if i == len(models_to_try) - 1:
                    return {"success": False, "error": str(result), "data": None}
                else:
                    logger.debug(f"DEBUG: 继续尝试下一个模型...")
        
        return {"success": False, "error": "未知错误", "data": None}
    
    async def close(self):
        """关闭会话"""
        if self.session and not self.session.closed:
            await self.session.close()
            logger.debug("DEBUG: HTTP会话已关闭")
    
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
            
            # 对于普通响应，检查 message.content
            if "message" in first_choice:
                message = first_choice["message"]
                if isinstance(message, dict) and "content" in message:
                    content = message["content"]
                    # content 可以是字符串（可能为空字符串）或 None
                    # 只要字段存在就认为是有效的
                    return True
            
            # 对于流式响应的 chunk，检查 delta.content
            if "delta" in first_choice:
                delta = first_choice["delta"]
                if isinstance(delta, dict) and "content" in delta:
                    # delta.content 字段存在就认为有效
                    return True
            
            # 如果既没有 message 也没有 delta，或者没有 content 字段
            return False
            
        except Exception as e:
            logger.debug(f"DEBUG: 检查 content 字段时发生异常: {e}")
            return False

# 全局代理实例
proxy = OpenAIProxy()

# 定义 lifespan 事件处理器
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理器"""
    # 启动事件
    logger.info("OpenAI代理服务启动中...")
    yield
    # 关闭事件
    await proxy.close()
    logger.info("OpenAI代理服务已关闭")

# FastAPI应用 - 使用 lifespan 参数
app = FastAPI(title="OpenAI Proxy Service", version="1.0.0", lifespan=lifespan)

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    OpenAI兼容的聊天完成接口 - 支持完全参数透传
    """
    logger.debug("DEBUG: 接收到新的聊天完成请求")
    logger.debug(f"DEBUG: 请求客户端IP: {request.client.host if request.client else 'unknown'}")
    logger.debug(f"DEBUG: 请求头: {dict(request.headers)}")
    
    try:
        request_data = await request.json()
        logger.debug("DEBUG: 请求JSON解析成功")
    except Exception as e:
        logger.error(f"DEBUG: 请求JSON解析失败: {str(e)}")
        raise HTTPException(status_code=400, detail=f"无效的JSON请求体: {str(e)}")
    
    # 验证必要参数
    if not request_data.get("messages"):
        logger.error("DEBUG: 请求缺少messages参数")
        raise HTTPException(status_code=400, detail="messages参数是必需的")
    
    logger.debug(f"DEBUG: 请求是否为流式: {request_data.get('stream', False)}")
    
    if request_data.get("stream", False):
        # 流式响应处理
        async def stream_generator():
            logger.debug("DEBUG: 开始流式响应生成器")
            try:
                result = await proxy.chat_completion_stream(request_data)
                if hasattr(result, 'iter_any'):
                    # 处理 StreamResponseWrapper 对象
                    logger.debug("DEBUG: 流式响应 - 使用包装的流式响应")
                    async for chunk in result.iter_any():
                        chunk_size = len(chunk)
                        logger.debug(f"DEBUG: 流式响应 - 转发数据块，大小: {chunk_size}字节")
                        
                        # 打印响应内容（进行脱敏和截断处理）
                        try:
                            chunk_str = chunk.decode('utf-8', errors='replace')
                            # 截断过长的内容以避免日志过大
                            if len(chunk_str) > 500:
                                chunk_preview = chunk_str[:500] + "..."
                            else:
                                chunk_preview = chunk_str
                            
                            # 检查是否包含敏感信息（如API密钥等），如果有则脱敏
                            if "api_key" in chunk_preview.lower() or "secret" in chunk_preview.lower():
                                chunk_preview = "[SENSITIVE DATA REDACTED]"
                            
                            logger.debug(f"DEBUG: 流式响应内容预览: {chunk_preview}")
                        except Exception as decode_error:
                            logger.debug(f"DEBUG: 无法解码响应内容为UTF-8: {decode_error}")
                        
                        yield chunk
                elif isinstance(result, aiohttp.ClientResponse):
                    logger.debug("DEBUG: 流式响应 - 直接转发模型响应")
                    async for chunk in result.content.iter_any():
                        chunk_size = len(chunk)
                        logger.debug(f"DEBUG: 流式响应 - 转发数据块，大小: {chunk_size}字节")
                        
                        # 打印响应内容（进行脱敏和截断处理）
                        try:
                            chunk_str = chunk.decode('utf-8', errors='replace')
                            # 截断过长的内容以避免日志过大
                            if len(chunk_str) > 500:
                                chunk_preview = chunk_str[:500] + "..."
                            else:
                                chunk_preview = chunk_str
                            
                            # 检查是否包含敏感信息（如API密钥等），如果有则脱敏
                            if "api_key" in chunk_preview.lower() or "secret" in chunk_preview.lower():
                                chunk_preview = "[SENSITIVE DATA REDACTED]"
                            
                            logger.debug(f"DEBUG: 流式响应内容预览: {chunk_preview}")
                        except Exception as decode_error:
                            logger.debug(f"DEBUG: 无法解码响应内容为UTF-8: {decode_error}")
                        
                        yield chunk
                else:
                    # 如果返回的是普通响应但请求是流式的，转换为流式格式
                    logger.debug("DEBUG: 流式响应 - 转换普通响应为流式")
                    result_str = json.dumps(result)
                    # 打印转换后的响应内容
                    if len(result_str) > 500:
                        result_preview = result_str[:500] + "..."
                    else:
                        result_preview = result_str
                    logger.debug(f"DEBUG: 转换后的流式响应内容: {result_preview}")
                    yield result_str.encode() + b"\n"
            except Exception as e:
                logger.error(f"DEBUG: 流式响应生成器异常: {str(e)}", exc_info=True)
                error_response = {
                    "error": {
                        "message": str(e),
                        "type": "proxy_error",
                        "param": None,
                        "code": "proxy_error"
                    }
                }
                error_str = json.dumps(error_response)
                logger.debug(f"DEBUG: 流式错误响应内容: {error_str}")
                yield error_str.encode() + b"\n"
        
        logger.debug("DEBUG: 返回流式响应")
        return StreamingResponse(stream_generator(), media_type="text/plain")
    else:
        # 普通响应
        logger.debug("DEBUG: 处理普通（非流式）响应")
        result = await proxy.chat_completion_non_stream(request_data)
        logger.debug("DEBUG: 返回普通响应")
        return result

@app.get("/health")
async def health_check():
    """健康检查接口"""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.get("/models")
async def list_models():
    """列出所有可用的模型组"""
    return {"models": list(proxy.models.keys())}

if __name__ == "__main__":
    import uvicorn
    logger.info("启动OpenAI代理服务...")
    logger.info("请确保已配置 models.json 文件")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")