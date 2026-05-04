"""插件基类 - 所有插件都应该继承此类"""

import asyncio
import aiohttp
import logging
import os
import re
from typing import List, Dict, Any, Optional, Union
from abc import ABC, abstractmethod
import time


logger = logging.getLogger(__name__)


class BasePlugin(ABC):
    """插件基类 - 提供通用功能和接口规范"""
    
    def __init__(
        self, 
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        cache_ttl: int = 300,
        plugin_config: Optional[Dict[str, Any]] = None,
        **kwargs
    ):
        """
        初始化插件基类
        
        Args:
            api_key: API 密钥，如果为 None 则从环境变量获取
            base_url: API 基础URL
            cache_ttl: 缓存有效期（秒），默认300秒（5分钟）
            plugin_config: 插件配置字典，包含 args 等配置项
            **kwargs: 其他插件特定参数（保留用于向后兼容）
        """
        self.api_key = api_key
        self.base_url = base_url
        self.cache_ttl = cache_ttl
        self.models_cache: List[Any] = []
        self.last_cache_time = 0
        
        # 存储插件配置并预解析 args
        self.plugin_config = plugin_config or {}
        self._plugin_args = self.plugin_config.get('args', {})
        
        # 解析环境变量（如果存在）
        if self.api_key and isinstance(self.api_key, str):
            self.api_key = self.resolve_env_vars(self.api_key)
        if self.base_url and isinstance(self.base_url, str):
            self.base_url = self.resolve_env_vars(self.base_url)
            
    def get_plugin_arg(self, key: str, default=None) -> Any:
        """
        统一获取插件参数的方法
        
        从预解析的 _plugin_args 中获取指定键的值。
        
        Args:
            key: 参数键名
            default: 默认值，当键不存在时返回
            
        Returns:
            参数值或默认值
        """
        return self._plugin_args.get(key, default)
    
    def get_plugin_args(self) -> Dict[str, Any]:
        """
        获取所有插件参数的副本
        
        Returns:
            插件参数字典的副本
        """
        return self._plugin_args.copy()
    
    @staticmethod
    def resolve_env_vars(value: Union[str, Any]) -> Union[str, Any]:
        """
        解析并替换字符串中的环境变量占位符 ${VAR_NAME}
        
        Args:
            value: 包含环境变量占位符的值
            
        Returns:
            替换后的值，如果环境变量不存在则保留原占位符
        """
        if not isinstance(value, str):
            return value

        def replace_match(match):
            var_name = match.group(1)
            return os.getenv(var_name, match.group(0))

        # 使用正则表达式匹配 ${VAR_NAME} 格式的占位符
        pattern = r'\$\{([^}]+)\}'
        return re.sub(pattern, replace_match, value)
    
    def parse_plugin_config(self, plugin_config: Dict[str, Any]) -> Dict[str, Any]:
        """
        解析插件配置，处理环境变量和默认值
        
        Args:
            plugin_config: 原始插件配置字典
            
        Returns:
            解析后的配置字典
        """
        if not plugin_config:
            return {}
            
        parsed_config = {}
        for key, value in plugin_config.items():
            # 跳过内置字段
            if key in ['code', 'cache_timeout']:
                continue
                
            # 解析环境变量
            if isinstance(value, str):
                parsed_config[key] = self.resolve_env_vars(value)
            else:
                parsed_config[key] = value
                
        return parsed_config
    
    def is_cache_valid(self) -> bool:
        """
        检查缓存是否有效
        
        Returns:
            True 如果缓存有效，False 如果缓存已过期或不存在
        """
        if self.cache_ttl <= 0:  # 缓存被禁用
            return False
            
        current_time = time.time()
        return (
            len(self.models_cache) > 0 and 
            current_time - self.last_cache_time < self.cache_ttl
        )
    
    def update_cache(self, models: List[Any]) -> None:
        """
        更新模型缓存
        
        Args:
            models: 新的模型列表
        """
        self.models_cache = models
        self.last_cache_time = time.time()
        logger.debug(f"更新缓存，共 {len(models)} 个模型，TTL: {self.cache_ttl}s")
    
    def clear_cache(self) -> None:
        """清除缓存"""
        self.models_cache = []
        self.last_cache_time = 0
        logger.debug("缓存已清除")
    
    def _build_model_list_request(
        self, 
        plugin_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        构建模型列表请求配置
        
        根据插件配置构建完整的 HTTP 请求参数，支持 GET 和 POST 方法。
        
        Args:
            plugin_config: 完整的插件配置（包含 args）
            
        Returns:
            请求配置字典，包含 url, method, headers, params/body
            
        Raises:
            ValueError: 当无法确定模型列表 URL 时抛出
        """
        args = plugin_config.get('args', {})
        
        # URL：优先使用配置的 model_list_url，否则使用默认值
        url = args.get(
            'model_list_url', 
            f"{self.base_url}/models" if self.base_url else None
        )
        if not url:
            raise ValueError("无法确定模型列表 URL，请配置 model_list_url 或 base_url")
        
        # HTTP 方法
        method = args.get('model_list_method', 'GET').upper()
        
        # Headers
        headers = args.get('model_list_headers', {})
        
        # 根据方法类型选择参数
        if method == 'POST':
            body = args.get('request_body', {})
            return {
                'url': url,
                'method': method,
                'headers': headers,
                'json_data': body,
                'params': None
            }
        else:  # GET 或其他方法
            params = args.get('request_params', {})
            return {
                'url': url,
                'method': method,
                'headers': headers,
                'json_data': None,
                'params': params
            }
    
    async def _make_api_request(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        timeout: int = 30
    ) -> Dict[str, Any]:
        """
        发起API请求的通用方法（支持 GET 和 POST）
        
        Args:
            url: 请求URL
            method: HTTP方法（GET/POST/PUT等）
            headers: 请求头
            params: 查询参数（GET请求）
            json_data: JSON请求体（POST请求）
            timeout: 超时时间（秒）
            
        Returns:
            API响应数据
            
        Raises:
            Exception: API请求失败时抛出异常
        """
        try:
            async with aiohttp.ClientSession() as session:
                request_headers = headers or {}
                if self.api_key:
                    request_headers.setdefault("Authorization", f"Bearer {self.api_key}")
                
                request_kwargs = {
                    "headers": request_headers,
                    "timeout": aiohttp.ClientTimeout(total=timeout)
                }
                
                # 根据方法类型添加参数
                if method.upper() == 'GET' and params:
                    request_kwargs["params"] = params
                elif method.upper() in ['POST', 'PUT', 'PATCH'] and json_data:
                    request_kwargs["json"] = json_data
                
                async with getattr(session, method.lower())(url, **request_kwargs) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        error_text = await response.text()
                        raise Exception(f"HTTP {response.status}: {error_text}")
                        
        except asyncio.TimeoutError:
            raise Exception(f"请求超时 ({timeout}秒)")
        except Exception as e:
            raise Exception(f"API请求失败: {str(e)}")
    
    def _validate_request_config(
        self, 
        plugin_config: Dict[str, Any]
    ) -> List[str]:
        """
        验证请求配置的通用规则
        
        验证项：
        1. URL 是否可确定
        2. POST 请求是否有 body
        3. GET 请求是否有 params（可选）
        
        Args:
            plugin_config: 插件配置
            
        Returns:
            警告消息列表（空列表表示无警告）
        """
        warnings = []
        args = plugin_config.get('args', {})
        method = args.get('model_list_method', 'GET').upper()
        
        # 1. 检查 URL
        if not args.get('model_list_url') and not self.base_url:
            warnings.append(
                "未配置 model_list_url 且 base_url 为空，将无法发起请求"
            )
        
        # 2. 检查 POST 请求是否有 body
        if method == 'POST' and not args.get('request_body'):
            warnings.append(
                "POST 请求未配置 request_body，可能导致 API 返回错误"
            )
        
        # 3. 检查 GET 请求是否有 params（可选，有些 API 不需要参数）
        if method == 'GET' and not args.get('request_params'):
            logger.debug(
                f"GET 请求未配置 request_params，将使用默认端点: "
                f"{self.base_url}/models"
            )
        
        return warnings
    
    @abstractmethod
    async def get_models(self, plugin_config: Dict[str, Any] = None) -> List[Any]:
        """
        获取模型列表 - 子类必须实现
        
        Args:
            plugin_config: 插件配置字典
            
        Returns:
            模型对象列表
        """
        pass
    
    @abstractmethod
    async def health_check(self, plugin_config: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        健康检查 - 子类必须实现
        
        Args:
            plugin_config: 插件配置字典
            
        Returns:
            健康检查结果字典，包含status、response_time_ms等字段
        """
        pass