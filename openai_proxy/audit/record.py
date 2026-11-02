"""
审计记录 - 一次上游模型调用（attempt 级）的结构化审计行

粒度说明：
- 一次客户端请求 = 一个 gateway_request_id，可能对应多条 attempt（故障转移/重试）
- 流式调用先落 stream_open（流已交给上层转发），流结束时再补齐用量并改写结局

outcome 语义（统计口径的地基）：
    stream_open  流已交给上层，尚未收尾（finalized=0，不计入成功率）
    success      非流式完成 / 流已发完（含「[DONE] 已转发后客户端才关闭连接」）
    aborted      客户端在 [DONE] 之前断开，内容确实没发完（done_sent=0）
    failure      本次上游尝试失败

done_sent 是区分「真中途断开」与「发完后被关」的依据：
按 SSE 协议读流的客户端拿到 [DONE]/finish_reason 就关连接去执行工具，此时上游可能还没结束
chunked body；这种情况内容其实已经发完，不能算客户端中断。
"""
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

OUTCOME_STREAM_OPEN = "stream_open"
OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_ABORTED = "aborted"

# 已收尾的结局：成功率等统计只信任这些行
FINAL_OUTCOMES = (OUTCOME_SUCCESS, OUTCOME_FAILURE, OUTCOME_ABORTED)

# 落库列顺序，与 SqliteAuditStore 的建表语句一一对应
COLUMNS = (
    "attempt_key", "gateway_request_id", "request_id", "endpoint", "stream",
    "selector", "platform", "model_name", "model_id", "attempt_seq", "retry_seq",
    "ts", "outcome", "finalized", "http_status", "error_category", "error_code",
    "disabled_model", "duration_ms", "probe_ms", "ttft_ms", "has_usage", "upstream_id",
    "input_tokens", "cached_tokens", "output_tokens", "output_chars", "throughput",
    "tool_call_converted", "keepalive_count", "msg_count", "done_sent",
)


def utc_now_iso() -> str:
    """UTC 时间戳（毫秒精度），统计按它分桶"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def build_attempt_key(gateway_request_id: str, attempt_seq: int) -> str:
    """主键：同一请求内的尝试序号，finalize 时按它定位行"""
    return f"{gateway_request_id}-{attempt_seq}"


def to_ms(seconds: Optional[float]) -> Optional[int]:
    """秒转毫秒整数；无效值返回 None（缺失不能用 0 冒充）"""
    if seconds is None:
        return None
    try:
        return int(round(float(seconds) * 1000))
    except (TypeError, ValueError):
        return None


@dataclass
class AuditAttempt:
    """单次上游模型调用的审计行"""

    attempt_key: str
    gateway_request_id: str
    endpoint: str
    stream: bool
    selector: str = ""
    platform: str = ""
    model_name: str = ""
    model_id: str = ""
    attempt_seq: int = 0
    retry_seq: int = 0
    ts: str = field(default_factory=utc_now_iso)
    outcome: str = OUTCOME_FAILURE
    finalized: bool = True
    http_status: Optional[int] = None
    error_category: Optional[str] = None
    error_code: Optional[str] = None
    disabled_model: bool = False
    duration_ms: Optional[int] = None
    probe_ms: Optional[int] = None
    ttft_ms: Optional[int] = None
    has_usage: bool = False
    upstream_id: Optional[str] = None
    input_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    output_chars: Optional[int] = None
    throughput: Optional[float] = None
    tool_call_converted: Optional[int] = None
    keepalive_count: Optional[int] = None
    msg_count: Optional[int] = None
    # 网关是否已把终止标记 [DONE] 转发给客户端：aborted 只有配着 0 才是「内容没发完」
    done_sent: bool = False
    # 计时起点（不落库）：由 begin 时刻写入
    started_at: float = field(default_factory=time.time)

    # ---------- 结论 ----------

    def elapsed_ms(self) -> int:
        """从尝试开始到现在的毫秒数"""
        if not self.started_at:
            return 0
        return int(round((time.time() - self.started_at) * 1000))

    def mark_stream_open(self) -> None:
        """流式：已拿到流并交给上层转发，用量等 finalize 补齐"""
        self.probe_ms = self.elapsed_ms()
        self.duration_ms = self.probe_ms
        self.outcome = OUTCOME_STREAM_OPEN
        self.finalized = False

    def mark_success(self, response: Any = None) -> None:
        """非流式：调用完成，用量一次落齐"""
        self.duration_ms = self.elapsed_ms()
        self.outcome = OUTCOME_SUCCESS
        self.finalized = True
        self.probe_ms = None
        self.apply_response(response)

    def mark_failure(self, error: Any = None, http_status: Optional[int] = None) -> None:
        """本次尝试失败：记录错误分类，便于统计失败原因"""
        self.duration_ms = self.elapsed_ms()
        self.outcome = OUTCOME_FAILURE
        self.finalized = True
        if http_status is None:
            http_status = self._extract_http_status(error)
        self.http_status = http_status
        self.error_category = self._extract_category(error)
        self.error_code = self._extract_code(error)
        self.disabled_model = self._extract_bool(error, "should_disable_model")

    def mark_aborted(self) -> None:
        """客户端在 [DONE] 之前断开：内容没发完，不能算成功，也不能算上游失败"""
        self.duration_ms = self.elapsed_ms()
        self.outcome = OUTCOME_ABORTED
        self.finalized = True

    # ---------- 用量与性能 ----------

    def apply_response(self, response: Any) -> None:
        """从非流式响应体提取 usage（上游没返回 usage 时保持未知，不写 0）"""
        if not isinstance(response, dict):
            return
        from openai_proxy.utils.stream_metrics import summarize_response

        try:
            self.apply_stats(summarize_response(response))
        except Exception:  # noqa: BLE001 - 审计不得影响主流程
            pass

    def apply_stats(self, stats: Optional[Dict[str, Any]]) -> None:
        """合并 StreamPerformanceMonitor.finish() 的统计结果（流结束时调用）"""
        if not isinstance(stats, dict):
            return

        self.ttft_ms = to_ms(stats.get("ttft"))
        total_ms = to_ms(stats.get("total"))
        if total_ms is not None:
            self.duration_ms = total_ms

        if stats.get("has_usage"):
            self.has_usage = True
            self.input_tokens = _int_or_none(stats.get("input_tokens"))
            self.cached_tokens = _int_or_none(stats.get("cached_tokens"))
            self.output_tokens = _int_or_none(stats.get("output_tokens"))

        self.output_chars = _int_or_none(stats.get("output_chars"))
        throughput = stats.get("throughput")
        self.throughput = float(throughput) if isinstance(throughput, (int, float)) else None

        upstream_id = stats.get("upstream_id")
        if isinstance(upstream_id, str) and upstream_id:
            self.upstream_id = upstream_id

        keepalive_count = stats.get("keepalive_count")
        if isinstance(keepalive_count, int):
            self.keepalive_count = keepalive_count

    # ---------- 落库 ----------

    def to_row(self) -> Dict[str, Any]:
        """转换为落库字典（bool 转 0/1，SQLite 不认 bool）"""
        row: Dict[str, Any] = {}
        for column in COLUMNS:
            value = getattr(self, column, None)
            if isinstance(value, bool):
                value = 1 if value else 0
            row[column] = value
        return row

    # ---------- 错误字段提取（鸭子类型，不依赖 error_classifier） ----------

    @staticmethod
    def _extract_category(error: Any) -> Optional[str]:
        """取 ClassifiedError.category.value"""
        category = getattr(error, "category", None)
        value = getattr(category, "value", category)
        return str(value) if value else None

    @staticmethod
    def _extract_code(error: Any) -> Optional[str]:
        """取错误码：优先 metadata.code，其次分类名，最后异常类型名"""
        metadata = getattr(error, "metadata", None)
        if isinstance(metadata, dict):
            for key in ("code", "error_code", "upstream_code"):
                value = metadata.get(key)
                if isinstance(value, (str, int)) and str(value).strip():
                    return str(value)[:64]
        category = getattr(error, "category", None)
        value = getattr(category, "value", category)
        if value:
            return str(value)[:64]
        if error is not None and not isinstance(error, str):
            return type(error).__name__[:64]
        return None

    @staticmethod
    def _extract_http_status(error: Any) -> Optional[int]:
        """取上游 HTTP 状态码（错误分类器的 metadata 里带着）"""
        metadata = getattr(error, "metadata", None)
        if isinstance(metadata, dict):
            status = metadata.get("http_status") or metadata.get("status_code")
            if isinstance(status, int):
                return status
        return None

    @staticmethod
    def _extract_bool(error: Any, attribute: str) -> bool:
        """读取 ClassifiedError 上的布尔策略标记"""
        return bool(getattr(error, attribute, False))


def _int_or_none(value: Any) -> Optional[int]:
    """整数取值，非法值返回 None（避免把「未知」写成 0）"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None
