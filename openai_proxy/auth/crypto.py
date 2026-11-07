"""
用户级密钥的加密存储 - 用对称加密保存可恢复的明文密钥

设计要点：
- 数据库里只保存 Fernet 密文，查询接口才能把密钥交还给管理方；
- 加密密钥来自 API_KEYS_ENCRYPTION_KEY；未配置时回退到 GATEWAY_API_KEY，
  保证零配置也能用，但轮换管理密钥会导致旧用户密钥无法解密（鉴权不受影响）；
- 任何解密失败都只影响「取回密钥」，不影响已发出的密钥继续鉴权。
"""
import base64
import hashlib
import logging
import os
from typing import Optional

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - 依赖缺失时降级为「不能取回密钥」
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

ENCRYPTION_KEY_ENV = "API_KEYS_ENCRYPTION_KEY"


class KeyEncryptionUnavailable(Exception):
    """没有可用的加密密钥，无法生成/取回用户密钥"""


class KeyDecryptionFailed(Exception):
    """密文无法解密（加密密钥被轮换或数据损坏）"""


class UserKeyCipher:
    """用户密钥的对称加解密"""

    def __init__(self, secret: str):
        if Fernet is None:
            raise KeyEncryptionUnavailable(
                "未安装 cryptography，无法加密保存用户密钥；请安装 requirements.txt 里的依赖"
            )
        # 任意长度的字符串都先做 SHA-256，再转成 Fernet 要求的 32 字节 URL-safe base64
        digest = hashlib.sha256(secret.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    @classmethod
    def from_env(cls, admin_key: str = "") -> Optional["UserKeyCipher"]:
        """优先用 API_KEYS_ENCRYPTION_KEY，缺失时回退到管理密钥"""
        secret = (os.getenv(ENCRYPTION_KEY_ENV, "") or "").strip()
        if not secret:
            fallback = (admin_key or "").strip()
            if not fallback:
                return None
            logger.warning(
                f"未配置 {ENCRYPTION_KEY_ENV}，已回退使用管理密钥加密用户密钥；"
                "轮换管理密钥后将无法取回旧用户密钥（鉴权不受影响）"
            )
            secret = fallback
        try:
            return cls(secret)
        except KeyEncryptionUnavailable as exc:
            logger.error(str(exc))
            return None

    def encrypt(self, key: str) -> str:
        """加密明文密钥，返回可直接落库的文本"""
        return self._fernet.encrypt((key or "").encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        """解密落库文本；失败时抛 KeyDecryptionFailed"""
        try:
            return self._fernet.decrypt((ciphertext or "").encode("utf-8")).decode("utf-8")
        except (InvalidToken, ValueError, TypeError) as exc:
            raise KeyDecryptionFailed("用户密钥无法解密，可能是加密密钥已轮换") from exc
