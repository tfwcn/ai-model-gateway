"""错误分类器模块 - 实现错误类型识别和分类处理"""

from enum import Enum
from typing import Any, Dict, Optional, Tuple
import json
import logging

logger = logging.getLogger(__name__)


class ErrorType(Enum):
    """错误类型枚举"""
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"  # 超出配额
    AUTH_ERROR = "AUTH_ERROR"  # 认证错误
    NETWORK_ERROR = "NETWORK_ERROR"  # 网络错误
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"  # 模型不可用
    SERVER_ERROR = "SERVER_ERROR"  # 服务器错误
    TIMEOUT_ERROR = "TIMEOUT_ERROR"  # 超时错误
    INVALID_RESPONSE = "INVALID_RESPONSE"  # 无效响应
    UNKNOWN_ERROR = "UNKNOWN_ERROR"  # 未知错误


class ErrorClassifier:
    """错误分类器 - 根据 HTTP 状态码和响应内容识别错误类型"""
    
    # HTTP 状态码到错误类型的映射
    STATUS_CODE_MAP = {
        400: ErrorType.MODEL_UNAVAILABLE,  # 请求错误，可能是模型不可用
        401: ErrorType.AUTH_ERROR,  # 未授权
        403: ErrorType.AUTH_ERROR,  # 禁止访问
        404: ErrorType.MODEL_UNAVAILABLE,  # 未找到，可能是模型不可用
        429: ErrorType.QUOTA_EXCEEDED,  # 超出配额/速率限制
        500: ErrorType.SERVER_ERROR,  # 服务器内部错误
        502: ErrorType.SERVER_ERROR,  # 网关错误
        503: ErrorType.SERVER_ERROR,  # 服务不可用
        504: ErrorType.TIMEOUT_ERROR,  # 网关超时
    }
    
    @classmethod
    def classify_by_status_code(cls, status_code: int) -> ErrorType:
        """
        根据 HTTP 状态码分类错误
        
        Args:
            status_code: HTTP 状态码
            
        Returns:
            ErrorType: 错误类型
        """
        return cls.STATUS_CODE_MAP.get(status_code, ErrorType.UNKNOWN_ERROR)
    
    @classmethod
    def classify_by_exception(cls, exception: Exception) -> ErrorType:
        """
        根据异常类型分类错误
        
        Args:
            exception: 异常对象
            
        Returns:
            ErrorType: 错误类型
        """
        exception_type = type(exception).__name__
        exception_str = str(exception).lower()
        
        # 超时相关异常
        if any(keyword in exception_type.lower() for keyword in ['timeout', 'timedout']):
            return ErrorType.TIMEOUT_ERROR
        
        # 网络连接相关异常
        if any(keyword in exception_type.lower() for keyword in ['connection', 'network', 'socket']):
            return ErrorType.NETWORK_ERROR
        
        if any(keyword in exception_str for keyword in ['timeout', 'timed out', 'connection refused', 'connection reset']):
            return ErrorType.TIMEOUT_ERROR if 'timeout' in exception_str else ErrorType.NETWORK_ERROR
        
        return ErrorType.UNKNOWN_ERROR
    
    @classmethod
    def classify_by_response(cls, status_code: int, response_body: Optional[str] = None) -> ErrorType:
        """
        根据响应状态码和内容分类错误
        
        Args:
            status_code: HTTP 状态码
            response_body: 响应体内容（可选）
            
        Returns:
            ErrorType: 错误类型
        """
        # 首先根据状态码分类
        error_type = cls.classify_by_status_code(status_code)
        
        # 如果有响应体，尝试进一步分析
        if response_body:
            try:
                response_data = json.loads(response_body)
                error_type = cls._analyze_json_response(response_data, error_type)
            except (json.JSONDecodeError, ValueError):
                # 响应体不是有效 JSON，检查文本内容
                error_type = cls._analyze_text_response(response_body, error_type)
        
        return error_type
    
    @classmethod
    def _analyze_json_response(cls, response_data: Dict[str, Any], default_type: ErrorType) -> ErrorType:
        """
        分析 JSON 响应内容
        
        Args:
            response_data: JSON 响应数据
            default_type: 默认错误类型
            
        Returns:
            ErrorType: 错误类型
        """
        # 检查是否包含错误信息
        error_info = response_data.get("error", {})
        if isinstance(error_info, dict):
            error_message = error_info.get("message", "").lower()
            error_type_str = error_info.get("type", "").lower()
            
            # 检查配额相关错误
            if any(keyword in error_message for keyword in ['quota', 'rate limit', 'too many requests', '超出配额', '频率限制']):
                return ErrorType.QUOTA_EXCEEDED
            
            # 检查认证相关错误
            if any(keyword in error_message for keyword in ['authentication', 'unauthorized', 'invalid api key', '认证失败', '密钥无效']):
                return ErrorType.AUTH_ERROR
            
            # 检查模型相关错误
            if any(keyword in error_message for keyword in ['model', 'not found', 'does not exist', ' unavailable', '模型不可用']):
                return ErrorType.MODEL_UNAVAILABLE
            
            # 检查无效响应
            if not error_info or not error_message:
                return ErrorType.INVALID_RESPONSE
        
        # 检查响应是否缺少有效内容
        if not response_data.get("choices") and not response_data.get("data"):
            if not response_data.get("error"):
                return ErrorType.INVALID_RESPONSE
        
        return default_type
    
    @classmethod
    def _analyze_text_response(cls, response_text: str, default_type: ErrorType) -> ErrorType:
        """
        分析文本响应内容
        
        Args:
            response_text: 文本响应内容
            default_type: 默认错误类型
            
        Returns:
            ErrorType: 错误类型
        """
        text_lower = response_text.lower()
        
        # 检查配额相关关键词
        if any(keyword in text_lower for keyword in ['quota', 'rate limit', 'too many requests']):
            return ErrorType.QUOTA_EXCEEDED
        
        # 检查认证相关关键词
        if any(keyword in text_lower for keyword in ['unauthorized', 'authentication failed', 'invalid api key']):
            return ErrorType.AUTH_ERROR
        
        # 检查模型相关关键词
        if any(keyword in text_lower for keyword in ['model not found', 'model unavailable']):
            return ErrorType.MODEL_UNAVAILABLE
        
        return default_type
    
    @classmethod
    def get_handling_strategy(cls, error_type: ErrorType) -> Dict[str, Any]:
        """
        获取错误处理策略
        
        Args:
            error_type: 错误类型
            
        Returns:
            Dict[str, Any]: 处理策略配置
        """
        strategies = {
            ErrorType.QUOTA_EXCEEDED: {
                "action": "disable_model",
                "duration": "quota_period",
                "retry": False,
                "log_level": "warning"
            },
            ErrorType.AUTH_ERROR: {
                "action": "disable_platform",
                "duration": "permanent",
                "retry": False,
                "log_level": "error",
                "alert": True
            },
            ErrorType.NETWORK_ERROR: {
                "action": "retry_with_backoff",
                "max_retries": 3,
                "backoff_factor": 2,
                "retry": True,
                "log_level": "warning"
            },
            ErrorType.MODEL_UNAVAILABLE: {
                "action": "try_next_model",
                "retry": False,
                "log_level": "warning"
            },
            ErrorType.SERVER_ERROR: {
                "action": "retry_with_backoff",
                "max_retries": 3,
                "backoff_factor": 2,
                "retry": True,
                "log_level": "warning"
            },
            ErrorType.TIMEOUT_ERROR: {
                "action": "immediate_failover",
                "disable_duration": 60,  # 临时禁用 1 分钟
                "retry": False,
                "log_level": "warning"
            },
            ErrorType.INVALID_RESPONSE: {
                "action": "try_next_model",
                "retry": False,
                "log_level": "warning"
            },
            ErrorType.UNKNOWN_ERROR: {
                "action": "try_next_model",
                "retry": False,
                "log_level": "warning"
            }
        }
        
        return strategies.get(error_type, strategies[ErrorType.UNKNOWN_ERROR])