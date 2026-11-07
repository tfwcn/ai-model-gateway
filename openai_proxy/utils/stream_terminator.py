"""
流终止判定 - 「[DONE] 就是流的结束」这条规则的实现

为什么需要这一层（真实踩点）：
- 上游发完 `[DONE]` 之后，什么时候真正结束 chunked body 各家行为不一样，可能滞后。
- 转发循环如果继续无限等上游 EOF，客户端一关闭连接（按流式协议读流的客户端拿到
  `[DONE]`/`finish_reason` 就断开去执行工具），转发协程就会卡在「等下一块」的位置被打断，
  于是内容已经发完的流被审计记成「客户端中断」。
- 所以：看到 `[DONE]` 之后只再用一个很短的收尾窗口尝试收可能跟在后面的尾片
  （有些上游把 usage 放在 `[DONE]` 之后），然后正常收尾并尽早释放上游连接。

这里只负责「什么时候不再等上游」，不解析业务内容、不碰响应格式。
"""
import asyncio
import logging
import os
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)

# 转发完 [DONE] 之后，还愿意为「可能跟在后面的尾片（usage）」多等多少秒
DEFAULT_DRAIN_TIMEOUT = float(os.getenv("SSE_DONE_DRAIN_TIMEOUT", "0.5"))

# SSE 终止标记：data 行的完整取值就是这个字符串
DONE_MARKER = "[DONE]"


def is_done_event(event: str) -> bool:
    """
    判断一个 SSE 事件是不是终止标记

    只看 data 行的完整取值，避免把正文里出现的 "[DONE]" 文本误判成流结束。
    """
    for line in event.split("\n"):
        stripped = line.strip()
        if not stripped.lower().startswith("data:"):
            continue
        if stripped[5:].strip() == DONE_MARKER:
            return True
    return False


class StreamTerminator:
    """按「[DONE] 即终止」的策略驱动上游流迭代，不再依赖上游自己结束响应体"""

    def __init__(self, source: AsyncIterator[bytes], drain_timeout: Optional[float] = None):
        """
        Args:
            source: 上游原始字节流
            drain_timeout: 转发完 [DONE] 之后的收尾窗口（秒），None 时取环境变量默认值
        """
        self.source = source
        self.drain_timeout = DEFAULT_DRAIN_TIMEOUT if drain_timeout is None else drain_timeout
        self.done_seen = False      # [DONE] 是否已经转发出去
        self.drain_expired = False  # 收尾窗口到期（上游还没结束，我们不等了）
        self._iterator = source.__aiter__()

    def mark_done(self) -> None:
        """标记 [DONE] 已转发：此后取上游数据都带超时，不再无限等待"""
        self.done_seen = True

    async def next_chunk(self) -> Optional[bytes]:
        """
        取下一块上游数据

        Returns:
            原始字节块；None 表示按当前策略应当结束转发（上游真的结束了，或收尾窗口到期）

        任务被取消（客户端断开）时 CancelledError 原样外抛：上层收尾据此判断流是否被打断。
        """
        try:
            if not self.done_seen:
                return await self._iterator.__anext__()
            return await asyncio.wait_for(self._iterator.__anext__(), timeout=self.drain_timeout)
        except StopAsyncIteration:
            return None
        except asyncio.TimeoutError:
            self.drain_expired = True
            return None

    async def release(self) -> None:
        """
        结束转发后释放上游

        先关闭迭代器，再归还底层响应：
        - 收尾窗口到期时上游还在生产数据，不显式关闭就会占着连接
        - 客户端中途断开时也要关闭，否则上游会白跑到底
        """
        close = getattr(self._iterator, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception as exc:  # noqa: BLE001 - 释放失败只影响连接复用，不影响响应
                logger.debug(f"关闭上游流迭代器失败（忽略）: {exc}")
        await self._release_underlying_response()

    async def _release_underlying_response(self) -> None:
        """上游流对象带原始响应（如 aiohttp 响应）时一并关闭，让连接回到连接池"""
        response = getattr(self.source, "original_response", None)
        if response is None:
            return
        for name in ("close", "release"):
            closer = getattr(response, name, None)
            if closer is None:
                continue
            try:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # noqa: BLE001 - 连接已不可用时无须再管
                logger.debug(f"释放上游响应失败（忽略）: {exc}")
            return
