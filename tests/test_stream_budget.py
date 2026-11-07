"""首块预算（stream_budget）单元测试"""

import time

import pytest

from openai_proxy.model.stream_budget import FirstChunkTimeout, StreamBudget, StreamProbeResult


class TestStreamBudget:
    """首块预算的时间计算"""

    @pytest.mark.unit
    def test_remaining_and_exhausted(self):
        """剩余时间为正时未耗尽，为负时判定耗尽"""
        budget = StreamBudget("m", total_seconds=10.0, started_at=time.time())
        assert budget.remaining > 9.0
        assert not budget.exhausted
        assert budget.elapsed() < 1.0

        expired = StreamBudget("m", total_seconds=0.0, started_at=time.time() - 1)
        assert expired.remaining < 0
        assert expired.exhausted

    @pytest.mark.unit
    def test_deadline_is_start_plus_total(self):
        """截止时间 = 开始时间 + 预算"""
        budget = StreamBudget("m", total_seconds=20.0, started_at=1000.0)
        assert budget.deadline == 1020.0

    @pytest.mark.unit
    def test_first_chunk_timeout_is_exception(self):
        """首块超时是独立异常类型，可被单独捕获"""
        assert issubclass(FirstChunkTimeout, Exception)
        with pytest.raises(FirstChunkTimeout):
            raise FirstChunkTimeout("上游未在预算内返回首块")


class TestStreamProbeResult:
    """探测结果数据结构"""

    @pytest.mark.unit
    def test_default_chunks_is_independent(self):
        """默认 chunks 不能在各实例间共享"""
        first, second = StreamProbeResult(False), StreamProbeResult(False)
        first.chunks.append(b"x")
        assert second.chunks == []
        assert second.budget_exhausted is False
