"""
用户级密钥管理接口 - /admin/users

鉴权：与其他 /admin/* 一样只接受管理密钥（GATEWAY_API_KEY）；
用户级密钥访问这里会得到 403（见 service.py 的中间件）。

约束：
- 列表只返回可读前缀，永不返回明文或摘要；
- 明文密钥只在创建响应里出现一次，页面必须提示用户立即保存。
"""
import asyncio
import logging
from typing import Any, Dict

from fastapi import APIRouter
from pydantic import BaseModel, Field

from openai_proxy.auth.crypto import KeyDecryptionFailed, KeyEncryptionUnavailable
from openai_proxy.auth.manager import (
    KeyGenerationFailed,
    KeyNotRetrievable,
    UserKeyManager,
    UserKeyStoreUnavailable,
    UserNameExists,
    UserNotFound,
)
from openai_proxy.auth.store import parse_user_id
from openai_proxy.core.api_errors import ApiError

logger = logging.getLogger(__name__)


class CreateUserRequest(BaseModel):
    """新增用户：只需给一个用于统计与展示的标签"""

    name: str = Field(..., min_length=1, max_length=64, description="用户标签，例如 user-a")


class GetUserKeyRequest(BaseModel):
    """按用户名取回密钥明文"""

    name: str = Field(..., min_length=1, max_length=64, description="用户标签，例如 user-a")


def build_user_router(manager_provider) -> APIRouter:
    """
    构建用户密钥管理路由

    Args:
        manager_provider: 返回当前 UserKeyManager 的函数（应用创建后才绑定存储）
    """
    router = APIRouter()

    def _manager() -> UserKeyManager:
        return manager_provider()

    @router.post("/admin/users/list")
    async def list_users() -> Dict[str, Any]:
        """列出全部用户级密钥（只含前缀，不含明文）"""
        return await _run(_manager().list_users)

    @router.post("/admin/users", status_code=201)
    async def create_user(payload: CreateUserRequest) -> Dict[str, Any]:
        """新增用户并生成一把用户级密钥（加密落库，可后续取回）"""
        return await _run(_manager().create_user, payload.name)

    @router.post("/admin/users/key")
    async def get_user_key(payload: GetUserKeyRequest) -> Dict[str, Any]:
        """按用户名取回密钥明文（只接受管理密钥）"""
        return await _run(_manager().get_user_key, payload.name)

    @router.post("/admin/users/{user_id}/delete")
    async def delete_user(user_id: str) -> Dict[str, Any]:
        """删除用户：该用户的密钥立即失效，审计历史保留"""
        parsed = parse_user_id(user_id)
        if parsed is None:
            raise ApiError(400, "invalid_user_id", "user_id 必须是正整数")

        deleted = await _run(_manager().delete_user, parsed)
        if not deleted:
            raise ApiError(404, "user_not_found", "用户不存在")
        return {"deleted": True, "id": parsed}

    return router


async def _run(func, *args: Any) -> Any:
    """在线程池执行 SQLite 操作，并把可预期的错误转成统一 JSON 错误"""
    try:
        return await asyncio.to_thread(func, *args)
    except UserKeyStoreUnavailable as exc:
        raise ApiError(503, "key_store_unavailable", str(exc)) from exc
    except KeyEncryptionUnavailable as exc:
        raise ApiError(503, "key_encryption_unavailable", str(exc)) from exc
    except UserNotFound as exc:
        raise ApiError(404, "user_not_found", str(exc)) from exc
    except KeyNotRetrievable as exc:
        raise ApiError(409, "key_unavailable", str(exc)) from exc
    except KeyDecryptionFailed as exc:
        raise ApiError(500, "key_decrypt_failed", str(exc)) from exc
    except UserNameExists as exc:
        raise ApiError(409, "user_exists", str(exc)) from exc
    except KeyGenerationFailed as exc:
        raise ApiError(500, "key_generation_failed", str(exc)) from exc
    except ValueError as exc:
        raise ApiError(400, "invalid_name", str(exc)) from exc
