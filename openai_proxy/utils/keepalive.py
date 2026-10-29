"""
SSE Keep-Alive - 流式响应心跳注入

设计动机：
- 推理模型在首个 token 之前可能静默较久（思考阶段 / 长 prefill），
  客户端在此期间收不到任何字节，无法区分"仍在处理"与"连接已挂起"。
- 在最终输出流外包裹一层心跳：空闲超过阈值即下发 SSE 注释行，
  标准 SSE 客户端会忽略注释行，心跳仅用于保活与提示进度。

使用示例：
    async for chunk in KeepAliveStream(_convert_responses_sse(...)):
        yield chunk
"""
import asyncio
import logging
import os
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)

# 空闲多久下发一次心跳（秒）；0 或负数表示关闭心跳
DEFAULT_INTERVAL = float(os.getenv("SSE_KEEPALIVE_INTERVAL", "1.0"))


class KeepAliveStream:
    """
    把任意异步字节流包裹为"空闲即发心跳"的流。

    职责单一：只负责在源流静默期插入注释行，不解析、不改写任何业务数据。
    通过后台任务把源流泵入队列，主循环带超时地从队列取值，
    从而在等待源流的同一事件循环内插入心跳，避免阻塞。
    """

    # SSE 注释行，以冒号开头，标准客户端解析时忽略
    HEARTBEAT: bytes = b": keep-alive\n\n"
    # 源流结束的哨兵标记
    _DONE = object()

    def __init__(self, source: AsyncIterator[bytes], interval: Optional[float] = None):
        """
        Args:
            source: 需要包裹的异步字节流
            interval: 心跳间隔（秒），None 时取环境变量默认值
        """
        self.source = source
        self.interval = DEFAULT_INTERVAL if interval is None else interval

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.interval <= 0:
            # 关闭心跳时直接透传，避免无谓的队列开销
            async for chunk in self.source:
                yield chunk
            return

        queue: asyncio.Queue = asyncio.Queue()
        pump = asyncio.create_task(self._pump(queue))
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=self.interval)
                except asyncio.TimeoutError:
                    # 空闲超时：下发心跳，继续等待
                    yield self.HEARTBEAT
                    continue
                if item is self._DONE:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            # 客户端断开或异常退出时，确保后台泵任务被回收
            if not pump.done():
                pump.cancel()
            try:
                await pump
            except (asyncio.CancelledError, Exception):
                pass

    async def _pump(self, queue: "asyncio.Queue") -> None:
        """把源流内容/异常/结束信号送入队列，供主循环消费。"""
        try:
            async for chunk in self.source:
                if chunk:
                    await queue.put(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 传播给主循环，由调用方按原逻辑处理
            await queue.put(exc)
        finally:
            await queue.put(self._DONE)


class UpstreamStartHeartbeat:
    """
    等待上游返回「流对象」期间的 SSE 心跳。

    设计动机：
    - KeepAliveStream 只能在上游已经返回流对象之后才开始工作，
      而「建立连接 → 上游排队 → 首块」这段等待可能长达数十秒，
      此时客户端一个字节都收不到，容易误判为卡死或被中间层掐断连接。
    - 本类把这段等待变成「静默期产出心跳、结束后交回结果」的异步迭代器：

        waiter = UpstreamStartHeartbeat(manager.chat_completion_stream(payload))
        async for data in waiter:
            yield data            # 静默期下发 SSE 注释行
        upstream_stream = waiter.result

    职责单一：只做心跳注入，不处理上游结果，异常在迭代结束后原样抛出。
    """

    # 复用 KeepAliveStream 的 SSE 注释行，两处心跳格式保持一致
    HEARTBEAT: bytes = KeepAliveStream.HEARTBEAT

    def __init__(self, target, interval: Optional[float] = None):
        """
        Args:
            target: 需要等待的可等待对象（通常是流式调用的协程）
            interval: 心跳间隔（秒），None 时取环境变量默认值；<=0 表示关闭心跳
        """
        self._target = target
        self.interval = DEFAULT_INTERVAL if interval is None else interval
        self.result = None

    async def __aiter__(self) -> AsyncIterator[bytes]:
        task = asyncio.ensure_future(self._target)
        try:
            if self.interval <= 0:
                self.result = await task
                return
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=self.interval)
                if done:
                    break
                yield self.HEARTBEAT
            # 上游已返回：异常在此抛出，交给调用方原有逻辑处理
            self.result = task.result()
        finally:
            # 客户端断开或异常退出时，确保上游任务被回收
            if not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
