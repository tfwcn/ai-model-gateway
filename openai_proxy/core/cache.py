"""缓存系统模块 - 实现请求内容哈希和响应缓存"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
import hashlib
import json
import time
import logging

logger = logging.getLogger(__name__)


class Cache(ABC):
    """缓存抽象接口"""

    @abstractmethod
    async def get(self, key: str) -> Optional[Any]:
        """
        获取缓存

        Args:
            key: 缓存键

        Returns:
            缓存值，如果不存在返回 None
        """
        pass

    @abstractmethod
    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """
        设置缓存

        Args:
            key: 缓存键
            value: 缓存值
            ttl: 生存时间（秒）

        Returns:
            是否设置成功
        """
        pass

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """
        删除缓存

        Args:
            key: 缓存键

        Returns:
            是否删除成功
        """
        pass

    @abstractmethod
    async def clear(self) -> bool:
        """
        清除所有缓存

        Returns:
            是否清除成功
        """
        pass

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """
        检查缓存是否存在

        Args:
            key: 缓存键

        Returns:
            缓存是否存在
        """
        pass


class MemoryCache(Cache):
    """内存缓存实现"""

    def __init__(self, default_ttl: int = 300):
        """
        初始化内存缓存

        Args:
            default_ttl: 默认 TTL（秒）
        """
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._default_ttl = default_ttl

    async def get(self, key: str) -> Optional[Any]:
        """获取缓存"""
        if key in self._cache:
            entry = self._cache[key]
            # 检查是否过期
            if entry.get("expires_at", 0) > time.time():
                return entry.get("value")
            else:
                # 过期了，删除
                del self._cache[key]
        return None

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """设置缓存"""
        try:
            ttl = ttl if ttl is not None else self._default_ttl
            self._cache[key] = {
                "value": value,
                "expires_at": time.time() + ttl if ttl > 0 else float('inf')
            }
            return True
        except Exception as e:
            logger.error(f"内存缓存设置失败: {e}")
            return False

    async def delete(self, key: str) -> bool:
        """删除缓存"""
        if key in self._cache:
            del self._cache[key]
            return True
        return False

    async def clear(self) -> bool:
        """清除所有缓存"""
        self._cache.clear()
        return True

    async def exists(self, key: str) -> bool:
        """检查缓存是否存在"""
        if key in self._cache:
            entry = self._cache[key]
            if entry.get("expires_at", 0) > time.time():
                return True
            else:
                del self._cache[key]
        return False


class RedisCache(Cache):
    """Redis 缓存实现"""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        password: Optional[str] = None,
        default_ttl: int = 300
    ):
        """
        初始化 Redis 缓存

        Args:
            host: Redis 主机
            port: Redis 端口
            db: Redis 数据库
            password: Redis 密码
            default_ttl: 默认 TTL（秒）
        """
        self._host = host
        self._port = port
        self._db = db
        self._password = password
        self._default_ttl = default_ttl
        self._redis = None

    async def _get_redis(self):
        """获取 Redis 连接"""
        if self._redis is None:
            try:
                import redis.asyncio as aioredis
                self._redis = aioredis.Redis(
                    host=self._host,
                    port=self._port,
                    db=self._db,
                    password=self._password,
                    decode_responses=True
                )
            except ImportError:
                logger.error("Redis 缓存需要安装 redis 包: pip install redis")
                raise
        return self._redis

    async def get(self, key: str) -> Optional[Any]:
        """获取缓存"""
        try:
            redis = await self._get_redis()
            value = await redis.get(key)
            if value:
                return json.loads(value)
            return None
        except Exception as e:
            logger.error(f"Redis 缓存获取失败: {e}")
            return None

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """设置缓存"""
        try:
            redis = await self._get_redis()
            ttl = ttl if ttl is not None else self._default_ttl
            value_str = json.dumps(value, ensure_ascii=False)
            if ttl and ttl > 0:
                await redis.setex(key, ttl, value_str)
            else:
                await redis.set(key, value_str)
            return True
        except Exception as e:
            logger.error(f"Redis 缓存设置失败: {e}")
            return False

    async def delete(self, key: str) -> bool:
        """删除缓存"""
        try:
            redis = await self._get_redis()
            result = await redis.delete(key)
            return result > 0
        except Exception as e:
            logger.error(f"Redis 缓存删除失败: {e}")
            return False

    async def clear(self) -> bool:
        """清除所有缓存"""
        try:
            redis = await self._get_redis()
            await redis.flushdb()
            return True
        except Exception as e:
            logger.error(f"Redis 缓存清除失败: {e}")
            return False

    async def exists(self, key: str) -> bool:
        """检查缓存是否存在"""
        try:
            redis = await self._get_redis()
            return await redis.exists(key)
        except Exception as e:
            logger.error(f"Redis 缓存检查失败: {e}")
            return False


def compute_request_hash(request_data: Dict[str, Any]) -> str:
    """
    计算请求内容哈希值

    Args:
        request_data: 请求数据

    Returns:
        哈希值字符串
    """
    # 提取用于缓存的关键字段
    cache_key_data = {
        "model": request_data.get("model"),
        "messages": request_data.get("messages"),
        "temperature": request_data.get("temperature"),
        "top_p": request_data.get("top_p"),
        "max_completion_tokens": request_data.get("max_completion_tokens"),
        "stream": request_data.get("stream", False)
    }

    # 序列化为 JSON 并计算哈希
    key_str = json.dumps(cache_key_data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(key_str.encode('utf-8')).hexdigest()


class CacheManager:
    """缓存管理器 - 统一管理缓存操作"""

    def __init__(self, cache: Cache, default_ttl: int = 300):
        """
        初始化缓存管理器

        Args:
            cache: 缓存实例
            default_ttl: 默认 TTL（秒）
        """
        self._cache = cache
        self._default_ttl = default_ttl

    async def get_response(self, request_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        根据请求数据获取缓存的响应

        Args:
            request_data: 请求数据

        Returns:
            缓存的响应数据
        """
        # 流式响应不缓存
        if request_data.get("stream", False):
            return None

        cache_key = compute_request_hash(request_data)
        cached = await self._cache.get(cache_key)

        if cached:
            logger.debug(f"缓存命中: {cache_key[:16]}...")
            return cached

        logger.debug(f"缓存未命中: {cache_key[:16]}...")
        return None

    async def set_response(
        self,
        request_data: Dict[str, Any],
        response_data: Dict[str, Any],
        ttl: Optional[int] = None
    ) -> bool:
        """
        缓存响应数据

        Args:
            request_data: 请求数据
            response_data: 响应数据
            ttl: TTL（秒）

        Returns:
            是否缓存成功
        """
        # 流式响应不缓存
        if request_data.get("stream", False):
            return False

        cache_key = compute_request_hash(request_data)
        ttl = ttl if ttl is not None else self._default_ttl

        return await self._cache.set(cache_key, response_data, ttl)

    async def clear_cache(self) -> bool:
        """清除所有缓存"""
        return await self._cache.clear()

    async def delete_response(self, request_data: Dict[str, Any]) -> bool:
        """
        删除特定请求的缓存

        Args:
            request_data: 请求数据

        Returns:
            是否删除成功
        """
        cache_key = compute_request_hash(request_data)
        return await self._cache.delete(cache_key)
