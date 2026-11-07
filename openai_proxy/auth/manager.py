"""
用户级密钥管理 - 组装 SQLite 存储与内存鉴权快照

对外动作：
    start()    打开库、建表、加载快照（启动时调用；失败只降级，不影响转发）
    close()    关闭库
    create()   生成新密钥并加密落库
    delete()   删除用户并刷新快照
    resolve()  请求热路径：只读内存快照
"""
import asyncio
import logging
import os
import secrets
import sqlite3
from typing import Any, Dict, Optional

from openai_proxy.auth.crypto import UserKeyCipher
from openai_proxy.auth.keys import (
    ADMIN_KEY_ENV,
    USER_KEY_PREFIX,
    ApiKeyRegistry,
    GatewayCaller,
    hash_api_key,
)
from openai_proxy.auth.store import UserKeyStore, normalize_name

logger = logging.getLogger(__name__)

# 密钥库路径：与审计库分开，关闭审计不影响鉴权
DB_PATH_ENV = "API_KEYS_DB_PATH"
DEFAULT_DB_PATH = "data/api_keys.db"

# 明文密钥前缀的可读长度（gw_ + 6 位随机字符）
_PREFIX_VISIBLE = len(USER_KEY_PREFIX) + 6
# 摘要碰撞的概率可以忽略，但仍然给一个有限重试
_KEY_RETRY = 3


class UserKeyStoreUnavailable(Exception):
    """密钥库未启动或打开失败，管理接口据此返回 503"""


class UserNameExists(Exception):
    """用户名已被占用（接口层转成 409 user_exists）"""


class KeyGenerationFailed(RuntimeError):
    """连续生成密钥都撞上摘要（概率极低）"""


class UserNotFound(Exception):
    """查询/操作的用户不存在"""


class KeyNotRetrievable(Exception):
    """用户存在，但没有可解密的历史密钥（旧版本创建）"""


class UserKeyManager:
    """用户级密钥的增删、持久化与内存快照"""

    def __init__(self, admin_key: str = "", db_path: Optional[str] = None):
        self.admin_key = (admin_key or "").strip()
        self.registry = ApiKeyRegistry(admin_key=self.admin_key)
        self.cipher = UserKeyCipher.from_env(self.admin_key)
        self.db_path = db_path or os.getenv(DB_PATH_ENV) or DEFAULT_DB_PATH
        self.store: Optional[UserKeyStore] = None

    @classmethod
    def from_env(cls) -> "UserKeyManager":
        """按环境变量创建（管理密钥 + 密钥库路径）"""
        return cls(admin_key=os.getenv(ADMIN_KEY_ENV, ""))

    @property
    def started(self) -> bool:
        """密钥库是否已经打开"""
        return self.store is not None

    @property
    def admin_enabled(self) -> bool:
        """管理接口是否可用（与注册表同口径）"""
        return self.registry.admin_enabled

    @property
    def enabled(self) -> bool:
        """是否启用了任意一种密钥"""
        return self.registry.enabled

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        """打开库并加载快照；任何异常都降级为「只有管理密钥可用」"""
        try:
            await asyncio.to_thread(self.start_sync)
        except Exception as exc:  # noqa: BLE001 - 鉴权库故障不能阻止网关启动
            logger.error(f"用户密钥库初始化失败，已降级为仅管理密钥: {exc}", exc_info=True)
            self.store = None
            self.registry.set_user_hashes({})
            self.registry.log_summary()

    def start_sync(self) -> None:
        """同步打开库（测试与启动逻辑共用）"""
        store = UserKeyStore(self.db_path)
        store.init_schema()
        self.store = store
        self.reload()
        logger.info(f"用户密钥库已就绪: {self.db_path}（{len(self.registry.user_names)} 个用户）")
        self.registry.log_summary()

    async def close(self) -> None:
        """关闭库（关闭阶段不抛异常）"""
        store = self.store
        self.store = None
        if store is None:
            return
        try:
            await asyncio.to_thread(store.close)
        except Exception as exc:  # noqa: BLE001 - 关闭阶段不抛
            logger.debug(f"关闭用户密钥库失败: {exc}")

    # ---------- 鉴权热路径 ----------

    def resolve(self, token: str) -> Optional[GatewayCaller]:
        """解析调用方身份（只读内存快照）"""
        return self.registry.resolve(token)

    # ---------- 管理动作（同步，由路由放进线程池） ----------

    def reload(self) -> int:
        """从数据库重新加载快照，返回用户数量"""
        store = self._require_store()
        self.registry.set_user_hashes(store.snapshot())
        return len(self.registry.user_names)

    def list_users(self) -> Dict[str, Any]:
        """管理页面用的用户清单（不含密钥与摘要）"""
        store = self._require_store()
        items = store.list_users()
        return {"count": len(items), "items": items}

    def create_user(self, name: str) -> Dict[str, Any]:
        """生成并落库一把新密钥；返回 {user, key}"""
        store = self._require_store()
        clean_name = normalize_name(name)
        if store.exists_name(clean_name):
            raise UserNameExists(f"用户名已存在: {clean_name}")

        for _ in range(_KEY_RETRY):
            key = build_user_key()
            ciphertext = self.cipher.encrypt(key) if self.cipher is not None else None
            try:
                user = store.create_user(
                    clean_name,
                    key[:_PREFIX_VISIBLE],
                    hash_api_key(key),
                    ciphertext,
                )
            except sqlite3.IntegrityError:
                # 用户名冲突（并发创建）优先报重名；摘要碰撞则换一把重试
                if store.exists_name(clean_name):
                    raise UserNameExists(f"用户名已存在: {clean_name}") from None
                continue
            self.reload()
            return {"user": user, "key": key}
        raise KeyGenerationFailed("生成密钥失败，请重试")

    def get_user_key(self, name: str) -> Dict[str, Any]:
        """按用户名取回密钥明文（管理接口专用）"""
        store = self._require_store()
        clean_name = normalize_name(name)
        record = store.get_by_name(clean_name)
        if record is None:
            raise UserNotFound(f"用户不存在: {clean_name}")
        ciphertext = record.pop("key_ciphertext", None)
        if not ciphertext or self.cipher is None:
            raise KeyNotRetrievable(
                f"用户 {clean_name} 的密钥是旧版本创建的，无法取回明文；请删除后重新创建"
            )
        return {"user": record, "key": self.cipher.decrypt(ciphertext)}

    def ensure_user(self, name: str) -> str:
        """
        数据面用：用户名存在则直接返回；不存在则自动创建用户与密钥

        返回规范化后的用户名，供审计使用。并发创建同一个名字时，
        唯一约束会兜底，最终都返回同一个名字。
        """
        clean_name = normalize_name(name)
        if self.registry.has_user_name(clean_name):
            return clean_name

        store = self._require_store()
        if store.exists_name(clean_name):
            self.reload()
            return clean_name

        try:
            self.create_user(clean_name)
        except UserNameExists:
            self.reload()
        return clean_name

    async def ensure_user_async(self, name: str) -> str:
        """数据面热路径的异步入口：只在需要新建用户时才碰磁盘"""
        return await asyncio.to_thread(self.ensure_user, name)

    def delete_user(self, user_id: int) -> bool:
        """删除用户并立即刷新快照，已发出的密钥马上失效"""
        store = self._require_store()
        deleted = store.delete_user(user_id)
        if deleted:
            self.reload()
        return deleted

    # ---------- 内部 ----------

    def _require_store(self) -> UserKeyStore:
        if self.store is None:
            raise UserKeyStoreUnavailable(
                f"用户密钥库未就绪（{self.db_path}），请检查启动日志或 {DB_PATH_ENV}"
            )
        return self.store


def build_user_key() -> str:
    """生成一把用户密钥：可读前缀 + 32 字节随机串"""
    return f"{USER_KEY_PREFIX}{secrets.token_urlsafe(32)}"
