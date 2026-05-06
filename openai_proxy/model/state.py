import asyncio
import logging
from typing import Optional
from datetime import datetime, timedelta

from openai_proxy.models import ModelConfig

logger = logging.getLogger(__name__)


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

            is_available = model_config.name not in self.disabled_models

            return is_available
