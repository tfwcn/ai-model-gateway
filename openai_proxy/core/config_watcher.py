"""
配置文件监听 - 手改 models.yaml 后也能自动触发热重载

实现方式是最简单的轮询，不引入额外依赖：
- 按固定间隔计算文件内容哈希，只有内容真的变化才动作
- 等一小段时间确认写入稳定，避免编辑器分两次保存时读到半个文件
- 热重载交给 `ConfigManager`；失败时保留当前配置，等文件下次再变化再试
"""
import asyncio
import hashlib
import logging
from typing import Awaitable, Callable, Optional

from openai_proxy.core.config_manager import ConfigManager, ReloadResult

logger = logging.getLogger(__name__)


class ConfigFileWatcher:
    """轮询配置文件并在内容变化后触发一次重载

    Args:
        manager: 运行时配置管理器
        on_reloaded: 重载成功后的回调（服务层在这里替换运行时状态）
        interval: 轮询间隔（秒）
        debounce: 检测到变化后等待写入稳定的时间（秒）
    """

    def __init__(
        self,
        manager: ConfigManager,
        on_reloaded: Optional[Callable[[ReloadResult], Awaitable[None]]] = None,
        interval: float = 2.0,
        debounce: float = 0.5,
    ):
        self.manager = manager
        self.on_reloaded = on_reloaded
        self.interval = max(0.1, float(interval))
        self.debounce = max(0.0, float(debounce))
        self._task: Optional[asyncio.Task] = None
        # 上一次处理过的文件哈希：无效文件也只处理一次，避免每轮都刷同一条错误
        self._last_seen = ""

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        """启动监听任务（重复调用不会创建多个任务）"""
        if self._task is not None:
            return
        self._last_seen = self._file_version()
        self._task = asyncio.create_task(self._loop())
        logger.info(f"配置文件监听已启动: {self.manager.config_file}")

    async def stop(self) -> None:
        """停止监听任务"""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 关闭阶段不抛
            pass
        self._task = None

    # ---------- 轮询 ----------

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.interval)
                await self._check_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001 - 监听失败不能影响转发
                logger.warning(f"检查配置文件变化失败: {exc}")

    async def _check_once(self) -> None:
        """发现内容变化后等写入稳定，再交给 ConfigManager 重载"""
        version = self._file_version()
        if not version or version == self._last_seen:
            return

        if self.debounce:
            await asyncio.sleep(self.debounce)
        # 写入过程中内容可能又变了：这一轮先放过，下一轮会看到最终版本
        if self._file_version() != version:
            return

        # 管理接口刚重载过：文件内容与当前快照一致，不需要重复重载
        if version == self.manager.current.version:
            self._last_seen = version
            return

        # 管理接口明确跳过了这次重载（needs_restart / reload=false）：监听器不要越权
        if version == self.manager.ignored_file_version:
            self._last_seen = version
            return

        result = await self.manager.reload()
        self._last_seen = version
        if not result.reloaded:
            logger.error(f"配置文件变化后重载失败，继续使用旧配置: {result.message}")
            return

        logger.warning(f"检测到配置文件变化，已自动热重载: {result.message}")
        if self.on_reloaded is not None:
            await self.on_reloaded(result)

    def _file_version(self) -> str:
        """文件内容哈希；文件不存在或暂时读不到时返回空串"""
        path = self.manager.config_file
        try:
            with open(path, "rb") as handle:
                return hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            return ""
