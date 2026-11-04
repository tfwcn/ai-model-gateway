import asyncio
import json
import logging
import os
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Dict, List, Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from openai_proxy.admin_ui import AdminUiStatic
from openai_proxy.core.api_errors import error_response, install_error_handlers, is_forward_path
from openai_proxy.core.cache import MemoryCache
from openai_proxy.models import ModelConfig, list_available_models
from openai_proxy.core.config_loader import ConfigLoader
from openai_proxy.model.failover import ModelFailoverManager
from openai_proxy.adapter.responses import ResponsesAdapter
from openai_proxy.utils.session import DualModeSessionStore
from openai_proxy.utils.sse_parser import SSEEventParser
from openai_proxy.utils.keepalive import KeepAliveStream, UpstreamStartHeartbeat, HeartbeatCounter
from openai_proxy.utils.logger import sanitize_payload
from openai_proxy.utils.stream_metrics import StreamPerformanceMonitor
from openai_proxy.utils.stream_terminator import StreamTerminator, is_done_event
from openai_proxy.audit import AuditContext, AuditManager, load_metrics_collector
from openai_proxy.audit.routes import build_audit_router
from openai_proxy.auth import ADMIN_NAME, UserKeyManager
from openai_proxy.auth.crypto import KeyDecryptionFailed, KeyEncryptionUnavailable
from openai_proxy.auth.manager import KeyGenerationFailed, UserKeyStoreUnavailable
from openai_proxy.auth.routes import build_user_router
from openai_proxy.config_store.routes import build_config_router
from openai_proxy.config_store.service import ConfigCommandService
from openai_proxy.passthrough import PassthroughManager

logger = logging.getLogger(__name__)

# 数据面用户 header：只有携带管理密钥时才生效，用户密钥会忽略它
USER_HEADER = "X-Gateway-User"


async def _finish_stream_audit(audit_ctx: Optional[AuditContext], monitor: StreamPerformanceMonitor, completed: bool,
                               keepalive: Any = None, done_sent: bool = False) -> None:
    """
    收尾一次流式调用的审计（绝不外抛异常，否则破坏响应收尾）

    completed=False 说明转发循环没有正常走完（通常是客户端关闭连接）；
    done_sent=True 说明 [DONE] 已经转发给客户端，内容其实已经发完，
    此时即使连接被关也不算「客户端中断」，结局仍记 success。
    """
    if audit_ctx is None:
        return
    try:
        stats = monitor.finish()
        # 心跳计数对象可能是 HeartbeatCounter（共享）或带 heartbeat_count 的包裹层
        heartbeat_count = getattr(keepalive, "count", None)
        if heartbeat_count is None:
            heartbeat_count = getattr(keepalive, "heartbeat_count", 0)
        if heartbeat_count:
            audit_ctx.record_keepalive(int(heartbeat_count))
        audit_ctx.finalize_last(stats, aborted=not completed, done_sent=done_sent)
    except Exception as e:  # noqa: BLE001 - 审计收尾不得影响响应
        logger.debug(f"流式审计收尾失败: {e}")


async def _forward_normalized_sse(upstream_stream, audit_ctx: Optional[AuditContext] = None, keepalive: Any = None):
    """
    标准化 SSE 流转发（公共逻辑）

    使用 SSEEventParser 按完整事件分割并标准化，自动补发 [DONE]。
    消除 /v1/chat/completions 与 /v1/responses 间的重复代码。

    同时埋点：统计首块耗时、总耗时与输出 token，输出 tokens/s。

    转发节奏由 StreamTerminator 控制：[DONE] 一发出去就认定内容已发完，
    只再用一个很短的收尾窗口等可能跟在后面的 usage 尾片，不再死等上游结束响应体。
    """
    parser = SSEEventParser(normalize=True)
    monitor = StreamPerformanceMonitor.from_stream(upstream_stream)
    terminator = StreamTerminator(upstream_stream)
    completed = False
    try:
        while True:
            chunk = await terminator.next_chunk()
            if chunk is None:
                # 上游真的结束，或收尾窗口到期：两种情况都算转发走完
                break
            if not chunk:
                continue
            try:
                events = parser.feed(chunk)
                for event in events:
                    if not event.strip():
                        continue
                    monitor.observe(event)
                    # 先标记再交出：客户端很可能收到 [DONE] 就关连接去执行工具，
                    # 这时 [DONE] 已经算发出去了，不能再把这次关闭记成中断
                    if is_done_event(event):
                        terminator.mark_done()
                    yield (event + "\n\n").encode("utf-8")
            except Exception as e:
                logger.error(f"SSE normalization error: {e}", exc_info=True)
                yield chunk  # 降级：透传原始 chunk
        if not terminator.done_seen:
            logger.info("Upstream did not send [DONE], appending termination marker")
            terminator.mark_done()
            yield b"data: [DONE]\n\n"
        completed = True
    finally:
        # 客户端提前断开时也会走到这里：先释放上游，再落一条统计便于排查
        await terminator.release()
        await _finish_stream_audit(audit_ctx, monitor, completed, keepalive, terminator.done_seen)


async def _convert_responses_sse(upstream_stream, adapter, stream_context,
                                audit_ctx: Optional[AuditContext] = None, keepalive: Any = None):
    """
    Responses API 专用：标准化 + Chat→Responses 协议转换。

    显式绑定请求上下文，避免 StreamingResponse 在新协程执行时继承到其他请求的 ContextVar。
    """
    context_token = adapter.activate_context(stream_context)
    parser = SSEEventParser(normalize=True)
    monitor = StreamPerformanceMonitor.from_stream(upstream_stream)
    terminator = StreamTerminator(upstream_stream)
    chunk_count = 0
    completed = False
    try:
        # 流刚开始即下发打开事件（queued/created/in_progress），消除首 token 前的静默
        opening = adapter.build_stream_open_events()
        if opening:
            yield opening.encode("utf-8")

        while True:
            chunk = await terminator.next_chunk()
            if chunk is None:
                # 上游真的结束，或 [DONE] 后的收尾窗口到期：都算转发走完
                break
            chunk_count += 1
            if not chunk:
                continue
            try:
                events = parser.feed(chunk)
                for event in events:
                    if not event.strip():
                        continue
                    monitor.observe(event)
                    # 终止标记要在交给适配器改写之前识别（改写后事件已被拆成多条）
                    if is_done_event(event):
                        terminator.mark_done()
                    for line in event.split("\n"):
                        stripped = line.strip()
                        if stripped.lower().startswith("data:"):
                            converted = adapter.convert_stream_event(stripped)
                            if converted:
                                logger.debug(f"[{chunk_count}] Converted: {converted[:150]}...")
                                yield converted.encode("utf-8")
            except Exception as e:
                logger.error(f"Stream conversion error at chunk {chunk_count}: {e}", exc_info=True)
                try:
                    logger.error(f"Problematic chunk preview: {chunk[:200]}")
                except Exception:
                    pass
        # 循环正常走完：Responses 链路总会把终止事件交给客户端
        # （上游的 [DONE]，或上层在流尾补发的 response.completed + [DONE]）
        terminator.mark_done()
        completed = True
    finally:
        # 客户端提前断开时也会走到这里：先释放上游，再落审计
        await terminator.release()
        await _finish_stream_audit(audit_ctx, monitor, completed, keepalive, terminator.done_seen)
        adapter.reset_context(context_token)


class OpenAIProxyService:
    """AI Model Gateway 主服务类 - 负责FastAPI应用和路由"""

    def __init__(self, config_file: str = "models.yaml"):
        self.config_loader = ConfigLoader(config_file)
        # 注意：models 将在异步初始化方法中加载
        self.models: Dict[str, List[ModelConfig]] = {}
        self.failover_manager: ModelFailoverManager = None  # type: ignore[assignment]
        self.responses_adapter: ResponsesAdapter = None  # type: ignore[assignment]
        self.session_store: DualModeSessionStore = None  # type: ignore[assignment]

        cache_ttl = int(os.getenv("MODELS_API_CACHE_TTL", "300"))
        self.model_list_cache = MemoryCache(default_ttl=cache_ttl)

        # 审计与用量统计：库与后台刷盘任务在 initialize() 里打开
        self.audit = AuditManager(metrics=load_metrics_collector())
        # 透传路由：配置在 initialize()/reload 时灌入，请求热路径只读当前元组
        self.passthrough = PassthroughManager(self.audit)
        # 用户级密钥：库在 initialize() 里打开，create_app() 时绑定到中间件与路由
        self.user_keys: Optional[UserKeyManager] = None
        # 插件结构快照：reload 时据此判断「进程内无法生效，需要重启」
        self._platform_snapshot: Dict[str, Any] = {}
        # 主事件循环：配置接口在工作线程里触发热重载时需要把协程投回这个循环
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None
        # 配置编辑服务：接口与 CLI 共用同一套补丁
        self._config_service: Optional[ConfigCommandService] = None

    async def _resolve_api_user(self, request: Request) -> str:
        """
        解析数据面这次请求的审计用户名

        规则：
        - 用户密钥：忽略 X-Gateway-User，按密钥自己的用户名记账；
        - 管理密钥没有 header：记 admin；
        - 管理密钥带 header：用户不存在时自动创建，创建失败直接返回错误，不请求上游；
        - 未启用鉴权：忽略 header，记 admin。
        """
        caller = getattr(request.state, "caller", None)
        if caller is None:
            return ADMIN_NAME
        if not caller.is_admin:
            return caller.name

        name = (request.headers.get(USER_HEADER, "") or "").strip()
        if not name:
            return caller.name
        if self.user_keys is None:
            raise HTTPException(status_code=503, detail=f"{USER_HEADER} 无法解析：用户密钥库未就绪")

        try:
            return await self.user_keys.ensure_user_async(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"{USER_HEADER} 不合法: {exc}") from exc
        except (UserKeyStoreUnavailable, KeyEncryptionUnavailable, KeyGenerationFailed) as exc:
            raise HTTPException(
                status_code=503,
                detail=f"{USER_HEADER} 自动创建用户失败: {exc}",
            ) from exc
        except KeyDecryptionFailed as exc:
            raise HTTPException(status_code=500, detail=f"{USER_HEADER} 处理失败: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - 自动建用户失败不能继续请求上游
            logger.warning(f"{USER_HEADER} 自动创建用户失败: {exc}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"{USER_HEADER} 自动创建用户失败: {exc}",
            ) from exc

    def build_config_service(self) -> ConfigCommandService:
        """
        配置编辑服务（懒创建）

        写盘成功后尝试进程内热重载；插件/爬虫结构变化会标记 needs_restart，
        由用户自行重启（服务不会自行重启）。
        """
        if self._config_service is None:
            self._config_service = ConfigCommandService(
                config_file=self.config_loader.config_file,
                backup_dir=os.getenv("CONFIG_BACKUP_DIR", "data/config_backups"),
                reload_callback=self._reload_models_blocking,
                audit_recorder=lambda op, detail="", success=True: self.audit.record_config_change(op, detail, success),
            )
        return self._config_service

    def _reload_models_blocking(self) -> Dict[str, Any]:
        """在工作线程里同步等待进程内重载完成（协程必须回到它自己的事件循环执行）"""
        if self._main_loop is None or self._main_loop.is_closed():
            return {"reloaded": False, "needs_restart": True, "message": "服务未启动，配置将在下次启动时生效"}
        future = asyncio.run_coroutine_threadsafe(self.reload_models(), self._main_loop)
        return future.result(timeout=120)

    async def initialize(self):
        """异步初始化服务

        对于使用爬虫模式的插件（如 OpenRouter、NVIDIA）：
        1. 先获取平台列表（不加载模型，避免调用插件）
        2. 启动爬虫并等待完成
        3. 爬虫完成后加载完整的配置（包含模型列表）

        这样只会加载一次配置，避免重复。

        如果设置了 SKIP_PLUGIN_SCRAPER 环境变量，则跳过爬虫步骤，直接使用缓存。
        """
        # 检查是否跳过插件爬虫
        skip_scraper = os.getenv("SKIP_PLUGIN_SCRAPER", "false").lower() == "true"

        # 记录主事件循环：配置接口在工作线程里触发热重载时要投回这个循环
        self._main_loop = asyncio.get_running_loop()

        # 打开用户级密钥库（失败时仅保留管理密钥，不影响转发）
        if self.user_keys is not None:
            await self.user_keys.start()

        if skip_scraper:
            logger.info("=" * 60)
            logger.info("检测到 SKIP_PLUGIN_SCRAPER=true，跳过插件爬虫，直接使用缓存")
            logger.info("=" * 60)
            # 直接加载完整配置（会使用缓存的模型列表）
            self.models = await self.config_loader.load_config()
        else:
            # 第一步：仅加载平台列表（不加载模型，不会触发插件调用）
            platforms = await self.config_loader.load_platforms_only()

            # 第二步：启动所有插件的定时任务调度器（会等待首次爬虫完成）
            await self._start_plugin_schedulers_for_platforms(platforms)

            # 第三步：爬虫完成后，加载完整配置以获取最新的模型列表
            logger.info("=" * 60)
            logger.info("爬虫任务已完成，加载配置以获取最新模型列表")
            logger.info("=" * 60)
            self.models = await self.config_loader.load_config()

        # 第四步：初始化故障转移管理器
        self.failover_manager = ModelFailoverManager(
            self.models,
            all_aliases=self.config_loader.all_aliases
        )

        # 第五步：初始化 Responses API 适配器和会话存储
        self.session_store = DualModeSessionStore()
        self.responses_adapter = ResponsesAdapter(session_store=self.session_store)

        # 第六步：打开审计（配置来自 models.yaml 顶层 audit: 段）
        self.audit.apply_yaml(self.config_loader.audit_config)
        # 第七步：加载透传路由（配置来自顶层 passthrough: 段）
        self.passthrough.configure(self.config_loader.passthrough_config)
        await self.audit.start()

    async def _start_plugin_schedulers_for_platforms(self, platforms: Dict[str, Any]):
        """
        为指定平台启动插件调度器

        Args:
            platforms: 平台配置字典，键为平台名称，值为平台配置
        """
        try:
            logger.info(f"开始启动插件调度器，平台列表: {list(platforms.keys())}")
            # 遍历所有平台，查找并启动插件调度器
            for platform_name, platform_config in platforms.items():
                if not isinstance(platform_config, dict):
                    logger.info(f"平台 [{platform_name}] 配置不是字典，跳过")
                    continue

                logger.info(f"检查平台 [{platform_name}] 的插件...")
                # 尝试从插件管理器获取插件实例
                plugin = self.config_loader.plugin_manager.get_plugin(platform_name)
                logger.info(f"平台 [{platform_name}] 获取到的插件: {plugin}")

                if plugin and hasattr(plugin, 'start_scheduler'):
                    logger.info(f"正在启动平台 [{platform_name}] 的插件调度器...")
                    try:
                        # 等待初始爬虫任务完成，确保使用最新的模型数据
                        await plugin.start_scheduler(wait_for_initial=True)
                        logger.info(f"✓ 平台 [{platform_name}] 的插件调度器已启动")
                    except Exception as e:
                        logger.error(f"✗ 启动平台 [{platform_name}] 的插件调度器失败: {e}", exc_info=True)
                else:
                    if not plugin:
                        logger.warning(f"平台 [{platform_name}] 没有关联的插件实例")
                    else:
                        logger.warning(f"平台 [{platform_name}] 的插件没有 start_scheduler 方法")
        except Exception as e:
            logger.error(f"启动插件调度器时出错: {e}", exc_info=True)

    async def _start_plugin_schedulers(self):
        """启动所有插件的定时任务调度器"""
        try:
            # 遍历所有平台，查找并启动插件调度器
            for platform_name, model_configs in self.models.items():
                # 即使模型列表为空，也应该尝试启动插件调度器（让定时任务去填充模型）

                # 获取该平台的第一个模型配置（所有模型共享同一个插件）
                first_model = model_configs[0] if model_configs else None

                # 尝试从插件管理器获取插件实例
                plugin = self.config_loader.plugin_manager.get_plugin(platform_name)
                if plugin and hasattr(plugin, 'start_scheduler'):
                    logger.info(f"正在启动平台 [{platform_name}] 的插件调度器...")
                    try:
                        # 等待初始爬虫任务完成，确保使用最新的模型数据
                        await plugin.start_scheduler(wait_for_initial=True)
                        logger.info(f"✓ 平台 [{platform_name}] 的插件调度器已启动")
                    except Exception as e:
                        logger.error(f"✗ 启动平台 [{platform_name}] 的插件调度器失败: {e}")
                else:
                    if not plugin:
                        logger.info(f"平台 [{platform_name}] 没有关联的插件")
        except Exception as e:
            logger.error(f"启动插件调度器时出错: {e}")

    async def close(self):
        """关闭服务"""
        # 停止所有插件的定时任务调度器
        await self._stop_plugin_schedulers()
        if self.failover_manager:
            await self.failover_manager.close()
        if self.session_store:
            await self.session_store.close()
        # 关闭用户级密钥库
        if self.user_keys is not None:
            await self.user_keys.close()
        # 关闭透传共享会话
        await self.passthrough.close()
        # 排空审计缓冲并关闭库
        await self.audit.close()

    async def reload_models(self) -> Dict[str, Any]:
        """
        进程内重载模型配置：只重建模型表与别名，不重启插件调度器

        aiohttp 会话与进行中的流保持不变（流各自持有已取出的对象），
        因此模型级/平台级参数立即生效；插件结构（爬虫参数）变化需要用户自行重启。

        Returns:
            {"reloaded": bool, "needs_restart": bool, "message": str}
        """
        try:
            platforms = await self.config_loader.load_platforms_only()
            snapshot = {
                name: (cfg.get("plugin") if isinstance(cfg, dict) else None)
                for name, cfg in platforms.items()
            }
            needs_restart = bool(self._platform_snapshot) and snapshot != self._platform_snapshot
            self._platform_snapshot = snapshot

            self.models = await self.config_loader.load_config()
            # 复用同一个故障转移管理器（保住 aiohttp 会话），只替换模型表
            self.failover_manager.models = self.models
            self.failover_manager.all_aliases = self.config_loader.all_aliases
            # 透传路由整体替换；进行中的请求各自持有旧配置
            self.passthrough.configure(self.config_loader.passthrough_config)

            await self.model_list_cache.clear()

            message = "模型配置已重载" + ("；插件结构有变化，重启服务后完全生效" if needs_restart else "")
            logger.info(message)
            return {"reloaded": True, "needs_restart": needs_restart, "message": message}
        except Exception as exc:  # noqa: BLE001 - 重载失败时保留旧配置继续服务
            logger.error(f"重载模型配置失败，继续使用旧配置: {exc}", exc_info=True)
            return {"reloaded": False, "needs_restart": True, "message": f"重载失败: {exc}"}

    async def _stop_plugin_schedulers(self):
        """停止所有插件的定时任务调度器"""
        try:
            for platform_name, model_configs in self.models.items():
                if not model_configs:
                    continue

                plugin = self.config_loader.plugin_manager.get_plugin(platform_name)
                if plugin and hasattr(plugin, 'stop_scheduler'):
                    logger.info(f"正在停止平台 [{platform_name}] 的插件调度器...")
                    try:
                        await plugin.stop_scheduler()
                        logger.info(f"✓ 平台 [{platform_name}] 的插件调度器已停止")
                    except Exception as e:
                        logger.error(f"✗ 停止平台 [{platform_name}] 的插件调度器失败: {e}")
        except Exception as e:
            logger.error(f"停止插件调度器时出错: {e}")

    def _mount_admin_ui(self, app: FastAPI) -> None:
        """挂载管理台前端（纯静态文件，不需要构建步骤）

        前端只是 /admin/* 接口的另一个客户端：静态壳本身不含任何敏感数据，
        因此允许无密钥加载；它拿到的数据仍然一律要求 GATEWAY_API_KEY。
        只暴露转发接口的部署可以用 ADMIN_UI_ENABLED=false 整体关闭。
        """
        if os.getenv("ADMIN_UI_ENABLED", "true").strip().lower() != "true":
            logger.info("管理台前端已关闭（ADMIN_UI_ENABLED=false）")
            return

        web_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web"))
        if not os.path.isdir(web_dir):
            logger.warning(f"管理台前端目录不存在，已跳过挂载: {web_dir}")
            return

        app.mount("/admin/ui", AdminUiStatic(html=True, directory=web_dir))
        logger.info(f"管理台前端已挂载: /admin/ui -> {web_dir}")

    def create_app(self) -> FastAPI:
        """创建FastAPI应用"""

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            """应用生命周期管理器"""
            # 启动事件
            logger.info("AI Model Gateway 启动中...")
            logger.info(f"正在加载配置文件: {self.config_loader.config_file}")

            # 初始化服务（加载配置和插件）
            await self.initialize()

            logger.info("AI Model Gateway 启动完成")
            yield
            # 关闭事件
            await self.close()
            logger.info("AI Model Gateway 已关闭")

        app = FastAPI(title="AI Model Gateway", version="1.0.0", lifespan=lifespan)
        install_error_handlers(app)

        gateway_api_keys = UserKeyManager.from_env()
        self.user_keys = gateway_api_keys
        # 透传接口复用同一套用户解析（管理密钥 + X-Gateway-User）
        self.passthrough.user_resolver = self._resolve_api_user
        # 管理端路径：审计与配置接口。未配置 GATEWAY_API_KEY 时一律拒绝，避免用量与配置对外裸露
        ADMIN_PATHS = ("/admin", "/metrics")
        # 前端静态壳不含敏感数据，允许无鉴权加载（数据接口仍需密钥）
        UI_PATH = "/admin/ui"

        def _is_ui_asset(path: str) -> bool:
            return path == UI_PATH or path.startswith(UI_PATH + "/")

        @app.middleware("http")
        async def auth_middleware(request: Request, call_next):
            path = request.url.path
            if _is_ui_asset(path):
                return await call_next(request)

            is_admin = path.startswith(ADMIN_PATHS)
            if is_admin and not gateway_api_keys.admin_enabled:
                return error_response(
                    403,
                    "admin_disabled",
                    "管理接口需要配置 GATEWAY_API_KEY",
                )

            # 调用方身份：管理密钥 → admin，用户密钥 → 对应标签；未启用鉴权时为匿名
            caller = None
            if gateway_api_keys.enabled and path != "/health":
                auth_header = request.headers.get("Authorization", "")
                token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
                caller = gateway_api_keys.resolve(token)
                if caller is None:
                    if is_forward_path(path):
                        # 转发协议接口保持原来的鉴权错误形状
                        return JSONResponse(
                            status_code=401,
                            content={"error": {"message": "Invalid or missing API key",
                                               "type": "auth_error", "code": "unauthorized"}},
                        )
                    return error_response(401, "unauthorized", "Invalid or missing API key")

            # 用户级密钥只能走数据面，不能读管理数据
            if is_admin and caller is not None and not caller.is_admin:
                return error_response(403, "forbidden", "该密钥没有管理权限")

            request.state.caller = caller
            return await call_next(request)

        # 审计与用量接口
        app.include_router(build_audit_router(lambda: self.audit, lambda: self.passthrough.describe()))

        # 用户级密钥管理（新增/删除；只接受管理密钥）
        app.include_router(build_user_router(lambda: self.user_keys))

        # 配置管理接口（可用 CONFIG_API_ENABLED=false 整体关闭）
        if os.getenv("CONFIG_API_ENABLED", "true").lower() == "true":
            app.include_router(build_config_router(self.build_config_service, lambda: self.models))

        self._mount_admin_ui(app)

        @app.post("/v1/chat/completions")
        async def chat_completions(request: Request):
            """
            OpenAI兼容的聊天完成接口 - 支持完全参数透传
            """

            try:
                request_data = await request.json()
                # 打印请求体到日志，方便调试
                logger.debug("=" * 80)
                logger.debug("请求体内容:")
                logger.debug(json.dumps(sanitize_payload(request_data), indent=2, ensure_ascii=False))
                logger.debug("=" * 80)

            except Exception as e:

                raise HTTPException(status_code=400, detail=f"无效的JSON请求体: {str(e)}")

            # 验证必要参数
            if not request_data.get("messages"):

                raise HTTPException(status_code=400, detail="messages参数是必需的")

            # 审计用户：管理密钥可带 X-Gateway-User；需要自动建用户时在这里完成
            api_user = await self._resolve_api_user(request)

            # 审计上下文：贯穿整条故障转移链
            is_stream = bool(request_data.get("stream", False))
            audit_ctx = self.audit.new_context(
                endpoint="chat_completions",
                selector=str(request_data.get("model", "all")),
                stream=is_stream,
                msg_count=len(request_data.get("messages") or []),
                api_user=api_user,
            )

            if is_stream:
                # 流式响应处理
                async def stream_generator():
                    # 心跳计数：等待上游流对象阶段 + 上游首 token 静默阶段共用一个计数器
                    heartbeat = HeartbeatCounter()
                    try:
                        # 等待上游返回流对象的静默期（连接建立/上游排队/首块等待）同样下发心跳，
                        # 否则客户端在这段时间收不到任何字节，无法区分「仍在处理」与「已卡死」
                        waiter = UpstreamStartHeartbeat(
                            self.failover_manager.chat_completion_stream(request_data, audit_ctx),
                            counter=heartbeat,
                        )
                        async for data in waiter:
                            yield data
                        audit_ctx.record_keepalive(heartbeat.count)
                        result = waiter.result
                        if hasattr(result, '__aiter__'):
                            logger.info("SSE normalization enabled for /v1/chat/completions")
                            # 包裹心跳：上游首 token 前静默期下发 SSE 注释，避免客户端等待空白
                            forwarder = _forward_normalized_sse(result, audit_ctx, heartbeat)
                            async for data in KeepAliveStream(forwarder, counter=heartbeat):
                                yield data
                        else:
                            # 如果返回的是普通响应但请求是流式的，转换为流式格式
                            try:
                                if isinstance(result, (bytes, bytearray)):
                                    payload = bytes(result)
                                elif isinstance(result, str):
                                    payload = result.encode('utf-8')
                                else:
                                    if hasattr(result, 'text'):
                                        payload = (await result.text()).encode('utf-8')
                                    elif hasattr(result, 'read'):
                                        raw = await result.read()
                                        payload = raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode('utf-8')
                                    else:
                                        payload = json.dumps(result, ensure_ascii=False).encode('utf-8')
                            except Exception as e:
                                logger.error(f"Stream fallback serialization error: {e}", exc_info=True)
                                payload = str(result).encode('utf-8')
                            yield payload + b"\n"
                            # 补发 [DONE]（非 SSE 流场景）
                            yield b"data: [DONE]\n\n"
                    except Exception as e:
                        error_response = {
                            "error": {
                                "message": str(e),
                                "type": "proxy_error",
                                "param": None,
                                "code": "proxy_error"
                            }
                        }
                        yield json.dumps(error_response).encode() + b"\n"

                return StreamingResponse(stream_generator(), media_type="text/event-stream")  # type: ignore[arg-type]
            else:
                # 普通响应

                result = await self.failover_manager.chat_completion_non_stream(request_data, audit_ctx)

                return result

        @app.post("/v1/responses")
        async def responses_create(request: Request):
            """
            OpenAI Responses API 兼容接口 - 自动转换为 Chat Completions API
            """
            try:
                responses_payload = await request.json()
                logger.debug("=" * 80)
                logger.debug("📥 [RECEIVED] Responses API Request:")
                logger.debug(json.dumps(sanitize_payload(responses_payload), indent=2, ensure_ascii=False))
                logger.debug("=" * 80)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")

            # 1. 转换请求体 (Responses -> Chat)
            chat_payload, request_id = await self.responses_adapter.convert_request(responses_payload)

            logger.debug("=" * 80)
            logger.debug("🔄 [CONVERTED] Chat Completions Request:")
            logger.debug(json.dumps(sanitize_payload(chat_payload), indent=2, ensure_ascii=False))
            logger.debug("=" * 80)

            is_stream = bool(chat_payload.get("stream", False))

            # 审计用户：与 /v1/chat/completions 同一套规则
            api_user = await self._resolve_api_user(request)

            # 审计上下文：/v1/responses 走的是同一套 failover，endpoint 区分开便于统计
            audit_ctx = self.audit.new_context(
                endpoint="responses",
                selector=str(chat_payload.get("model", "all")),
                stream=is_stream,
                msg_count=len(chat_payload.get("messages") or []),
                api_user=api_user,
            )

            # StreamingResponse 会在独立协程中迭代生成器，必须显式保留本请求的上下文。
            stream_context = self.responses_adapter.context

            if is_stream:
                async def stream_generator():
                    context_token = self.responses_adapter.activate_context(stream_context)
                    heartbeat = HeartbeatCounter()
                    try:
                        # 同 /v1/chat/completions：等待上游流对象的静默期也要下发心跳
                        waiter = UpstreamStartHeartbeat(
                            self.failover_manager.chat_completion_stream(chat_payload, audit_ctx),
                            counter=heartbeat,
                        )
                        async for data in waiter:
                            yield data
                        audit_ctx.record_keepalive(heartbeat.count)
                        upstream_stream = waiter.result
                        # 包裹心跳：思考阶段/首 token 前静默期下发 SSE 注释
                        forwarder = _convert_responses_sse(
                            upstream_stream, self.responses_adapter, stream_context, audit_ctx, heartbeat
                        )
                        async for data in KeepAliveStream(forwarder, counter=heartbeat):
                            yield data
                        # 上游流结束,检查是否已发出完成事件（ModelScope 等不发 [DONE] 的上游）
                        if not self.responses_adapter.context.has_received_done:
                            logger.info("Upstream did not send [DONE], emitting completion events")
                            completion_events = self.responses_adapter._build_completion_events()
                            if completion_events:
                                yield completion_events.encode('utf-8')
                    except Exception as e:
                        logger.error(f"Upstream stream error: {e}", exc_info=True)
                        self.responses_adapter.context.cleanup()
                    finally:
                        self.responses_adapter.reset_context(context_token)

                return StreamingResponse(stream_generator(), media_type="text/event-stream")
            else:
                # 非流式处理逻辑
                chat_response = await self.failover_manager.chat_completion_non_stream(chat_payload, audit_ctx)

                logger.debug("=" * 80)
                logger.debug("📤 [UPSTREAM RESPONSE] Chat Completions Response:")
                logger.debug(json.dumps(sanitize_payload(chat_response), indent=2, ensure_ascii=False))
                logger.debug("=" * 80)

                # 1. 包装响应对象
                response_obj, new_id = self.responses_adapter.build_response_object(chat_response, responses_payload, request_id)

                logger.debug("=" * 80)
                logger.debug("📦 [FINAL] Responses API Response:")
                logger.debug(json.dumps(sanitize_payload(response_obj), indent=2, ensure_ascii=False))
                logger.debug("=" * 80)

                # 2. 更新会话状态 (将当前请求和回复存入 Redis)
                # 提取当前请求的 input 转换为 messages
                current_messages = self.responses_adapter._convert_input_to_messages(responses_payload.get("input", []))
                if responses_payload.get("instructions"):
                    current_messages.insert(0, {"role": "system", "content": responses_payload["instructions"]})

                # 构造完整的对话历史并保存
                history = await self.session_store.get_history(responses_payload.get("previous_response_id", "")) if responses_payload.get("previous_response_id") else []

                # 提取助手响应内容（支持message和function_call/custom_tool_call类型）
                assistant_message: Dict[str, Any] = {"role": "assistant"}
                if response_obj["output"] and len(response_obj["output"]) > 0:
                    tool_calls = []
                    for output_item in response_obj["output"]:
                        if output_item.get("type") == "message" and output_item.get("content"):
                            assistant_message["content"] = output_item["content"][0].get("text", "")
                        elif output_item.get("type") == "function_call":
                            tool_calls.append({
                                "id": output_item.get("call_id", ""),
                                "type": "function",
                                "function": {
                                    "name": output_item.get("name", ""),
                                    "arguments": output_item.get("arguments", "{}")
                                }
                            })
                        elif output_item.get("type") == "custom_tool_call":
                            tool_calls.append({
                                "id": output_item.get("call_id", ""),
                                "type": "function",
                                "function": {
                                    "name": output_item.get("name", ""),
                                    "arguments": output_item.get("input", "")
                                }
                            })
                    if tool_calls:
                        assistant_message["tool_calls"] = tool_calls

                full_history = history + current_messages + [assistant_message]
                # 保存会话时同时传递原始 output 数组
                await self.session_store.save_session(new_id, full_history, original_output=response_obj.get("output"))

                return response_obj

        @app.get("/v1/models")
        async def get_models(request: Request):
            """返回代理当前可用模型列表"""
            try:
                provider = request.query_params.get("provider")
                capability = request.query_params.get("capability")
                q = request.query_params.get("q")
                limit = int(request.query_params.get("limit", 50))
                offset = int(request.query_params.get("offset", 0))
            except ValueError:
                raise HTTPException(status_code=400, detail="limit 和 offset 必须是整数")

            if limit < 0 or offset < 0:
                raise HTTPException(status_code=400, detail="limit 和 offset 必须为非负整数")

            cache_key = json.dumps({
                "provider": provider,
                "capability": capability,
                "q": q,
                "limit": limit,
                "offset": offset
            }, sort_keys=True, ensure_ascii=False)

            cached_response = await self.model_list_cache.get(cache_key)
            if cached_response is not None:
                return cached_response

            try:
                items, total = list_available_models(
                    self.models,
                    provider=provider,
                    capability=capability,
                    q=q,
                    limit=limit,
                    offset=offset,
                    all_aliases=self.config_loader.all_aliases,
                )
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"无法获取模型列表: {str(e)}")

            response = {
                "data": items,
                "meta": {"total": total, "limit": limit, "offset": offset}
            }

            await self.model_list_cache.set(cache_key, response)
            return response

        @app.post("/health")
        @app.get("/health")
        async def health_check():
            """健康检查端点"""
            return {"status": "healthy", "timestamp": datetime.now().isoformat()}

        # 透传兜底路由：只接管 /proxy/ 命名空间，内置路由先匹配
        @app.api_route(
            "/proxy/{full_path:path}",
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        )
        async def passthrough_dispatch(request: Request, full_path: str):
            """未命中内置路由时尝试透传；没有配置对应前缀则 404"""
            response = await self.passthrough.handle(request)
            if response is None:
                raise HTTPException(status_code=404, detail="Not Found")
            return response

        return app
