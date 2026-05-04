import json
import logging
import os
from typing import List, Dict, Any, Optional
import redis.asyncio as redis

logger = logging.getLogger(__name__)


class RedisSessionStore:
    """
    基于 Redis 的 Responses API 会话状态存储器。
    用于维护 previous_response_id 到历史消息的映射。
    """

    def __init__(self, redis_url: Optional[str] = None, default_ttl: Optional[int] = None):
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379")
        self.default_ttl = default_ttl or int(os.getenv("RESPONSES_SESSION_TTL", "86400"))
        self.redis_client = redis.from_url(self.redis_url, decode_responses=True)

    async def get_history(self, response_id: str) -> List[Dict[str, Any]]:
        """根据 response_id 获取历史消息"""
        try:
            key = f"resp:session:{response_id}"
            data = await self.redis_client.get(key)
            if data:
                return json.loads(data)
            return []
        except Exception as e:
            logger.error(f"Failed to get history from Redis: {e}")
            return []

    async def save_session(self, response_id: str, messages: List[Dict[str, Any]], ttl: Optional[int] = None):
        """保存会话历史并设置过期时间"""
        try:
            key = f"resp:session:{response_id}"
            await self.redis_client.set(
                key, 
                json.dumps(messages), 
                ex=ttl or self.default_ttl
            )
        except Exception as e:
            logger.error(f"Failed to save session to Redis: {e}")

    async def close(self):
        """关闭 Redis 连接"""
        await self.redis_client.close()
