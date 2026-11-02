"""
审计上下文 - 一次客户端请求的 attempt 编号与记录器

设计要点：
- failover 只在三个调用点包一层 run_attempt()，避免在二十多个返回点散落埋点
- gateway_request_id 贯穿整条故障转移链，attempt_seq 递增，切换模型时自动记 failover
- collector 为 None 时全部退化为空操作，关闭审计零开销
- 流式结局由「转发循环是否走完」+「[DONE] 是否已转发」共同决定，
  避免把「内容已发完、客户端随后关闭连接」误记成客户端中断
"""
import logging
import uuid
from typing import Any, Optional, Tuple

from openai_proxy.audit.collector import AuditCollector
from openai_proxy.audit.record import (
    AuditAttempt,
    OUTCOME_STREAM_OPEN,
    OUTCOME_SUCCESS,
    build_attempt_key,
)

logger = logging.getLogger(__name__)


class AttemptRecorder:
    """单次上游尝试的结论落库入口"""

    def __init__(self, attempt: AuditAttempt, collector: Optional[AuditCollector], is_stream: bool):
        self.attempt = attempt
        self.collector = collector
        self.is_stream = is_stream
        self.committed = False

    def commit_stream_open(self) -> None:
        """流已交给上层转发"""
        self.attempt.mark_stream_open()
        self._submit()

    def commit_success(self, response: Any = None) -> None:
        """非流式调用完成（含 usage 提取）"""
        self.attempt.mark_success(response)
        self._submit()

    def commit_error(self, error: Any = None, http_status: Optional[int] = None) -> None:
        """本次尝试失败"""
        self.attempt.mark_failure(error, http_status)
        self._submit()

    def finalize(self, stats: Optional[dict] = None, aborted: bool = False, done_sent: bool = False) -> None:
        """
        流结束：补齐用量并改写结局（stream_open → success / aborted）

        Args:
            stats: StreamPerformanceMonitor.finish() 的统计结果
            aborted: 转发循环没有正常走完（通常是客户端关闭连接）
            done_sent: 网关是否已经把 [DONE] 转发给客户端
        """
        if self.committed and self.attempt.outcome != OUTCOME_STREAM_OPEN:
            # 已收尾的 attempt（例如非流式）不需要二次 finalize
            return

        self.attempt.apply_stats(stats)
        self.attempt.done_sent = self.attempt.done_sent or bool(done_sent)

        if aborted and not self.attempt.done_sent:
            self.attempt.mark_aborted()
            self._submit_finalize()
            return

        if aborted:
            # [DONE] 都转发出去了，说明内容已经发完：客户端随后关闭不属于中断
            logger.info(
                f"流已发完后客户端关闭连接，按完成记账: attempt={self.attempt.attempt_key} "
                f"模型={self.attempt.model_name or '-'}"
            )
        self.attempt.outcome = OUTCOME_SUCCESS
        self.attempt.finalized = True
        if self.attempt.duration_ms is None:
            self.attempt.duration_ms = self.attempt.elapsed_ms()
        self._submit_finalize()

    def _submit_finalize(self) -> None:
        if self.collector is not None:
            self.collector.finalize(self.attempt)

    def _submit(self) -> None:
        self.committed = True
        if self.collector is not None:
            self.collector.submit(self.attempt)


class AuditContext:
    """一次客户端请求的审计上下文（贯穿 failover）"""

    def __init__(
        self,
        collector: Optional[AuditCollector],
        endpoint: str,
        selector: str = "",
        stream: bool = False,
        gateway_request_id: Optional[str] = None,
        msg_count: Optional[int] = None,
    ):
        self.collector = collector
        self.endpoint = endpoint
        self.selector = selector or ""
        self.stream = stream
        self.gateway_request_id = gateway_request_id or uuid.uuid4().hex[:8]
        self.msg_count = msg_count
        self.attempt_seq = 0
        self._previous_model: str = ""
        self._last_recorder: Optional[AttemptRecorder] = None

    @property
    def last_attempt_key(self) -> Optional[str]:
        """最后一次尝试的主键，供流结束时定位"""
        return self._last_recorder.attempt.attempt_key if self._last_recorder else None

    @property
    def total_attempts(self) -> int:
        return self.attempt_seq

    def begin(self, model_config: Any, retry_seq: int = 1) -> AttemptRecorder:
        """开始一次尝试：递增序号并在切换模型时记录 failover"""
        self.attempt_seq += 1
        model_name = getattr(model_config, "name", "") or ""
        if self.collector is not None and self.attempt_seq > 1 and self._previous_model and self._previous_model != model_name:
            self.collector.record_failover(self._previous_model, model_name)
        self._previous_model = model_name

        attempt = AuditAttempt(
            attempt_key=build_attempt_key(self.gateway_request_id, self.attempt_seq),
            gateway_request_id=self.gateway_request_id,
            endpoint=self.endpoint,
            stream=self.stream,
            selector=self.selector,
            platform=_platform_of(model_name),
            model_name=model_name,
            model_id=getattr(model_config, "model", "") or "",
            attempt_seq=self.attempt_seq,
            retry_seq=retry_seq,
            msg_count=self.msg_count,
        )
        recorder = AttemptRecorder(attempt, self.collector, self.stream)
        self._last_recorder = recorder
        return recorder

    async def run_attempt(
        self,
        model_config: Any,
        awaitable: Any,
        retry_seq: int = 1,
    ) -> Tuple[bool, Any]:
        """包一层调用：按 (success, result) 落一条 attempt，返回值原样交回上层"""
        recorder = self.begin(model_config, retry_seq)
        try:
            success, result = await awaitable
        except Exception as exc:  # noqa: BLE001 - 记录后原样抛出，不改变既有控制流
            recorder.commit_error(exc)
            raise

        if success:
            if self.stream:
                # 把 attempt 主键挂到流对象上，上层转发结束时据此补齐用量
                try:
                    result.attempt_key = self._last_recorder.attempt.attempt_key
                except (AttributeError, TypeError):  # noqa: BLE001 - 结果对象不可写属性时忽略
                    pass
                recorder.commit_stream_open()
            else:
                recorder.commit_success(result)
        else:
            recorder.commit_error(result)
        return success, result

    def finalize_last(self, stats: Optional[dict] = None, aborted: bool = False, done_sent: bool = False) -> None:
        """流结束时收尾最后一次尝试"""
        if self._last_recorder is not None:
            self._last_recorder.finalize(stats, aborted, done_sent)

    def record_keepalive(self, count: int) -> None:
        """记录心跳次数（上游排队/思考时长的信号）"""
        if self._last_recorder is not None and count > 0:
            self._last_recorder.attempt.keepalive_count = count


def _platform_of(model_name: str) -> str:
    """模型名形如 platform|model，取平台段"""
    if "|" in model_name:
        return model_name.split("|", 1)[0]
    return model_name
