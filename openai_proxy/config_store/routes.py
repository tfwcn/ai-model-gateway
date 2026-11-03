"""
配置管理接口 - /admin/config

鉴权：与其它 /admin/* 一样要求 GATEWAY_API_KEY（见 service.py 的中间件）。
并发：写接口支持 If-Match（或请求体里的 version）做乐观并发检查，冲突返回 409。
返回：一律只返回脱敏视图，密钥字段只给出它引用的环境变量名与是否已设置。
"""
import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Header
from pydantic import BaseModel, Field

from openai_proxy.config_store.editor import OPS
from openai_proxy.config_store.runtime_models import RuntimeModelCatalog
from openai_proxy.config_store.service import ConfigCommandService, ConfigConflict, ConfigInvalid
from openai_proxy.config_store.writer import ConfigWriteError
from openai_proxy.core.api_errors import ApiError

logger = logging.getLogger(__name__)


class PatchRequest(BaseModel):
    """补丁请求体"""

    ops: List[Dict[str, Any]] = Field(default_factory=list, description="补丁操作列表")
    version: Optional[str] = Field(None, description="客户端看到的配置版本号（乐观并发检查）")
    note: str = Field("", max_length=200, description="写进备份元数据的备注")
    reload: bool = Field(True, description="写盘后是否尝试进程内热重载")


class RollbackRequest(BaseModel):
    """回滚请求体"""

    name: str = Field(..., min_length=1, max_length=200, description="备份文件名")
    reload: bool = Field(True, description="回滚后是否尝试进程内热重载")


def schema_payload() -> Dict[str, Any]:
    """
    前端渲染表单与字段说明所需的全部元数据

    单独提成模块级函数：前端冒烟测试（无浏览器）直接读这份真实契约，
    避免测试里手写一份会漂移的假 schema。
    """
    from openai_proxy.config_store import config_guide, field_docs, schema

    return {
        "ops": list(OPS),
        "platform_fields": {field: kind for field, kind in schema.PLATFORM_FIELDS.items()},
        "model_fields": {field: kind for field, kind in schema.MODEL_FIELDS.items()},
        "audit_fields": {field: kind for field, kind in schema.AUDIT_FIELDS.items()},
        "limits": {field: {"min": low, "max": high} for field, (low, high) in schema._LIMITS.items()},
        # 取值只有有限几种的字段：界面渲染成下拉，而不是让人在输入框里猜写法
        "enums": field_docs.enum_fields(),
        # 字典按作用域分开：同一个 enabled 在平台级和 audit 段含义完全不同
        "docs": {
            "platform": field_docs.docs_for(list(schema.PLATFORM_FIELDS), "platform"),
            "model": field_docs.docs_for(list(schema.MODEL_FIELDS), "model"),
            "audit": field_docs.docs_for(list(schema.AUDIT_FIELDS), "audit"),
        },
        "sections": config_guide.guide_sections(),
    }


def build_config_router(
    service_provider: Callable[[], ConfigCommandService],
    runtime_provider: Optional[Callable[[], Dict[str, Any]]] = None,
) -> APIRouter:
    """
    构建配置管理路由

    Args:
        service_provider: 返回当前 ConfigCommandService 的函数（服务启动后才确定配置文件路径）
        runtime_provider: 返回进程内模型表 `{平台名: [ModelConfig, ...]}` 的函数。
                          爬虫平台的模型只存在于进程里，界面要给出候选就得问它。
                          不传就当服务还没加载模型（界面会显示「没有候选」而不是报错）。
    """
    router = APIRouter()
    catalog = RuntimeModelCatalog(runtime_provider or (lambda: {}))

    def _version_or(body_version: Optional[str], if_match: Optional[str]) -> Optional[str]:
        """版本号可以从 If-Match 头或请求体传入，二者冲突时直接报错"""
        header = (if_match or "").strip().strip('"')
        body = (body_version or "").strip()
        if header and body and header != body:
            raise ApiError(400, "version_mismatch", "If-Match 与请求体里的 version 不一致")
        return header or body or None

    # ---------- 读 ----------

    @router.post("/admin/config/document")
    async def config_document() -> Dict[str, Any]:
        """脱敏的配置视图（含可编辑字段与支持的补丁操作）"""
        return await _run(service_provider().view)

    @router.post("/admin/config/raw")
    async def config_raw() -> Dict[str, Any]:
        """原始 YAML 文本（供只读预览；写回请使用补丁接口）"""
        return await _run(service_provider().raw_text)

    @router.post("/admin/config/env-status")
    async def config_env_status() -> Dict[str, Any]:
        """配置引用的环境变量是否已设置（不返回密钥内容）"""
        return await _run(service_provider().env_status)

    @router.post("/admin/config/validate")
    async def config_validate() -> Dict[str, Any]:
        """校验当前配置文件"""
        return await _run(service_provider().validate)

    @router.post("/admin/config/schema")
    async def config_schema() -> Dict[str, Any]:
        """字段白名单、取值边界与字段字典（前端据此渲染表单与说明）"""
        return schema_payload()

    @router.post("/admin/config/backups")
    async def config_backups() -> Dict[str, Any]:
        """可回滚的备份列表"""
        return await _run(service_provider().backups)

    @router.post("/admin/config/models")
    async def config_models() -> Dict[str, Any]:
        """
        进程内当前可用的模型清单（只有平台名与模型条目名）

        爬虫平台的模型是插件灌进来的，配置文件里根本没有条目；界面要给出可勾的候选就只能问进程。
        """
        return catalog.payload()

    # ---------- 写 ----------

    @router.post("/admin/config/plan")
    async def config_plan(
        body: PatchRequest, if_match: Optional[str] = Header(None)
    ) -> Dict[str, Any]:
        """预演补丁：返回变更与行级差异，不写盘"""
        return await _run(service_provider().plan, body.ops, _version_or(body.version, if_match))

    @router.post("/admin/config/apply")
    async def config_apply(
        body: PatchRequest, if_match: Optional[str] = Header(None)
    ) -> Dict[str, Any]:
        """应用补丁并写盘（写盘前一定先备份）"""
        service = service_provider()
        result = await _run(
            service.write, body.ops, _version_or(body.version, if_match), body.note, body.reload
        )
        logger.info(f"配置已通过管理接口修改: {len(result['changes'])} 处变更")
        return result

    @router.post("/admin/config/rollback")
    async def config_rollback(
        body: RollbackRequest, if_match: Optional[str] = Header(None)
    ) -> Dict[str, Any]:
        """回滚到指定备份"""
        service = service_provider()
        expected = _version_or(None, if_match)
        if expected and expected != service.raw_text()["version"]:
            _raise_conflict(expected, service.raw_text()["version"])
        return await _run(service.rollback, body.name, body.reload)

    @router.post("/admin/config/reload")
    async def config_reload() -> Dict[str, Any]:
        """重新加载配置（改了 models.yaml 后不重启进程也能生效）"""
        service = service_provider()
        if service.reload_callback is None:
            raise ApiError(409, "reload_unavailable", "当前服务未提供热重载能力")
        return await _run(service.reload_callback)

    return router


async def _run(func: Callable[..., Dict[str, Any]], *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """在线程池执行文件操作，并把领域异常翻译成合适的 HTTP 状态码"""
    try:
        return await asyncio.to_thread(func, *args, **kwargs)
    except ConfigConflict as exc:
        _raise_conflict(exc.expected, exc.actual)
    except ConfigInvalid as exc:
        raise ApiError(
            400, "validation_failed", exc.message, {"errors": exc.errors}
        ) from exc
    except ConfigWriteError as exc:
        raise ApiError(500, "write_failed", exc.message) from exc
    except FileNotFoundError as exc:
        raise ApiError(404, "file_not_found", str(exc)) from exc
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc


def _raise_conflict(expected: str, actual: str) -> None:
    """版本冲突：把当前版本号带回给客户端，客户端刷新后重试即可"""
    raise ApiError(
        409,
        "version_conflict",
        "配置文件已被修改，请刷新后重试",
        {"expected": expected, "actual": actual},
    )
