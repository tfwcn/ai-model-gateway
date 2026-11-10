"""
运行时配置管理器 - 让「当前生效的配置」只有一个入口

设计目标：
- `ConfigLoader` 只负责解析文件，返回一份完整的 `ConfigSnapshot`
- `ConfigManager` 持有当前快照，负责初始化与进程内重载
- 重载时先构建完整的新快照，成功后再一次性替换，避免新旧配置混用
- 哪些改动必须重启由这里统一判断，调用方不需要各自记忆规则
"""
import asyncio
import hashlib
import logging
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional

from openai_proxy.core.config_loader import ConfigLoader, ConfigSnapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReloadResult:
    """一次进程内重载的结果"""

    snapshot: ConfigSnapshot
    reloaded: bool
    needs_restart: bool
    message: str
    # 替换前的快照：应用新快照失败时用于回退
    previous: Optional[ConfigSnapshot] = None

    def to_dict(self) -> Dict[str, Any]:
        """接口层使用的形状（与旧的 reload_models 返回保持一致）"""
        return {
            "reloaded": self.reloaded,
            "needs_restart": self.needs_restart,
            "message": self.message,
        }


class ConfigManager:
    """运行时配置的唯一入口

    Args:
        config_file: 配置文件路径
        loader: 可选的加载器（测试时可注入替身）
    """

    def __init__(self, config_file: str = "models.yaml", loader: Optional[ConfigLoader] = None):
        self.config_file = config_file
        self.loader = loader or ConfigLoader(config_file)
        # 初始为空快照：服务启动前读到的就是「还没有配置」
        self._current = ConfigSnapshot()
        self._initialized = False
        # 最近一次「已由管理接口处理、但不需要热重载」的文件版本：
        # 文件监听看到这个版本时直接跳过，避免覆盖 --no-reload / needs_restart 的决定
        self._ignored_file_version = ""
        # 已经提交、但进程内无法完全生效的变化：重启前每次重载都要继续提示
        self._pending_reasons: List[str] = []
        # 重载串行化：两个管理请求同时到达时，后一个总是基于前一个的结果
        self._reload_lock = asyncio.Lock()

    # ---------- 只读状态 ----------

    @property
    def current(self) -> ConfigSnapshot:
        """当前生效的配置快照（构建完成后不再修改）"""
        return self._current

    @property
    def plugin_manager(self):
        """插件实例管理器（插件本身不是配置快照的一部分）"""
        return self.loader.plugin_manager

    @property
    def initialized(self) -> bool:
        """是否已经成功提交过一份配置"""
        return self._initialized

    @property
    def ignored_file_version(self) -> str:
        """管理接口已处理、但刻意没有热重载的文件版本（监听器需要跳过）"""
        return self._ignored_file_version

    # ---------- 加载与重载 ----------

    async def initialize(self) -> ConfigSnapshot:
        """首次加载：成功后才替换当前快照"""
        snapshot = await self.loader.load_snapshot()
        self._current = snapshot
        self._initialized = True
        self._ignored_file_version = ""
        self._pending_reasons = []
        logger.info(
            f"配置已加载: {self.config_file}（{len(snapshot.models)} 个平台，"
            f"{sum(len(items) for items in snapshot.models.values())} 个模型）"
        )
        return snapshot

    async def load_platforms_only(self) -> Dict[str, Any]:
        """只加载平台结构（启动插件调度器前使用，不提交快照）"""
        return await self.loader.load_platforms_only()

    async def reload(self) -> ReloadResult:
        """
        进程内重载

        即使新配置需要重启才能完全生效，也会先提交快照：
        模型参数与别名立即生效，插件结构/audit 段等由调用方提示用户重启。
        """
        async with self._reload_lock:
            previous = self._current
            try:
                snapshot = await self.loader.load_snapshot()
            except Exception as exc:  # noqa: BLE001 - 重载失败时保留旧配置继续服务
                logger.error(f"重载配置失败，继续使用旧配置: {exc}", exc_info=True)
                return ReloadResult(previous, False, True, f"重载失败: {exc}")

            reasons = list(self._pending_reasons)
            for reason in self._restart_reasons(previous, snapshot):
                if reason not in reasons:
                    reasons.append(reason)
            self._pending_reasons = reasons
            self._current = snapshot
            self._initialized = True
            self._ignored_file_version = ""

            message = "模型配置已重载"
            if reasons:
                message += f"；{'、'.join(reasons)}，请自行重启服务后完全生效"
            logger.info(message)
            return ReloadResult(snapshot, True, bool(reasons), message, previous)

    def restore(self, snapshot: ConfigSnapshot) -> None:
        """应用新快照失败时回退到上一份快照（只用于服务层的失败兜底）"""
        if snapshot is not None:
            self._current = snapshot

    def replace_models(self, models: Dict[str, List[Any]]) -> None:
        """
        只替换当前快照的模型表

        管理接口的运行时模型清单读取的是这份快照；测试或嵌入式调用可以先注入模型表，
        不需要真的去解析配置文件。其他配置段保持不变。
        """
        self._current = replace(self._current, models=models or {})

    def ignore_current_file(self) -> None:
        """
        记录「当前文件内容已由管理接口处理，但刻意没有热重载」

        服务的写接口在 needs_restart / reload=false 时会跳过热重载；
        文件监听随后看到同一个版本时必须跳过，否则会绕过这个决定。
        """
        try:
            with open(self.config_file, "rb") as handle:
                self._ignored_file_version = hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            self._ignored_file_version = ""

    # ---------- 内部 ----------

    def _restart_reasons(self, previous: ConfigSnapshot, snapshot: ConfigSnapshot) -> List[str]:
        """比较新旧快照，找出进程内无法完全生效的变化"""
        if not self._initialized:
            # 第一次加载没有「旧配置」可比，不能把启动状态误报成需要重启
            return []

        reasons: List[str] = []
        if snapshot.plugin_signature != previous.plugin_signature:
            reasons.append("插件结构有变化")
        if snapshot.audit != previous.audit:
            reasons.append("audit 段有变化")
        return reasons
