"""
首块预算 - 约束「从发起请求到拿到首个有效内容」的等待时间

设计动机：
- 上游连接建立（DNS/TCP/TLS）或上游前置网关排队时，可能出现长时间静默；
  此时既没有响应头也没有首块，`stream_probe_timeout` 之类的探测逻辑覆盖不到，
  客户端只能干等，直到总超时（如 120s）才失败。
- 把这段等待单独抽成「首块预算」：超预算即放弃当前模型，交由上层做故障转移。

职责单一：本模块只提供预算的时间计算与探测结果的数据结构，不参与任何 I/O。
"""
import time
from dataclasses import dataclass, field
from typing import List


class FirstChunkTimeout(Exception):
    """首块预算耗尽：响应头 + 首个有效内容未在预算内到达"""


@dataclass
class StreamBudget:
    """单次调用的首块预算（单位：秒）"""

    model_name: str
    total_seconds: float
    started_at: float = field(default_factory=time.time)

    @property
    def deadline(self) -> float:
        """预算截止的绝对时间点"""
        return self.started_at + self.total_seconds

    @property
    def remaining(self) -> float:
        """距离截止还剩多少秒，可能为负"""
        return self.deadline - time.time()

    @property
    def exhausted(self) -> bool:
        """预算是否已经耗尽"""
        return self.remaining <= 0

    def elapsed(self) -> float:
        """从预算开始到现在的耗时（秒）"""
        return time.time() - self.started_at


@dataclass
class StreamProbeResult:
    """流式内容探测结果"""

    has_content: bool
    chunks: List[bytes] = field(default_factory=list)
    # 是否因为首块预算耗尽而结束（用于区别于「上游返回空流」）
    budget_exhausted: bool = False
