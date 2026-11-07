"""监控指标模块 - 实现 Prometheus 监控指标收集和暴露"""

from typing import Dict, List, Optional, Any
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
import os
import time
import threading

# 是否给请求级指标附加 model 标签。
# 爬虫模式下每个平台会自动灌入几十个模型，(platform × model × status × error_type)
# 的时间序列数量会失控；默认只按平台聚合，模型级细节留在 SQLite 审计表。
_LABEL_MODEL = os.getenv("METRICS_LABEL_MODEL", "false").strip().lower() in {"1", "true", "yes", "on"}

# tokens/s 的观测区间：推理模型慢到个位数、小模型快到大几十
_THROUGHPUT_BUCKETS = [1.0, 5.0, 10.0, 20.0, 40.0, 80.0, 150.0]
# 首块耗时（TTFB）的观测区间，与首块预算配置的量级一致
_TTFT_BUCKETS = [0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0]


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
        self.label_model = _LABEL_MODEL
        self._register_metrics()
    
    def _model_labels(self, extra: List[str]) -> List[str]:
        """按开关决定是否在标签集里加入 model"""
        return ["platform", "model"] + extra if self.label_model else ["platform"] + extra

    def _register_metrics(self):
        """注册所有监控指标"""
        
        # 请求计数器
        self.requests_total = Counter(
            'proxy_requests_total',
            'Total number of proxy requests',
            self._model_labels(['status', 'error_type'])
        )
        
        # 请求延迟直方图
        self.request_duration = Histogram(
            'proxy_request_duration_seconds',
            'Request duration in seconds',
            self._model_labels([]),
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

        # 流式性能分布：首块耗时、总耗时、输出速度
        self.stream_ttft = Histogram(
            'proxy_stream_ttft_seconds',
            'Time to first chunk in seconds',
            self._model_labels([]),
            buckets=_TTFT_BUCKETS
        )
        self.stream_duration = Histogram(
            'proxy_stream_duration_seconds',
            'Upstream call duration in seconds',
            self._model_labels([]),
            buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0]
        )
        self.stream_throughput = Histogram(
            'proxy_stream_throughput_tokens_per_second',
            'Output tokens per second (end to end)',
            self._model_labels([]),
            buckets=_THROUGHPUT_BUCKETS
        )
        # token 用量：按平台与类型聚合，用于统计用量
        self.stream_tokens_total = Counter(
            'proxy_stream_tokens_total',
            'Token usage reported by upstream',
            ['platform', 'token_type']
        )
        # 上游没有返回 usage 的调用次数：这类调用的用量统计不完整
        self.usage_missing_total = Counter(
            'proxy_usage_missing_total',
            'Calls whose upstream did not report usage',
            self._model_labels([])
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
        
        self.requests_total.labels(**self._labels(platform, model, status=status, error_type=error_type)).inc()
        
        self.request_duration.labels(**self._labels(platform, model)).observe(duration)
        
        if status == "failure" and error_type != "none":
            self.errors_total.labels(
                platform=platform,
                error_type=error_type
            ).inc()

    def observe_stream_stats(self, attempt: Any) -> None:
        """
        记录一次调用的性能分布（由审计采集器在记录落库时调用）

        Args:
            attempt: AuditAttempt 实例，读取 duration_ms / ttft_ms / throughput / token 用量
        """
        platform = getattr(attempt, "platform", "") or "unknown"
        model = getattr(attempt, "model_name", "") or "unknown"
        labels = self._labels(platform, model)

        ttft_ms = getattr(attempt, "ttft_ms", None)
        if isinstance(ttft_ms, (int, float)) and ttft_ms > 0:
            self.stream_ttft.labels(**labels).observe(ttft_ms / 1000.0)

        duration_ms = getattr(attempt, "duration_ms", None)
        if isinstance(duration_ms, (int, float)) and duration_ms > 0:
            self.stream_duration.labels(**labels).observe(duration_ms / 1000.0)

        throughput = getattr(attempt, "throughput", None)
        if isinstance(throughput, (int, float)) and throughput > 0:
            self.stream_throughput.labels(**labels).observe(throughput)

        if getattr(attempt, "has_usage", False):
            for token_type, attribute in (("input", "input_tokens"), ("output", "output_tokens"), ("cached", "cached_tokens")):
                tokens = getattr(attempt, attribute, None)
                if isinstance(tokens, (int, float)) and tokens > 0:
                    self.stream_tokens_total.labels(platform=platform, token_type=token_type).inc(int(tokens))
        else:
            # 上游没上报 usage 的次数本身就是一个需要盯的指标
            self.usage_missing_total.labels(**labels).inc()

    def _labels(self, platform: str, model: str, **extra: str) -> Dict[str, str]:
        """按 METRICS_LABEL_MODEL 开关组装标签，避免未注册的 label 组合"""
        labels: Dict[str, str] = {"platform": platform}
        if self.label_model:
            labels["model"] = model
        labels.update(extra)
        return labels
    
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