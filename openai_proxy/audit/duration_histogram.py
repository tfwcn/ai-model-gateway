"""耗时直方图

永久维度日汇总不能保存每次尝试的原始耗时，否则和审计明细一样会无限增长。
这里用固定桶数组近似 P95：桶边界按常用耗时区间划分，查询时把多行桶计数相加，
再取累计占比达到 95% 的桶上界。数值是估算值，但足够做趋势和排障。

同时提供 SQLite 聚合函数 histogram_p95：把多行 JSON 直方图合并后直接返回 P95 毫秒值。
"""
import json
from bisect import bisect_left
from typing import Any, Iterable, List, Optional

# 桶上界（毫秒）：覆盖从 10ms 到 2 分钟，超过 2 分钟统一落到最后一桶
BUCKETS_MS = (
    10, 20, 50, 100, 200, 300, 500, 750, 1000, 1500, 2000, 3000,
    5000, 7500, 10000, 15000, 20000, 30000, 60000, 120000,
)


class DurationHistogram:
    """固定桶的耗时分布，负责 JSON 序列化、合并与分位数估算"""

    def __init__(self, counts: Optional[Iterable[Any]] = None):
        """按桶边界初始化计数；多余或缺失的桶自动补齐"""
        self.counts: List[int] = [0] * len(BUCKETS_MS)
        for index, value in enumerate(list(counts or [])[: len(BUCKETS_MS)]):
            self.counts[index] = max(0, int(value or 0))

    @classmethod
    def from_json(cls, raw: Any) -> "DurationHistogram":
        """从数据库里的 JSON 数组还原；非法内容按空直方图处理"""
        if raw is None or raw == "":
            return cls()
        try:
            payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except (TypeError, ValueError):
            return cls()
        if not isinstance(payload, list):
            return cls()
        return cls(payload)

    def add(self, duration_ms: Optional[int]) -> None:
        """记录一次耗时；非正数不进入分布（P95 只看真实耗时样本）"""
        if duration_ms is None:
            return
        value = int(duration_ms)
        if value <= 0:
            return
        index = bisect_left(BUCKETS_MS, value)
        if index >= len(BUCKETS_MS):
            index = len(BUCKETS_MS) - 1
        self.counts[index] += 1

    def merge(self, other: "DurationHistogram") -> "DurationHistogram":
        """把另一个直方图累加进来，返回自身便于链式调用"""
        for index, value in enumerate(other.counts):
            self.counts[index] += value
        return self

    def to_json(self) -> str:
        """紧凑 JSON，节省日汇总表空间"""
        return json.dumps(self.counts, separators=(",", ":"))

    def percentile(self, ratio: float = 0.95) -> Optional[int]:
        """取累计占比达到 ratio 的桶上界；没有样本时返回 None"""
        total = sum(self.counts)
        if total <= 0:
            return None
        target = max(1.0, total * float(ratio))
        cumulative = 0
        for bound, count in zip(BUCKETS_MS, self.counts):
            cumulative += count
            if cumulative >= target:
                return bound
        return BUCKETS_MS[-1]


class HistogramP95:
    """SQLite 聚合函数：合并多行直方图后返回 P95 毫秒值"""

    def __init__(self) -> None:
        self._histogram = DurationHistogram()

    def step(self, raw: Any) -> None:
        self._histogram.merge(DurationHistogram.from_json(raw))

    def finalize(self) -> Optional[int]:
        return self._histogram.percentile(0.95)
