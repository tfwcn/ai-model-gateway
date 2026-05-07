import json
import logging
import os
from typing import List, Dict, Any, Optional
import redis.asyncio as redis

logger = logging.getLogger(__name__)


class SessionStore:
    """
    会话存储接口，支持 Redis 和本地文件双模存储。
    优先使用 Redis，当 Redis 不可用时自动降级为本地文件存储。
    """

    async def get_history(self, response_id: str) -> List[Dict[str, Any]]:
        """根据 response_id 获取历史消息"""
        raise NotImplementedError

    async def save_session(self, response_id: str, messages: List[Dict[str, Any]], original_output: Optional[List] = None, ttl: Optional[int] = None):
        """保存会话历史"""
        raise NotImplementedError

    async def close(self):
        """关闭连接"""
        raise NotImplementedError


class RedisSessionStore(SessionStore):
    """
    基于 Redis 的 Responses API 会话状态存储器。
    用于维护 previous_response_id 到历史消息的映射。
    """

    def __init__(self, redis_url: Optional[str] = None, default_ttl: Optional[int] = None):
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379")
        self.default_ttl = default_ttl or int(os.getenv("RESPONSES_SESSION_TTL", "86400"))
        self.redis_client = redis.from_url(self.redis_url, decode_responses=True)
        self._available = True

    async def _check_availability(self):
        """检查 Redis 是否可用"""
        if not self._available:
            return False
        try:
            await self.redis_client.ping()
            return True
        except Exception as e:
            logger.warning(f"Redis connection failed: {e}, falling back to file storage")
            self._available = False
            return False

    async def get_history(self, response_id: str) -> List[Dict[str, Any]]:
        """根据 response_id 获取历史消息"""
        if not await self._check_availability():
            return []

        try:
            key = f"resp:session:{response_id}"
            data = await self.redis_client.get(key)
            if data:
                return json.loads(data)
            return []
        except Exception as e:
            logger.error(f"Failed to get history from Redis: {e}")
            self._available = False
            return []

    async def save_session(self, response_id: str, messages: List[Dict[str, Any]], original_output: Optional[List] = None, ttl: Optional[int] = None):
        """保存会话历史并设置过期时间"""
        if not await self._check_availability():
            return

        try:
            key = f"resp:session:{response_id}"
            # 同时保存 messages 和原始 output
            session_data = {
                "messages": messages,
                "original_output": original_output
            }
            await self.redis_client.set(
                key,
                json.dumps(session_data),
                ex=ttl or self.default_ttl
            )
        except Exception as e:
            logger.error(f"Failed to save session to Redis: {e}")
            self._available = False

    async def close(self):
        """关闭 Redis 连接"""
        try:
            await self.redis_client.close()
        except:
            pass


class FileSessionStore(SessionStore):
    """
    基于本地文件的会话存储器，用于调试和容错。
    文件命名格式：resp_xxx.json
    """

    def __init__(self, storage_dir: str = "config/sessions"):
        self.storage_dir = storage_dir
        os.makedirs(self.storage_dir, exist_ok=True)
        logger.info(f"FileSessionStore initialized at: {self.storage_dir}")

    def _get_file_path(self, response_id: str) -> str:
        """获取文件路径"""
        return os.path.join(self.storage_dir, f"{response_id}.json")

    async def get_history(self, response_id: str) -> List[Dict[str, Any]]:
        """根据 response_id 获取历史消息"""
        file_path = self._get_file_path(response_id)
        try:
            if os.path.exists(file_path):
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # 兼容旧格式（直接是 messages 数组）和新格式（包含 messages 和 original_output）
                    if isinstance(data, dict):
                        return data.get("messages", [])
                    else:
                        return data
            return []
        except Exception as e:
            logger.error(f"Failed to read session file {file_path}: {e}")
            return []

    async def save_session(self, response_id: str, messages: List[Dict[str, Any]], original_output: Optional[List] = None, ttl: Optional[int] = None):
        """保存会话历史到文件"""
        file_path = self._get_file_path(response_id)
        try:
            session_data = {
                "messages": messages,
                "original_output": original_output
            }
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(session_data, f, ensure_ascii=False, indent=2)
            logger.debug(f"Session saved to file: {file_path}")
        except Exception as e:
            logger.error(f"Failed to save session to file {file_path}: {e}")

    async def close(self):
        """文件存储无需关闭连接"""
        pass


class DualModeSessionStore(SessionStore):
    """
    双模会话存储器：优先使用 Redis，失败时自动降级到文件存储。
    """

    def __init__(self, redis_url: Optional[str] = None, default_ttl: Optional[int] = None, storage_dir: str = "config/sessions"):
        self.redis_store = RedisSessionStore(redis_url, default_ttl)
        self.file_store = FileSessionStore(storage_dir)
        self._using_redis = True

    async def get_history(self, response_id: str) -> List[Dict[str, Any]]:
        """根据 response_id 获取历史消息"""
        if self._using_redis:
            try:
                data = await self.redis_store.get_history(response_id)
                if data:
                    return data
            except Exception as e:
                logger.warning(f"Redis get failed, falling back to file: {e}")
                self._using_redis = False

        # 降级到文件存储
        return await self.file_store.get_history(response_id)

    async def save_session(self, response_id: str, messages: List[Dict[str, Any]], original_output: Optional[List] = None, ttl: Optional[int] = None):
        """保存会话历史"""
        if self._using_redis:
            try:
                await self.redis_store.save_session(response_id, messages, original_output, ttl)
                return
            except Exception as e:
                logger.warning(f"Redis save failed, falling back to file: {e}")
                self._using_redis = False

        # 降级到文件存储
        await self.file_store.save_session(response_id, messages, original_output, ttl)

    async def close(self):
        """关闭连接"""
        await self.redis_store.close()
        await self.file_store.close()
