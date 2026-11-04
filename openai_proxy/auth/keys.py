"""
网关 API 密钥 - 管理密钥的校验与用户级密钥的内存快照

设计要点：
- GATEWAY_API_KEY 是管理密钥：可访问全部接口（含 /admin 与 /metrics），
  数据面请求按 admin 身份记账，兼容原有单密钥部署。
- 用户级密钥存放在 SQLite（见 UserKeyManager），进程内只保留「哈希 → 用户标签」的快照，
  请求热路径不碰数据库；新增/删除后由管理接口刷新快照。
- 数据库里保存密钥的 SHA-256、可读前缀和加密密文；鉴权只查摘要。
- 两者都不存在时保持原有的「不鉴权」行为；只配置用户密钥时管理接口仍然关闭。
- 管理密钥用常量时间比较，避免通过响应时间侧信道试探。
"""
import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Dict, Mapping, Optional

logger = logging.getLogger(__name__)

# 管理密钥的环境变量名（保持原名，兼容已有部署）
ADMIN_KEY_ENV = "GATEWAY_API_KEY"

# 调用方角色
ADMIN_ROLE = "admin"
USER_ROLE = "user"

# 默认调用方标签：管理与未启用鉴权时的流量都记在 admin 名下
ADMIN_NAME = "admin"

# 用户密钥的可读前缀：只用于管理页面辨认，不参与鉴权
USER_KEY_PREFIX = "gw_"


@dataclass(frozen=True)
class GatewayCaller:
    """一次请求的调用方身份（仅供进程内使用，密钥本身不会进入审计）"""

    name: str
    role: str

    @property
    def is_admin(self) -> bool:
        """是否管理身份（可以访问 /admin 与 /metrics）"""
        return self.role == ADMIN_ROLE


def hash_api_key(key: str) -> str:
    """用户密钥落库前的单向摘要（SQLite 里不保存明文）"""
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()


class ApiKeyRegistry:
    """密钥表：把请求头里的 Bearer token 解析成调用方身份"""

    def __init__(self, admin_key: str = ""):
        self._admin_key = (admin_key or "").strip()
        # key_hash -> 用户标签；整体替换保证刷新时请求读到的是完整快照
        self._users: Dict[str, str] = {}

    @property
    def admin_enabled(self) -> bool:
        """管理接口是否可用（需要管理密钥）"""
        return bool(self._admin_key)

    @property
    def enabled(self) -> bool:
        """是否启用了任意一种密钥（都为空时保持不鉴权行为）"""
        return bool(self._admin_key or self._users)

    @property
    def user_names(self) -> tuple:
        """已配置的用户标签（只暴露标签，不暴露任何密钥）"""
        return tuple(sorted(set(self._users.values())))

    def has_user_name(self, name: str) -> bool:
        """用户名是否已在当前快照里（用于数据面自动创建前的快速判断）"""
        return bool(name) and name in self._users.values()

    def set_user_hashes(self, users: Mapping[str, str]) -> None:
        """用最新快照替换用户密钥表（key_hash -> 用户标签）"""
        self._users = {key: name for key, name in users.items() if key and name}

    def resolve(self, token: str) -> Optional[GatewayCaller]:
        """把 token 解析成调用方；不认识的 token 返回 None"""
        clean_token = (token or "").strip()
        if not clean_token:
            return None

        if self._admin_key and _same_secret(clean_token, self._admin_key):
            return GatewayCaller(name=ADMIN_NAME, role=ADMIN_ROLE)

        # 命中用户密钥：只按摘要查表，数据库与内存里都没有明文可比
        name = self._users.get(hash_api_key(clean_token))
        if name:
            return GatewayCaller(name=name, role=USER_ROLE)
        return None

    def log_summary(self) -> None:
        """启动日志只写标签与数量，永远不打印密钥"""
        if not self.enabled:
            logger.info("未配置网关密钥，所有接口保持开放（仅建议内网使用）")
            return
        if not self.admin_enabled:
            logger.warning(f"未配置 {ADMIN_KEY_ENV}，管理接口保持关闭")
        if self.user_names:
            logger.info(f"已启用用户级密钥: {len(self.user_names)} 个用户（{', '.join(self.user_names)}）")


def _same_secret(value: str, expected: str) -> bool:
    """常量时间比较，避免长度/前缀差异从响应时间上泄漏"""
    return hmac.compare_digest(value.encode("utf-8"), expected.encode("utf-8"))
