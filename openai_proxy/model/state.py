import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from openai_proxy.models import ModelConfig

logger = logging.getLogger(__name__)


@dataclass
class DisableRecord:
    """
    「周期内用完」的记录

    除了到期时间，还保留最近一次失败原因：所有这些模型都被禁用后，
    上层可以把真实根因（例如上游 401）一并回传给下游，而不是只回一句
    「无可用模型」。
    """

    expiry: datetime
    category: str = ""
    message: str = ""
    http_status: Optional[int] = None
    recorded_at: datetime = field(default_factory=datetime.now)

    def expired(self, now: Optional[datetime] = None) -> bool:
        """是否已过期（过期后自动恢复可用）"""
        return (now or datetime.now()) >= self.expiry

    def remaining_seconds(self, now: Optional[datetime] = None) -> float:
        """距离恢复还有多少秒（已过期返回 0）"""
        remaining = (self.expiry - (now or datetime.now())).total_seconds()
        return max(0.0, remaining)


class ModelStateManager:
    """模型状态管理器 - 跟踪模型在当前周期内的可用性"""

    def __init__(self):
        # 存储模型失效记录: {model_name: DisableRecord}
        self.disabled_models: Dict[str, DisableRecord] = {}
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

    @staticmethod
    def _build_record(expiry: datetime, failure: Any = None) -> DisableRecord:
        """从分类错误（鸭子类型）里提取对外可展示的失败原因"""
        if failure is None:
            return DisableRecord(expiry=expiry)

        category = getattr(failure, "category", "") or ""
        # ErrorCategory 枚举取 value，字符串直接用
        category = getattr(category, "value", category)
        message = getattr(failure, "public_message", None) or getattr(failure, "message", "") or ""
        http_status = getattr(failure, "http_status", None)
        return DisableRecord(
            expiry=expiry,
            category=str(category),
            message=str(message),
            http_status=int(http_status) if http_status is not None else None,
        )

    async def disable_model_for_period(self, model_config: ModelConfig, failure: Any = None):
        """标记模型在当前周期内已用完（可附带失败原因）

        Args:
            model_config: 被禁用的模型
            failure: 触发禁用的分类错误（ClassifiedError），用于保留根因
        """
        if not model_config.quota_period:
            return  # 无周期限制，不标记

        async with self.lock:
            expiry_time = self._get_period_expiry(model_config.quota_period)
            self.disabled_models[model_config.name] = self._build_record(expiry_time, failure)

    def _purge_expired(self, now: datetime) -> None:
        """清理过期的禁用记录（调用方需持有锁）"""
        expired_models = [
            model_name
            for model_name, record in self.disabled_models.items()
            if record.expired(now)
        ]
        for model_name in expired_models:
            del self.disabled_models[model_name]

    async def is_model_available(self, model_config: ModelConfig) -> bool:
        """检查模型是否可用（未被标记为周期内用完）"""
        if not model_config.quota_period:
            return True  # 无周期限制，始终可用

        async with self.lock:
            now = datetime.now()
            self._purge_expired(now)
            return model_config.name not in self.disabled_models

    async def recent_disable_record(self, model_configs: List[ModelConfig]) -> Optional[DisableRecord]:
        """
        取这批模型里最近一次的禁用记录

        用于「平台内已无可用模型」时回传真实根因（例如上一次是上游 401）。
        """
        async with self.lock:
            self._purge_expired(datetime.now())
            records = [
                self.disabled_models[mc.name]
                for mc in model_configs
                if mc.name in self.disabled_models
            ]
        if not records:
            return None
        return max(records, key=lambda record: record.recorded_at)

    async def nearest_recovery_seconds(self, model_configs: List[ModelConfig]) -> Optional[float]:
        """这批模型里最早的恢复时间（秒），用于 Retry-After"""
        async with self.lock:
            now = datetime.now()
            self._purge_expired(now)
            remaining = [
                self.disabled_models[mc.name].remaining_seconds(now)
                for mc in model_configs
                if mc.name in self.disabled_models
            ]
        if not remaining:
            return None
        return min(remaining)