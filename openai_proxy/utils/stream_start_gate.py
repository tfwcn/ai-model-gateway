"""
流式请求的「快速失败窗口」

问题：
- 流式响应一旦发出第一个字节，HTTP 状态码就固定为 200，之后再失败只能塞进流里；
- 但绝大多数失败（鉴权错误、404、上游秒回 5xx）其实在几百毫秒内就发生了。

做法：
- 先用和 failover 的结果赛跑：grace 秒内出结果，就还能用真实的 4xx/5xx 状态码
  返回标准 JSON 错误，OpenAI SDK 可以直接抛对应的异常；
- 超过 grace 秒说明上游确实慢，此时才切换成「SSE + 心跳」模式，
  后续失败以标准 SSE error 事件下发。
"""
import asyncio
import logging
import os
from typing import Any, Awaitable, Optional

logger = logging.getLogger(__name__)

# 快速失败窗口（秒）：0 表示关闭，直接进入 SSE + 心跳模式
DEFAULT_GRACE_SECONDS = float(os.getenv("FAILOVER_FAST_FAIL_SECONDS", "3"))


class GateOutcome:
    """快速失败窗口的判定结果"""

    def __init__(self, task: "asyncio.Future", error: Optional[BaseException] = None):
        self.task = task
        self.error = error

    @property
    def failed(self) -> bool:
        """是否在窗口内就已经失败（此时还能返回真实状态码）"""
        return self.error is not None


class StreamStartGate:
    """在 grace 秒内等待 failover 出结果，尽量保留真实 HTTP 状态码"""

    def __init__(self, grace: Optional[float] = None):
        grace = DEFAULT_GRACE_SECONDS if grace is None else grace
        self.grace = max(0.0, float(grace))

    async def open(self, coro: Awaitable[Any]) -> GateOutcome:
        """
        启动 failover 任务，并在 grace 秒内观察它

        - 成功/失败都在窗口内：直接带出结果或异常，调用方可用真实状态码响应；
        - 超时：交出仍在运行的任务，由调用方转成 SSE 流继续等待。

        注意：超时后任务不会被取消（客户端断开时由流生成器负责回收）。
        """
        task = asyncio.ensure_future(coro)

        if self.grace <= 0:
            return GateOutcome(task=task)

        try:
            # shield 保证 wait_for 超时不会取消任务
            await asyncio.wait_for(asyncio.shield(task), timeout=self.grace)
            return GateOutcome(task=task)
        except asyncio.TimeoutError:
            logger.info(f"failover 超过 {self.grace:g} 秒仍未出结果，转入 SSE + 心跳模式")
            return GateOutcome(task=task)
        except asyncio.CancelledError:
            task.cancel()
            raise
        except Exception as exc:  # noqa: BLE001 - 失败原因交给调用方按原逻辑处理
            return GateOutcome(task=task, error=exc)