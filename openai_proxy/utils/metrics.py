"""监控指标模块 - 实现 Prometheus 监控指标收集和暴露"""

from typing import Dict, Optional, Any
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
import time
import threading


class MetricsCollector:
    """Prometheus 监控指标收集器"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        """单例模式"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._initialized = True
        self._register_metrics()
    
    def _register_metrics(self):
        """注册所有监控指标"""
        
        # 请求计数器
        self.requests_total = Counter(
            'proxy_requests_total',
            'Total number of proxy requests',
            ['platform', 'model', 'status', 'error_type']
        )
        
        # 请求延迟直方图
        self.request_duration = Histogram(
            'proxy_request_duration_seconds',
            'Request duration in seconds',
            ['platform', 'model'],
            buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0]
        )
        
        # 错误计数器
        self.errors_total = Counter(
            'proxy_errors_total',
            'Total number of proxy errors',
            ['platform', 'error_type']
        )
        
        # 平台可用性指标
        self.platform_availability = Gauge(
            'platform_availability',
            'Platform availability status (1=available, 0=unavailable)',
            ['platform', 'model']
        )
        
        # 故障转移计数器
        self.failover_total = Counter(
            'proxy_failover_total',
            'Total number of failovers',
            ['from_platform', 'to_platform']
        )
        
        # 缓存命中计数器
        self.cache_hits_total = Counter(
            'cache_hits_total',
            'Total number of cache hits'
        )
        
        # 缓存未命中计数器
        self.cache_misses_total = Counter(
            'cache_misses_total',
            'Total number of cache misses'
        )
        
        # 活跃连接数
        self.active_connections = Gauge(
            'active_connections',
            'Number of active connections'
        )
        
        # 待处理请求数
        self.pending_requests = Gauge(
            'pending_requests',
            'Number of pending requests'
        )
    
    def record_request(
        self,
        platform: str,
        model: str,
        status: str,
        duration: float,
        error_type: Optional[str] = None
    ):
        """
        记录请求指标
        
        Args:
            platform: 平台名称
            model: 模型名称
            status: 请求状态（success/failure）
            duration: 请求耗时（秒）
            error_type: 错误类型（可选）
        """
        error_type = error_type or "none"
        
        self.requests_total.labels(
            platform=platform,
            model=model,
            status=status,
            error_type=error_type
        ).inc()
        
        self.request_duration.labels(
            platform=platform,
            model=model
        ).observe(duration)
        
        if status == "failure" and error_type != "none":
            self.errors_total.labels(
                platform=platform,
                error_type=error_type
            ).inc()
    
    def record_failover(self, from_platform: str, to_platform: str):
        """
        记录故障转移事件
        
        Args:
            from_platform: 源平台
            to_platform: 目标平台
        """
        self.failover_total.labels(
            from_platform=from_platform,
            to_platform=to_platform
        ).inc()
    
    def set_platform_availability(self, platform: str, model: str, available: bool):
        """
        设置平台可用性状态
        
        Args:
            platform: 平台名称
            model: 模型名称
            available: 是否可用
        """
        self.platform_availability.labels(
            platform=platform,
            model=model
        ).set(1 if available else 0)
    
    def record_cache_hit(self):
        """记录缓存命中"""
        self.cache_hits_total.inc()
    
    def record_cache_miss(self):
        """记录缓存未命中"""
        self.cache_misses_total.inc()
    
    def set_active_connections(self, count: int):
        """
        设置活跃连接数
        
        Args:
            count: 连接数
        """
        self.active_connections.set(count)
    
    def set_pending_requests(self, count: int):
        """
        设置待处理请求数
        
        Args:
            count: 请求数
        """
        self.pending_requests.set(count)
    
    def get_metrics(self) -> str:
        """
        获取 Prometheus 格式的指标数据
        
        Returns:
            str: Prometheus 格式的指标字符串
        """
        return generate_latest()
    
    def get_metrics_content_type(self) -> str:
        """
        获取指标内容的 MIME 类型
        
        Returns:
            str: Content-Type 值
        """
        return CONTENT_TYPE_LATEST


# 全局指标收集器实例
metrics_collector = MetricsCollector()


class MetricsMiddleware:
    """FastAPI 监控中间件"""
    
    def __init__(self):
        self.metrics = MetricsCollector()
        self._request_start_times: Dict[str, float] = {}
    
    async def before_request(self, request_id: str):
        """
        请求前处理
        
        Args:
            request_id: 请求唯一标识
        """
        self._request_start_times[request_id] = time.time()
        self.metrics.set_pending_requests(len(self._request_start_times))
    
    async def after_request(
        self,
        request_id: str,
        platform: str,
        model: str,
        status: str,
        error_type: Optional[str] = None
    ):
        """
        请求后处理
        
        Args:
            request_id: 请求唯一标识
            platform: 平台名称
            model: 模型名称
            status: 请求状态
            error_type: 错误类型
        """
        start_time = self._request_start_times.pop(request_id, time.time())
        duration = time.time() - start_time
        
        self.metrics.record_request(
            platform=platform,
            model=model,
            status=status,
            duration=duration,
            error_type=error_type
        )
        
        self.metrics.set_pending_requests(len(self._request_start_times))