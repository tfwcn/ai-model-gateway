"""
配置命令服务 - 把「读文档 → 打补丁 → 校验 → 写盘 → 重载」串成一条命令

前端接口与 CLI 都调用这里的同一套方法，保证两者行为完全一致：
    validate()  只校验当前文件
    plan()      预演补丁，返回变更与差异，不写盘
    write()     真正写盘（乐观并发检查 + 备份 + 可选热重载）
    rollback()  回滚到某份备份

乐观并发检查：调用方带上自己看到的 version；期间文件被别人改过就返回冲突，
避免界面上挂着的旧页面把别人的改动覆盖掉。
"""
import difflib
import logging
from typing import Any, Callable, Dict, List, Optional

from openai_proxy.config_store import schema
from openai_proxy.config_store.document import ConfigDocument
from openai_proxy.config_store.editor import PatchResult, apply_patch
from openai_proxy.config_store.line_diff import LineDiff
from openai_proxy.config_store.writer import ConfigWriter

logger = logging.getLogger(__name__)

# 这些操作无法在进程内生效，必须重启进程
# audit 段只在启动时应用一次（数据库路径/开关/刷盘参数都可能变化），同样按重启处理
_RESTART_OPS = ("add_platform", "remove_platform", "set_audit_field")


class ConfigConflict(Exception):
    """配置文件已被他人改动（If-Match 不匹配）"""

    def __init__(self, expected: str, actual: str):
        super().__init__("配置版本冲突")
        self.expected = expected
        self.actual = actual


class ConfigInvalid(Exception):
    """校验不通过，拒绝写盘"""

    def __init__(self, errors: List[Dict[str, str]], message: str = "配置校验未通过"):
        super().__init__(message)
        self.message = message
        self.errors = errors


class ConfigCommandService:
    """配置编辑命令服务"""

    def __init__(
        self,
        config_file: str = "models.yaml",
        backup_dir: str = "data/config_backups",
        reload_callback: Optional[Callable[[], Any]] = None,
        audit_recorder: Optional[Callable[..., bool]] = None,
        write_observer: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        """
        Args:
            config_file: 配置文件路径
            backup_dir: 备份目录
            reload_callback: 写盘后调用的进程内重载函数（由服务层注入）
            audit_recorder: 记录配置变更的回调（写进审计表，可在 /admin/audit 查历史）
            write_observer: 每次写盘/回滚后的观察回调（服务层用它告诉文件监听「这次已处理」，
                            避免主动跳过重载后又被监听器自动重载）
        """
        self.config_file = config_file
        self.writer = ConfigWriter(config_file, backup_dir)
        self.reload_callback = reload_callback
        self.audit_recorder = audit_recorder
        self.write_observer = write_observer

    # ---------- 读 ----------

    def document(self) -> ConfigDocument:
        """当前配置文档"""
        return ConfigDocument.load(self.config_file)

    def view(self) -> Dict[str, Any]:
        """脱敏视图（不含密钥内容）"""
        return self.document().view()

    def raw_text(self) -> Dict[str, Any]:
        """原始 YAML 文本与版本号"""
        document = self.document()
        return {"path": document.path, "version": document.version, "text": document.text}

    def env_status(self) -> Dict[str, Any]:
        """配置引用的环境变量是否已设置（不含值）"""
        document = self.document()
        return {"version": document.version, "items": document.env_status()}

    def validate(self) -> Dict[str, Any]:
        """校验当前文件"""
        document = self.document()
        errors, warnings = schema.split_issues(document.validate())
        return {"version": document.version, "valid": not errors, "errors": errors, "warnings": warnings}

    def backups(self) -> Dict[str, Any]:
        """可回滚的备份列表"""
        return {"items": self.writer.backups()}

    # ---------- 写 ----------

    def plan(self, ops: List[Dict[str, Any]], version: Optional[str] = None) -> Dict[str, Any]:
        """预演补丁：返回变更、差异与校验结果，不写盘"""
        document = self._require_version(version)
        return self._summarize(document, apply_patch(document, ops))

    def write(
        self,
        ops: List[Dict[str, Any]],
        version: Optional[str] = None,
        note: str = "",
        reload: bool = True,
    ) -> Dict[str, Any]:
        """
        应用补丁并写盘

        校验有 error 时直接拒绝，不留下半成品；写盘前的旧内容一定会先备份。

        Raises:
            ConfigConflict: 版本号与当前文件不匹配
            ConfigInvalid: 校验存在 error
        """
        document = self._require_version(version)
        result = apply_patch(document, ops)
        summary = self._summarize(document, result)

        if not summary["valid"]:
            raise ConfigInvalid(summary["errors"])
        if not ops:
            raise ConfigInvalid([{"level": "error", "path": "ops", "message": "没有任何要应用的操作"}])

        written = self.writer.write_text(result.document.dump(), note or _default_note(ops))
        summary["backup"] = written["backup"]
        summary["version_after"] = written["version_after"]
        summary["reloaded"] = False

        # 插件/爬虫结构变化无法在进程内重建，交给用户自己重启
        if reload and not summary["needs_restart"] and self.reload_callback is not None:
            outcome = self.reload_callback() or {}
            summary["reload"] = outcome
            summary["reloaded"] = bool(outcome.get("reloaded"))
            # 重载器可能发现文件里还有更早提交、但一直没生效的变化（例如 audit 段）
            if outcome.get("needs_restart"):
                summary["needs_restart"] = True
            if outcome.get("message"):
                summary["message"] = outcome["message"]

        self._record(_ops_names(ops), written["version_after"], success=True)
        self._notify_written(summary)
        return summary

    def rollback(self, name: str, reload: bool = True) -> Dict[str, Any]:
        """回滚到指定备份（当前内容也会先备份，因此回滚本身可再回滚）"""
        before = self.raw_text()
        written = self.writer.rollback(name)

        result: Dict[str, Any] = {
            "rolled_back_to": name,
            "backup": written["backup"],
            "version_before": before["version"],
            "version_after": written["version_after"],
            "reloaded": False,
        }
        if reload and self.reload_callback is not None:
            outcome = self.reload_callback() or {}
            result["reload"] = outcome
            result["reloaded"] = bool(outcome.get("reloaded"))
            if outcome.get("needs_restart"):
                result["needs_restart"] = True
            if outcome.get("message"):
                result["message"] = outcome["message"]

        self._record("rollback", f"{name} -> {written['version_after'][:12]}", success=True)
        self._notify_written(result)
        return result

    # ---------- 内部 ----------

    def _notify_written(self, result: Dict[str, Any]) -> None:
        """通知服务层「这次写盘已经处理过」；观察回调失败不能影响写盘结果"""
        if self.write_observer is None:
            return
        try:
            self.write_observer(result)
        except Exception as exc:  # noqa: BLE001 - 观察回调只是协调监听器
            logger.debug(f"配置写盘观察回调失败: {exc}")

    def _require_version(self, version: Optional[str]) -> ConfigDocument:
        """加载文档并做乐观并发检查"""
        document = self.document()
        if version and version != document.version:
            raise ConfigConflict(version, document.version)
        return document

    def _summarize(self, document: ConfigDocument, result: PatchResult) -> Dict[str, Any]:
        """补丁摘要 + 行级差异 + 是否需要重启"""
        summary = result.summary()
        after = result.document.dump()
        summary["diff"] = _unified_diff(document.text, after)
        # 界面要的是「每一行是新增还是修改还是删除」，所以同一份差异再多给一种按类别拆开的形状
        summary["diff_rows"] = LineDiff(document.text, after).rows()
        summary["needs_restart"] = _needs_restart(result.changes)
        return summary

    def _record(self, ops: str, version: str, success: bool) -> None:
        """把这次写入记进审计表（审计关闭时静默跳过）"""
        if self.audit_recorder is None:
            return
        try:
            self.audit_recorder(ops, detail=version[:12], success=success)
        except Exception as exc:  # noqa: BLE001 - 记录失败不影响配置写入结果
            logger.debug(f"记录配置变更失败: {exc}")


# ---------- 内部工具 ----------


def _unified_diff(before: str, after: str) -> List[str]:
    """行级差异：让用户在写盘前就看到「到底改了哪几行」"""
    return list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile="当前配置",
            tofile="应用补丁后",
            lineterm="",
            n=2,
        )
    )


def _needs_restart(changes: List[Dict[str, Any]]) -> bool:
    """补丁里是否包含必须重启才能生效的操作"""
    for change in changes:
        path = str(change.get("path") or "")
        if change.get("op") in _RESTART_OPS or path.endswith(".plugin"):
            return True
    return False


def _ops_names(ops: List[Dict[str, Any]]) -> str:
    """操作名列表，用于审计与备份备注"""
    return ", ".join(str(op.get("op")) for op in ops) or "无操作"


def _default_note(ops: List[Dict[str, Any]]) -> str:
    """备份备注：直接列出这次做了哪些操作"""
    if not ops:
        return "无操作"
    return ", ".join(
        f"{op.get('op')}:{op.get('platform') or op.get('alias') or op.get('name') or op.get('model') or op.get('field') or ''}"
        for op in ops
    )
