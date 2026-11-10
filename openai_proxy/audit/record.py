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
import json
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

# 标准维度键：聊天链路与透传链路共用同一套语义
# 存储上只有 dimensions 一份真相，platform/model/... 是 SQLite 生成列
DIM_PLATFORM = "platform"
DIM_MODEL = "model"
DIM_MODEL_ID = "model_id"
DIM_SELECTOR = "selector"
STANDARD_DIMENSIONS = (DIM_PLATFORM, DIM_MODEL, DIM_MODEL_ID, DIM_SELECTOR)

# 落库列顺序，与 SqliteAuditStore 的建表语句一一对应
# 标准维度不再单独写入，统一由 dimensions 派生
COLUMNS = (
    "attempt_key", "gateway_request_id", "api_user", "request_id", "endpoint", "stream",
    "attempt_seq", "retry_seq", "ts", "outcome", "finalized", "http_status",
    "error_category", "error_code", "disabled_model", "duration_ms", "probe_ms",
    "ttft_ms", "has_usage", "upstream_id", "input_tokens", "cached_tokens",
    "output_tokens", "output_chars", "throughput", "tool_call_converted",
    "keepalive_count", "msg_count", "done_sent", "dimensions",
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
    # 调用方标签：来自用户级密钥的名称；未启用鉴权时为空，查询时按 admin 展示
    api_user: str = ""
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
    # 业务维度快照：唯一可扩展的分组概念，标准键见 STANDARD_DIMENSIONS
    dimensions: Dict[str, str] = field(default_factory=dict)
    # 计时起点（不落库）：由 begin 时刻写入
    started_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        """把构造时传入的标准字段合并进 dimensions（保持旧调用方式可用）"""
        normalized = _normalize_dimensions(self.dimensions)
        for key, value in (
            (DIM_PLATFORM, self.platform),
            (DIM_MODEL, self.model_name),
            (DIM_MODEL_ID, self.model_id),
            (DIM_SELECTOR, self.selector),
        ):
            if value and not normalized.get(key):
                normalized[key] = str(value)
        self.dimensions = normalized

        # 反向同步：调用方只传 dimensions 时，标准字段也要能看到（指标与旧代码依赖它们）
        if not self.platform:
            self.platform = normalized.get(DIM_PLATFORM, "")
        if not self.model_name:
            self.model_name = normalized.get(DIM_MODEL, "")
        if not self.model_id:
            self.model_id = normalized.get(DIM_MODEL_ID, "")
        if not self.selector:
            self.selector = normalized.get(DIM_SELECTOR, "")

    def dimension(self, name: str) -> str:
        """取一个维度值；不存在时返回空串"""
        return self.dimensions.get(name, "")

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
        self.refresh_throughput()

    # ---------- 用量与性能 ----------

    def refresh_throughput(self) -> None:
        """
        用「输出 tokens / 已记录的耗时」重算 tokens/s

        throughput 是派生指标：只要 duration_ms 或 output_tokens 变了就必须重算。
        耗时缺失或为 0 时保持 None，不能用接近 0 的计时窗口编出一个天文数字。
        """
        if not self.output_tokens or not self.duration_ms or self.duration_ms <= 0:
            self.throughput = None
            return
        self.throughput = self.output_tokens * 1000.0 / self.duration_ms

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
        # throughput 不从 stats 直接抄：非流式提取没有真实计时窗口，
        # 统一用已经落库的 duration_ms 与 output_tokens 重算，避免出现天文数字。
        self.refresh_throughput()

        upstream_id = stats.get("upstream_id")
        if isinstance(upstream_id, str) and upstream_id:
            self.upstream_id = upstream_id

        keepalive_count = stats.get("keepalive_count")
        if isinstance(keepalive_count, int):
            self.keepalive_count = keepalive_count

    # ---------- 落库 ----------

    def to_row(self) -> Dict[str, Any]:
        """转换为落库字典（bool 转 0/1；dimensions 序列化为规范 JSON）"""
        row: Dict[str, Any] = {}
        dimensions = self._storage_dimensions()
        for column in COLUMNS:
            if column == "dimensions":
                value = json.dumps(
                    dimensions, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            else:
                value = getattr(self, column, None)
            if isinstance(value, bool):
                value = 1 if value else 0
            row[column] = value
        return row

    def _storage_dimensions(self) -> Dict[str, str]:
        """落库前再合并一次标准字段，避免调用方后改字段导致不一致"""
        dimensions = dict(self.dimensions)
        for key, value in (
            (DIM_PLATFORM, self.platform),
            (DIM_MODEL, self.model_name),
            (DIM_MODEL_ID, self.model_id),
            (DIM_SELECTOR, self.selector),
        ):
            if value and not dimensions.get(key):
                dimensions[key] = str(value)
        return _normalize_dimensions(dimensions)

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


# 单个维度值的最大长度：防止异常请求把维度基数与存储撑爆
MAX_DIMENSION_VALUE = 200


def _normalize_dimensions(values: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """维度统一成 `字符串键 -> 截断后的字符串值`，空值保留为空串"""
    if not isinstance(values, dict):
        return {}
    normalized: Dict[str, str] = {}
    for key, value in values.items():
        name = str(key).strip()
        if not name:
            continue
        text = _dimension_text(value)
        normalized[name] = text[:MAX_DIMENSION_VALUE]
    return normalized


def _dimension_text(value: Any) -> str:
    """维度值转字符串：标量直出，列表/对象存紧凑 JSON，None 记空串"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)
