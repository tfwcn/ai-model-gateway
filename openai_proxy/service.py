import asyncio
import json
import logging
import os
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Dict, List, Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse

from openai_proxy.core.cache import MemoryCache
from openai_proxy.models import ModelConfig, list_available_models
from openai_proxy.core.config_loader import ConfigLoader
from openai_proxy.model.failover import ModelFailoverManager
from openai_proxy.adapter.responses import ResponsesAdapter
from openai_proxy.utils.session import DualModeSessionStore
from openai_proxy.utils.sse_parser import SSEEventParser
from openai_proxy.utils.logger import sanitize_payload

logger = logging.getLogger(__name__)


async def _forward_normalized_sse(upstream_stream):
    """
    标准化 SSE 流转发（公共逻辑）

    使用 SSEEventParser 按完整事件分割并标准化，自动补发 [DONE]。
    消除 /v1/chat/completions 与 /v1/responses 间的重复代码。
    """
    parser = SSEEventParser(normalize=True)
    done_sent = False
    async for chunk in upstream_stream:
        if not chunk:
            continue
        try:
            events = parser.feed(chunk)
            for event in events:
                if not event.strip():
                    continue
                yield (event + "\n\n").encode("utf-8")
                if "[DONE]" in event:
                    done_sent = True
        except Exception as e:
            logger.error(f"SSE normalization error: {e}", exc_info=True)
            yield chunk  # 降级：透传原始 chunk
    if not done_sent:
        logger.info("Upstream did not send [DONE], appending termination marker")
        yield b"data: [DONE]\n\n"


async def _convert_responses_sse(upstream_stream, adapter):
    """
    Responses API 专用：标准化 + Chat→Responses 协议转换
    """
    parser = SSEEventParser(normalize=True)
    chunk_count = 0
    async for chunk in upstream_stream:
        chunk_count += 1
        if not chunk:
            continue
        try:
            events = parser.feed(chunk)
            for event in events:
                if not event.strip():
                    continue
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

        gateway_api_key = os.getenv("GATEWAY_API_KEY", "")

        @app.middleware("http")
        async def auth_middleware(request: Request, call_next):
            if gateway_api_key and request.url.path != "/health":
                auth_header = request.headers.get("Authorization", "")
                if not auth_header.startswith("Bearer ") or auth_header[7:] != gateway_api_key:
                    return JSONResponse(
                        status_code=401,
                        content={"error": {"message": "Invalid or missing API key", "type": "auth_error", "code": "unauthorized"}}
                    )
            return await call_next(request)

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

            if request_data.get("stream", False):
                # 流式响应处理
                async def stream_generator():
                    try:
                        result = await self.failover_manager.chat_completion_stream(request_data)
                        if hasattr(result, '__aiter__'):
                            logger.info("SSE normalization enabled for /v1/chat/completions")
                            async for data in _forward_normalized_sse(result):
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

                result = await self.failover_manager.chat_completion_non_stream(request_data)

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

            is_stream = chat_payload.get("stream", False)

            if is_stream:
                async def stream_generator():
                    try:
                        upstream_stream = await self.failover_manager.chat_completion_stream(chat_payload)
                        async for data in _convert_responses_sse(upstream_stream, self.responses_adapter):
                            yield data
                        # 上游流结束,检查是否已发出完成事件（ModelScope 等不发 [DONE] 的上游）
                        if self.responses_adapter.has_request_context() and not self.responses_adapter.context.has_received_done:
                            logger.info("Upstream did not send [DONE], emitting completion events")
                            completion_events = self.responses_adapter._build_completion_events()
                            if completion_events:
                                yield completion_events.encode('utf-8')
                    except Exception as e:
                        logger.error(f"Upstream stream error: {e}", exc_info=True)
                        if self.responses_adapter.has_request_context():
                            self.responses_adapter.context.cleanup()

                return StreamingResponse(stream_generator(), media_type="text/event-stream")
            else:
                # 非流式处理逻辑
                chat_response = await self.failover_manager.chat_completion_non_stream(chat_payload)

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

        @app.get("/health")
        async def health_check():
            """健康检查端点"""
            return {"status": "healthy", "timestamp": datetime.now().isoformat()}

        return app
