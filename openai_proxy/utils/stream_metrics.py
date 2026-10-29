"""
模型调用的输出速度埋点

记录单次模型调用的耗时与 token 用量，输出 tokens/s，用于分析「token 输出速度」是否变慢。

设计要点：
- 与业务解耦：本模块只做统计和打点，不参与请求/响应处理
- 上游字段兼容：usage 同时支持 chat/completions（completion_tokens）
  与 responses（output_tokens）两种命名
- 流式调用用 observe() 逐个事件喂入，非流式调用用 record_response() 喂入完整响应体
"""
import json
import logging
import os
import time
from typing import Any, Dict, Optional, Sequence

logger = logging.getLogger(__name__)

# 首块耗时告警阈值（秒）：超过即单独打一条 WARNING，便于检索慢请求
SLOW_FIRST_CHUNK_WARNING = float(os.getenv("SLOW_FIRST_CHUNK_WARNING", "15"))

# 输出/输入 token 字段：不同上游、不同协议的命名不一致
_OUTPUT_TOKEN_KEYS = ("completion_tokens", "output_tokens")
_INPUT_TOKEN_KEYS = ("prompt_tokens", "input_tokens")

# 输入缓存命中字段：OpenAI/Azure 放在 *_tokens_details.cached_tokens，DeepSeek 是扁平字段
_CACHE_HIT_DETAIL_KEYS = ("prompt_tokens_details", "input_tokens_details")
_CACHE_HIT_KEYS = ("prompt_cache_hit_tokens", "cache_read_input_tokens")

# 增量文本字段：usage 缺失时用输出字符数兜底，便于事后估算
_TEXT_KEYS = ("content", "reasoning_content", "text")
_TOOL_ARGUMENT_KEYS = ("arguments",)


def extract_usage(payload: Any) -> Optional[Dict[str, Any]]:
    """从响应体或流式事件中提取 usage，兼容 responses 协议的多层嵌套"""
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if isinstance(usage, dict):
        return usage
    response = payload.get("response")
    if isinstance(response, dict) and isinstance(response.get("usage"), dict):
        return response["usage"]
    return None


def _pick_int(usage: Dict[str, Any], keys: Sequence[str]) -> int:
    """按候选字段名依次取值，返回第一个有效整数，都没有则返回 0"""
    for key in keys:
        value = usage.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def extract_cached_tokens(usage: Dict[str, Any]) -> int:
    """取输入缓存命中的 token 数，兼容各家字段命名，取不到返回 0"""
    for key in _CACHE_HIT_DETAIL_KEYS:
        details = usage.get(key)
        if isinstance(details, dict) and isinstance(details.get("cached_tokens"), (int, float)):
            return int(details["cached_tokens"])
    return _pick_int(usage, _CACHE_HIT_KEYS)


def _text_length(node: Any, keys: Sequence[str]) -> int:
    """累计节点里候选文本字段的字符数，非字典节点返回 0"""
    if not isinstance(node, dict):
        return 0
    return sum(len(node[key]) for key in keys if isinstance(node.get(key), str))


def _delta_text_length(payload: Dict[str, Any]) -> int:
    """统计一条响应体/事件里增量文本的字符数（usage 缺失时的兜底指标）"""
    total = _text_length(payload, _TEXT_KEYS)
    for choice in payload.get("choices") or []:
        delta = choice.get("delta")
        total += _text_length(delta, _TEXT_KEYS)
        total += _text_length(choice.get("message"), _TEXT_KEYS)
        # 工具调用参数同样属于输出内容（流式在 delta，非流式在 message）
        for node in (delta, choice.get("message")):
            for tool_call in (node if isinstance(node, dict) else {}).get("tool_calls") or []:
                total += _text_length(tool_call.get("function"), _TOOL_ARGUMENT_KEYS)
    return total


class StreamPerformanceMonitor:
    """单次模型调用的性能埋点：首块耗时、总耗时、token 用量与输出速度"""

    def __init__(self, model_name: str = "unknown", request_id: str = "", started_at: Optional[float] = None):
        self.model_name = model_name
        self.request_id = request_id
        self.started_at = started_at if started_at is not None else time.time()
        self.first_chunk_at: Optional[float] = None
        self.upstream_id = ""
        self.has_usage = False
        self.input_tokens = 0
        self.cached_tokens = 0
        self.output_tokens = 0
        self.output_chars = 0

    @classmethod
    def from_stream(cls, upstream_stream: Any) -> "StreamPerformanceMonitor":
        """读取 failover 层写入流对象的埋点元信息，缺失时使用默认值"""
        return cls(
            model_name=getattr(upstream_stream, "model_name", "unknown"),
            request_id=getattr(upstream_stream, "request_id", ""),
            started_at=getattr(upstream_stream, "started_at", None),
        )

    def observe(self, event: str) -> None:
        """喂入一个 SSE 事件（形如 "data: {...}"），累积 token 与字符统计

        埋点位于响应热路径上，任何解析异常都只降级为「不统计」，不影响转发。
        """
        if self.first_chunk_at is None:
            self.first_chunk_at = time.time()
        try:
            payload = self._parse_event(event)
            if payload is not None:
                self._collect(payload)
        except Exception as e:  # noqa: BLE001 - 埋点不得影响主流程
            logger.debug(f"输出速度埋点解析事件失败，已跳过: {e}")

    def record_response(self, response: Any) -> None:
        """喂入完整响应体（非流式场景）

        非流式埋点在成功返回前调用，异常不能外抛，否则会把成功响应变成报错。
        """
        try:
            if isinstance(response, dict):
                self._collect(response)
        except Exception as e:  # noqa: BLE001 - 埋点不得影响主流程
            logger.debug(f"输出速度埋点解析响应体失败，已跳过: {e}")

    def finish(self) -> Dict[str, Any]:
        """输出统计日志并返回统计结果

        该方法是流式生成器的 finally 中被调用的，异常不能外抛，否则会破坏响应收尾。
        """
        try:
            finished_at = time.time()
            total = finished_at - self.started_at
            ttft = (self.first_chunk_at - self.started_at) if self.first_chunk_at else None
            stats = {
                "model": self.model_name,
                "request_id": self.request_id,
                "upstream_id": self.upstream_id,
                "has_usage": self.has_usage,
                "ttft": ttft,
                "total": total,
                "input_tokens": self.input_tokens,
                "cached_tokens": self.cached_tokens,
                "output_tokens": self.output_tokens,
                "output_chars": self.output_chars,
                # 端到端速度：含首块等待，即客户端感知的平均 token 输出速度
                "throughput": self._ratio(self.output_tokens, total),
            }
            logger.info(self._format(stats))
            self._warn_slow_first_chunk(stats)
            return stats
        except Exception as e:  # noqa: BLE001 - 埋点不得影响主流程
            logger.warning(f"输出速度埋点汇总失败: {e}", exc_info=True)
            return {}

    @staticmethod
    def _warn_slow_first_chunk(stats: Dict[str, Any]) -> None:
        """首块耗时超阈值时单独告警，便于快速定位链路/上游慢请求"""
        ttft = stats.get("ttft")
        if not ttft or ttft < SLOW_FIRST_CHUNK_WARNING:
            return
        logger.warning(
            f"首块耗时过长: 模型={stats['model']} 请求={stats['request_id'] or '-'} "
            f"上游id={stats['upstream_id'] or '-'} 首块={ttft:.2f}s "
            f"阈值={SLOW_FIRST_CHUNK_WARNING:.0f}s 总耗时={stats['total']:.2f}s"
        )

    def _collect(self, payload: Dict[str, Any]) -> None:
        """从单条响应体/事件中累积 token 用量与输出字符数"""
        self.output_chars += _delta_text_length(payload)
        # 上游 id：部分供应商不返回或返回非字符串，取到合法的才更新
        upstream_id = payload.get("id")
        if isinstance(upstream_id, str) and upstream_id:
            self.upstream_id = upstream_id
        usage = extract_usage(payload)
        if usage is None:
            return
        self.has_usage = True
        # 流式场景 usage 只在末块出现，取到有效值后即保留
        self.input_tokens = _pick_int(usage, _INPUT_TOKEN_KEYS) or self.input_tokens
        self.cached_tokens = extract_cached_tokens(usage) or self.cached_tokens
        self.output_tokens = _pick_int(usage, _OUTPUT_TOKEN_KEYS) or self.output_tokens

    @staticmethod
    def _parse_event(event: str) -> Optional[Dict[str, Any]]:
        """解析 SSE 事件中的 JSON 数据，非 data 行或 [DONE] 返回 None"""
        for line in event.split("\n"):
            line = line.strip()
            if not line.lower().startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    @staticmethod
    def _ratio(tokens: int, seconds: Optional[float]) -> Optional[float]:
        """计算 tokens/s，参数无效（无 token 或无非正耗时长）时返回 None"""
        if tokens <= 0 or not seconds or seconds <= 0:
            return None
        return tokens / seconds

    @staticmethod
    def _format(stats: Dict[str, Any]) -> str:
        """格式化为 key=value 的日志行（固定前缀便于检索），缺失值统一用 - 占位"""
        return (
            f"输出速度统计: 模型={stats['model']} 请求={stats['request_id'] or '-'} "
            f"上游id={stats['upstream_id'] or '-'} "
            f"首块={_format_seconds(stats['ttft'])} 总耗时={stats['total']:.2f}s "
            f"输入tokens={_format_tokens(stats['input_tokens'], stats['has_usage'])} "
            f"输入缓存命中={_format_tokens(stats['cached_tokens'], stats['has_usage'])} "
            f"输出tokens={_format_tokens(stats['output_tokens'], stats['has_usage'])} "
            f"输出字符数={stats['output_chars']} "
            f"端到端速度={_format_speed(stats['throughput'])}"
        )


def _format_seconds(value: Optional[float]) -> str:
    """格式化秒数字段，无有效值时返回占位符"""
    return f"{value:.2f}s" if value is not None else "-"


def _format_tokens(value: int, known: bool) -> str:
    """格式化 token 字段：上游没返回 usage 时属于未知，不能用 0 冒充"""
    return str(value) if known else "-"


def _format_speed(value: Optional[float]) -> str:
    """格式化速度字段，无有效值时返回占位符"""
    return f"{value:.2f}tokens/s" if value else "-"


def report_non_stream_usage(model_name: str, started_at: float, response: Any, request_id: str = "") -> Dict[str, Any]:
    """非流式调用的一键埋点：直接输出该次调用的 token 输出速度统计"""
    monitor = StreamPerformanceMonitor(model_name=model_name, request_id=request_id, started_at=started_at)
    monitor.record_response(response)
    return monitor.finish()
