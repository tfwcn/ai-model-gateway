"""
透传审计 - 把一次透传请求记成一条 attempt

口径与模型调用保持一致：
- 响应开始转发时先落 stream_open，让长响应/进程被杀的请求也留有痕迹
- 转发结束后按「是否完整发完 + 上游状态码」收尾
- 透传不重试、不故障转移，所以一个请求只有一条 attempt
"""
import logging
import time
import uuid
from typing import Any, Dict, Optional

from openai_proxy.audit.record import AuditAttempt, OUTCOME_FAILURE, OUTCOME_SUCCESS
from openai_proxy.passthrough.config import PassthroughRoute

logger = logging.getLogger(__name__)


class PassthroughAuditRecorder:
    """一次透传请求的审计行"""

    def __init__(
        self,
        manager: Any,
        route: PassthroughRoute,
        api_user: str,
        dimensions: Dict[str, str],
        stream: bool,
    ):
        self.manager = manager
        self.route = route
        self.request_id = uuid.uuid4().hex[:8]
        self.started_at = time.time()
        self.first_byte_sent = False
        self.bytes_sent = 0
        self.stream = stream

        merged = route.default_dimensions()
        merged.update({key: value for key, value in dimensions.items() if value != "" or key not in merged})
        self.attempt = AuditAttempt(
            attempt_key=f"passthrough-{self.request_id}-1",
            gateway_request_id=self.request_id,
            endpoint=route.endpoint,
            stream=stream,
            api_user=api_user,
            dimensions=merged,
        )

    @property
    def attempt_key(self) -> str:
        return self.attempt.attempt_key

    def start(self, http_status: Optional[int], probe_ms: Optional[int] = None) -> None:
        """拿到上游响应头，开始向客户端转发"""
        self.attempt.http_status = http_status
        if probe_ms is not None:
            self.attempt.probe_ms = probe_ms
        self.attempt.mark_stream_open()
        self._submit()

    def first_byte(self) -> None:
        """记录首块字节耗时"""
        if self.first_byte_sent:
            return
        self.first_byte_sent = True
        self.attempt.ttft_ms = int(round((time.time() - self.started_at) * 1000))

    def observe_chunk(self, chunk: bytes) -> None:
        """累计响应体大小（只用于排障，不记录内容）"""
        self.bytes_sent += len(chunk or b"")

    def finish(self, completed: bool, http_status: Optional[int]) -> None:
        """响应结束：按是否完整发完与状态码收尾"""
        self.attempt.http_status = http_status
        self.attempt.duration_ms = int(round((time.time() - self.started_at) * 1000))
        self.attempt.done_sent = bool(completed)

        if not completed:
            self.attempt.mark_aborted()
            self._finalize()
            return

        if http_status is not None and http_status >= 400:
            self.attempt.outcome = OUTCOME_FAILURE
            self.attempt.finalized = True
            self.attempt.error_category = _status_category(http_status)
            self.attempt.error_code = f"http_{http_status}"
        else:
            self.attempt.outcome = OUTCOME_SUCCESS
            self.attempt.finalized = True
        self._finalize()

    def fail(self, error: Any = None, http_status: Optional[int] = 502) -> None:
        """还没拿到上游响应就失败（连接失败、超时等）"""
        self.attempt.mark_failure(error, http_status=http_status)
        self.attempt.duration_ms = int(round((time.time() - self.started_at) * 1000))
        try:
            category = self.attempt.error_category or _error_category(error)
            self.attempt.error_category = category
            self.attempt.error_code = self.attempt.error_code or category
        except Exception:  # noqa: BLE001 - 审计字段失败不影响主流程
            pass
        self._submit()

    # ---------- 内部 ----------

    def _submit(self) -> None:
        collector = getattr(self.manager, "collector", None)
        if collector is not None:
            collector.submit(self.attempt)

    def _finalize(self) -> None:
        collector = getattr(self.manager, "collector", None)
        if collector is not None:
            collector.finalize(self.attempt)
        else:
            logger.debug(f"透传审计未启用: {self.attempt.attempt_key}")


def _status_category(status: int) -> str:
    """上游状态码归类，便于按错误类型聚合"""
    if 400 <= status < 500:
        return "upstream_4xx"
    if 500 <= status < 600:
        return "upstream_5xx"
    return "upstream_error"


def _error_category(error: Any) -> str:
    """连接/超时等异常归类"""
    name = type(error).__name__ if error is not None else ""
    if "Timeout" in name:
        return "upstream_timeout"
    return "upstream_connection"
