"""
错误分类模块 - 提供细粒度的错误类型识别和处理策略
"""
import logging
from enum import Enum
from typing import Optional, Dict, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class ErrorCategory(Enum):
    """错误分类枚举"""
    # 网络相关错误
    NETWORK_TIMEOUT = "network_timeout"  # 网络超时
    NETWORK_CONNECTION_ERROR = "network_connection_error"  # 网络连接错误
    NETWORK_DNS_ERROR = "network_dns_error"  # DNS解析错误
    
    # HTTP状态码错误
    HTTP_400_BAD_REQUEST = "http_400_bad_request"  # 请求格式错误
    HTTP_401_UNAUTHORIZED = "http_401_unauthorized"  # 认证失败
    HTTP_403_FORBIDDEN = "http_403_forbidden"  # 权限不足
    HTTP_404_NOT_FOUND = "http_404_not_found"  # 资源不存在
    HTTP_429_RATE_LIMIT = "http_429_rate_limit"  # 速率限制
    HTTP_500_INTERNAL_ERROR = "http_500_internal_error"  # 服务器内部错误
    HTTP_502_BAD_GATEWAY = "http_502_bad_gateway"  # 网关错误
    HTTP_503_SERVICE_UNAVAILABLE = "http_503_service_unavailable"  # 服务不可用
    HTTP_504_GATEWAY_TIMEOUT = "http_504_gateway_timeout"  # 网关超时
    
    # 业务逻辑错误
    INVALID_RESPONSE_FORMAT = "invalid_response_format"  # 响应格式无效
    MISSING_CONTENT = "missing_content"  # 缺少内容字段
    INVALID_MODEL = "invalid_model"  # 模型不存在或无效
    
    # 其他错误
    UNKNOWN_ERROR = "unknown_error"  # 未知错误


@dataclass
class ClassifiedError:
    """分类后的错误信息"""
    category: ErrorCategory  # 错误分类
    message: str  # 错误消息
    is_retryable: bool  # 是否可重试
    should_disable_model: bool  # 是否应该禁用模型
    retry_delay_seconds: float = 0.0  # 重试延迟（秒）
    metadata: Dict[str, Any] = None  # 额外元数据
    
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class ErrorClassifier:
    """错误分类器 - 根据错误信息判断错误类型和处理策略"""
    
    # HTTP状态码到错误分类的映射
    HTTP_STATUS_MAP = {
        400: ErrorCategory.HTTP_400_BAD_REQUEST,
        401: ErrorCategory.HTTP_401_UNAUTHORIZED,
        403: ErrorCategory.HTTP_403_FORBIDDEN,
        404: ErrorCategory.HTTP_404_NOT_FOUND,
        429: ErrorCategory.HTTP_429_RATE_LIMIT,
        500: ErrorCategory.HTTP_500_INTERNAL_ERROR,
        502: ErrorCategory.HTTP_502_BAD_GATEWAY,
        503: ErrorCategory.HTTP_503_SERVICE_UNAVAILABLE,
        504: ErrorCategory.HTTP_504_GATEWAY_TIMEOUT,
    }
    
    # 错误分类的处理策略
    ERROR_STRATEGIES = {
        # 网络错误 - 可重试，不禁用模型
        ErrorCategory.NETWORK_TIMEOUT: {
            "is_retryable": True,
            "should_disable_model": False,
            "retry_delay_seconds": 1.0,
        },
        ErrorCategory.NETWORK_CONNECTION_ERROR: {
            "is_retryable": True,
            "should_disable_model": False,
            "retry_delay_seconds": 2.0,
        },
        ErrorCategory.NETWORK_DNS_ERROR: {
            "is_retryable": True,
            "should_disable_model": False,
            "retry_delay_seconds": 5.0,
        },
        
        # HTTP 4xx 错误 - 通常不可重试（客户端错误）
        ErrorCategory.HTTP_400_BAD_REQUEST: {
            "is_retryable": False,
            "should_disable_model": False,
            "retry_delay_seconds": 0.0,
        },
        ErrorCategory.HTTP_401_UNAUTHORIZED: {
            "is_retryable": False,
            "should_disable_model": True,  # API密钥错误，应禁用
            "retry_delay_seconds": 0.0,
        },
        ErrorCategory.HTTP_403_FORBIDDEN: {
            "is_retryable": False,
            "should_disable_model": True,  # 权限问题，应禁用
            "retry_delay_seconds": 0.0,
        },
        ErrorCategory.HTTP_404_NOT_FOUND: {
            "is_retryable": False,
            "should_disable_model": True,  # 模型不存在，应禁用
            "retry_delay_seconds": 0.0,
        },
        
        # HTTP 429 速率限制 - 可重试，短暂禁用
        ErrorCategory.HTTP_429_RATE_LIMIT: {
            "is_retryable": True,
            "should_disable_model": True,
            "retry_delay_seconds": 10.0,  # 等待10秒后重试
        },
        
        # HTTP 5xx 错误 - 服务器错误，可重试
        ErrorCategory.HTTP_500_INTERNAL_ERROR: {
            "is_retryable": True,
            "should_disable_model": False,
            "retry_delay_seconds": 2.0,
        },
        ErrorCategory.HTTP_502_BAD_GATEWAY: {
            "is_retryable": True,
            "should_disable_model": False,
            "retry_delay_seconds": 3.0,
        },
        ErrorCategory.HTTP_503_SERVICE_UNAVAILABLE: {
            "is_retryable": True,
            "should_disable_model": False,
            "retry_delay_seconds": 5.0,
        },
        ErrorCategory.HTTP_504_GATEWAY_TIMEOUT: {
            "is_retryable": True,
            "should_disable_model": False,
            "retry_delay_seconds": 3.0,
        },
        
        # 业务逻辑错误
        ErrorCategory.INVALID_RESPONSE_FORMAT: {
            "is_retryable": False,
            "should_disable_model": True,  # 响应格式错误，可能是模型问题
            "retry_delay_seconds": 0.0,
        },
        ErrorCategory.MISSING_CONTENT: {
            "is_retryable": False,
            "should_disable_model": True,  # 缺少内容，可能是模型问题
            "retry_delay_seconds": 0.0,
        },
        ErrorCategory.INVALID_MODEL: {
            "is_retryable": False,
            "should_disable_model": True,
            "retry_delay_seconds": 0.0,
        },
        
        # 未知错误 - 保守处理
        ErrorCategory.UNKNOWN_ERROR: {
            "is_retryable": True,
            "should_disable_model": False,
            "retry_delay_seconds": 1.0,
        },
    }
    
    @classmethod
    def classify_http_error(cls, status_code: int, error_text: str, model_name: str) -> ClassifiedError:
        """
        分类HTTP错误
        
        Args:
            status_code: HTTP状态码
            error_text: 错误响应文本
            model_name: 模型名称
            
        Returns:
            ClassifiedError: 分类后的错误
        """
        category = cls.HTTP_STATUS_MAP.get(status_code, ErrorCategory.UNKNOWN_ERROR)
        strategy = cls.ERROR_STRATEGIES.get(category, cls.ERROR_STRATEGIES[ErrorCategory.UNKNOWN_ERROR])
        
        message = f"HTTP {status_code}: {error_text[:200]}"  # 限制错误消息长度
        
        return ClassifiedError(
            category=category,
            message=message,
            is_retryable=strategy["is_retryable"],
            should_disable_model=strategy["should_disable_model"],
            retry_delay_seconds=strategy["retry_delay_seconds"],
            metadata={
                "status_code": status_code,
                "model_name": model_name,
            }
        )
    
    @classmethod
    def classify_timeout_error(cls, model_name: str, elapsed_time: float, timeout: int) -> ClassifiedError:
        """
        分类超时错误
        
        Args:
            model_name: 模型名称
            elapsed_time: 已耗时（秒）
            timeout: 超时阈值（秒）
            
        Returns:
            ClassifiedError: 分类后的错误
        """
        strategy = cls.ERROR_STRATEGIES[ErrorCategory.NETWORK_TIMEOUT]
        message = f"模型 {model_name} 请求超时 (耗时: {elapsed_time:.2f}秒, 超时阈值: {timeout}秒)"
        
        return ClassifiedError(
            category=ErrorCategory.NETWORK_TIMEOUT,
            message=message,
            is_retryable=strategy["is_retryable"],
            should_disable_model=strategy["should_disable_model"],
            retry_delay_seconds=strategy["retry_delay_seconds"],
            metadata={
                "model_name": model_name,
                "elapsed_time": elapsed_time,
                "timeout": timeout,
            }
        )
    
    @classmethod
    def classify_connection_error(cls, exception: Exception, model_name: str) -> ClassifiedError:
        """
        分类连接错误
        
        Args:
            exception: 异常对象
            model_name: 模型名称
            
        Returns:
            ClassifiedError: 分类后的错误
        """
        error_str = str(exception).lower()
        
        # 判断具体的连接错误类型
        if "dns" in error_str or "name resolution" in error_str:
            category = ErrorCategory.NETWORK_DNS_ERROR
        elif "connection refused" in error_str or "connect failed" in error_str:
            category = ErrorCategory.NETWORK_CONNECTION_ERROR
        else:
            category = ErrorCategory.NETWORK_CONNECTION_ERROR
        
        strategy = cls.ERROR_STRATEGIES[category]
        message = f"模型 {model_name} 连接错误: {str(exception)[:200]}"
        
        return ClassifiedError(
            category=category,
            message=message,
            is_retryable=strategy["is_retryable"],
            should_disable_model=strategy["should_disable_model"],
            retry_delay_seconds=strategy["retry_delay_seconds"],
            metadata={
                "model_name": model_name,
                "exception_type": type(exception).__name__,
            }
        )
    
    @classmethod
    def classify_invalid_response(cls, model_name: str, reason: str) -> ClassifiedError:
        """
        分类无效响应错误
        
        Args:
            model_name: 模型名称
            reason: 错误原因
            
        Returns:
            ClassifiedError: 分类后的错误
        """
        if "content" in reason.lower():
            category = ErrorCategory.MISSING_CONTENT
        else:
            category = ErrorCategory.INVALID_RESPONSE_FORMAT
        
        strategy = cls.ERROR_STRATEGIES[category]
        
        return ClassifiedError(
            category=category,
            message=f"模型 {model_name} 返回无效响应: {reason}",
            is_retryable=strategy["is_retryable"],
            should_disable_model=strategy["should_disable_model"],
            retry_delay_seconds=strategy["retry_delay_seconds"],
            metadata={
                "model_name": model_name,
                "reason": reason,
            }
        )
    
    @classmethod
    def classify_unknown_error(cls, exception: Exception, model_name: str, elapsed_time: float) -> ClassifiedError:
        """
        分类未知错误
        
        Args:
            exception: 异常对象
            model_name: 模型名称
            elapsed_time: 已耗时（秒）
            
        Returns:
            ClassifiedError: 分类后的错误
        """
        strategy = cls.ERROR_STRATEGIES[ErrorCategory.UNKNOWN_ERROR]
        message = f"模型 {model_name} 调用异常: {str(exception)[:200]} (耗时: {elapsed_time:.2f}秒)"
        
        return ClassifiedError(
            category=ErrorCategory.UNKNOWN_ERROR,
            message=message,
            is_retryable=strategy["is_retryable"],
            should_disable_model=strategy["should_disable_model"],
            retry_delay_seconds=strategy["retry_delay_seconds"],
            metadata={
                "model_name": model_name,
                "exception_type": type(exception).__name__,
                "elapsed_time": elapsed_time,
            }
        )
    
    @classmethod
    def get_error_summary(cls, classified_error: ClassifiedError) -> str:
        """
        获取错误的简要描述
        
        Args:
            classified_error: 分类后的错误
            
        Returns:
            str: 错误摘要
        """
        category_descriptions = {
            ErrorCategory.NETWORK_TIMEOUT: "网络超时",
            ErrorCategory.NETWORK_CONNECTION_ERROR: "网络连接错误",
            ErrorCategory.NETWORK_DNS_ERROR: "DNS解析错误",
            ErrorCategory.HTTP_400_BAD_REQUEST: "请求格式错误",
            ErrorCategory.HTTP_401_UNAUTHORIZED: "认证失败",
            ErrorCategory.HTTP_403_FORBIDDEN: "权限不足",
            ErrorCategory.HTTP_404_NOT_FOUND: "资源不存在",
            ErrorCategory.HTTP_429_RATE_LIMIT: "速率限制",
            ErrorCategory.HTTP_500_INTERNAL_ERROR: "服务器内部错误",
            ErrorCategory.HTTP_502_BAD_GATEWAY: "网关错误",
            ErrorCategory.HTTP_503_SERVICE_UNAVAILABLE: "服务不可用",
            ErrorCategory.HTTP_504_GATEWAY_TIMEOUT: "网关超时",
            ErrorCategory.INVALID_RESPONSE_FORMAT: "响应格式无效",
            ErrorCategory.MISSING_CONTENT: "缺少内容字段",
            ErrorCategory.INVALID_MODEL: "模型无效",
            ErrorCategory.UNKNOWN_ERROR: "未知错误",
        }
        
        description = category_descriptions.get(classified_error.category, "未知错误")
        retry_info = "可重试" if classified_error.is_retryable else "不可重试"
        disable_info = "将禁用模型" if classified_error.should_disable_model else "不禁用模型"
        
        return f"[{description}] {retry_info}, {disable_info}"
