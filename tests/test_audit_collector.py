"""审计采集器单元测试

覆盖：
- 热路径约束：submit 是同步的、不抛异常、不碰磁盘
- 队列满时丢弃最旧记录并计数
- 写库失败时保留缓冲并计数（不丢数据、不抛异常）
- 只记失败尝试的开关
- 关闭时排空缓冲
- 落库后喂 Prometheus 打点
"""

import asyncio

import pytest

from openai_proxy.audit.collector import AuditCollector
from openai_proxy.audit.config import AuditConfig
from openai_proxy.audit.record import AuditAttempt, OUTCOME_FAILURE


class RecordingStore:
    """记录写入调用的假存储"""

    def __init__(self, fail_times: int = 0):
        self.rows = []
        self.insert_calls = 0
        self.fail_times = fail_times

    def init_schema(self) -> None:
        return None

    def insert_many(self, rows):
        self.insert_calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("模拟写库失败")
        self.rows.extend(rows)
        return len(rows)

    def finalize_row(self, attempt_key, fields):
        return 0

    def prune(self, before_iso):
        return 0

    def prune_old(self, retention_days):
        return 0

    def stats(self):
        return {"rows": len(self.rows)}

    def query(self, sql, params=()):
        return []

    def close(self):
        return None


class RecordingMetrics:
    """记录打点调用的假指标收集器"""

    def __init__(self):
        self.requests = []
        # 故意抛异常，验证审计不会因此中断
        self.broken = False

    def record_request(self, **kwargs):
        self.requests.append(kwargs)

    def observe_stream_stats(self, attempt):
        pass

    def record_failover(self, from_platform, to_platform):
        self.requests.append({"failover": (from_platform, to_platform)})


def _attempt(key: str, outcome: str = OUTCOME_FAILURE) -> AuditAttempt:
    """构造一条审计行并按结局收尾"""
    attempt = AuditAttempt(
        attempt_key=key,
        gateway_request_id=key.rsplit("-", 1)[0],
        endpoint="chat_completions",
        stream=False,
        platform="example-platform",
        model_name="example-platform|model-a",
    )
    if outcome == OUTCOME_FAILURE:
        attempt.mark_failure()
    else:
        attempt.mark_success()
    return attempt


def _collector(**config_overrides) -> AuditCollector:
    config = AuditConfig(**config_overrides)
    return AuditCollector(config, RecordingStore())


class TestHotPath:
    """热路径约束"""

    @pytest.mark.unit
    def test_submit是同步方法且不碰磁盘(self):
        collector = _collector()

        result = collector.submit(_attempt("req1-1"))

        assert result is True
        # 还没刷盘：存储一次都没被调用
        assert collector.store.insert_calls == 0

    @pytest.mark.unit
    def test_存储异常时submit不外抛(self):
        config = AuditConfig()
        broken_store = RecordingStore(fail_times=5)
        collector = AuditCollector(config, broken_store)

        assert collector.submit(_attempt("req1-1")) is True

    @pytest.mark.unit
    def test_队列满时丢弃最旧记录并计数(self):
        collector = _collector(queue_size=3, batch_size=100)

        for index in range(5):
            collector.submit(_attempt(f"req{index}-1"))

        stats = collector.stats()
        assert stats["dropped_total"] == 2
        assert stats["pending_rows"] == 3
        # 留下的是最新的三条
        await_flushed = list(collector._pending.keys())
        assert await_flushed == ["req2-1", "req3-1", "req4-1"]


class TestFlush:
    """刷盘"""

    @pytest.mark.asyncio
    async def test_刷盘把缓冲写入存储(self):
        collector = _collector(flush_interval=0.05)
        collector.submit(_attempt("req1-1"))
        collector.submit(_attempt("req1-2"))

        await collector._flush_once()

        assert len(collector.store.rows) == 2
        assert collector.stats()["pending_rows"] == 0

    @pytest.mark.asyncio
    async def test_按批大小分片写库(self):
        collector = _collector(batch_size=2, flush_interval=5)
        for index in range(5):
            collector.submit(_attempt(f"req{index}-1"))

        await collector._flush_once()

        assert collector.store.insert_calls == 3
        assert len(collector.store.rows) == 5

    @pytest.mark.asyncio
    async def test_写库失败时保留缓冲并计数(self):
        config = AuditConfig(flush_interval=5)
        store = RecordingStore(fail_times=1)
        collector = AuditCollector(config, store)
        collector.submit(_attempt("req1-1"))

        await collector._flush_once()

        assert collector.stats()["write_errors_total"] == 1
        assert collector.stats()["pending_rows"] == 1

        # 第二次成功后缓冲清空
        await collector._flush_once()
        assert collector.stats()["pending_rows"] == 0
        assert len(collector.store.rows) == 1

    @pytest.mark.asyncio
    async def test_后台任务异常不会退出(self):
        config = AuditConfig(flush_interval=0.01)
        collector = AuditCollector(config, RecordingStore(fail_times=10))
        collector.start()
        try:
            # 有缓冲数据才会真正尝试写库，写失败才会计数
            collector.submit(_attempt("req1-1"))
            await asyncio.sleep(0.05)
        finally:
            await collector.close()

        assert collector.stats()["write_errors_total"] >= 1


class TestFiltering:
    """只记失败尝试"""

    @pytest.mark.asyncio
    async def test_record_failed_only跳过成功记录(self):
        collector = _collector(record_failed_only=True)

        collector.submit(_attempt("req1-1", outcome="success"))
        collector.submit(_attempt("req2-1", outcome=OUTCOME_FAILURE))
        await collector._flush_once()

        assert collector.stats()["skipped_total"] == 1
        assert len(collector.store.rows) == 1
        assert collector.store.rows[0]["outcome"] == OUTCOME_FAILURE


class TestLifecycle:
    """启动与关闭"""

    @pytest.mark.asyncio
    async def test_close会排空缓冲(self):
        collector = _collector(flush_interval=30)
        collector.start()
        collector.submit(_attempt("req1-1"))

        await collector.close()

        assert len(collector.store.rows) == 1
        assert collector.stats()["last_flush_at"]

    @pytest.mark.asyncio
    async def test_未启动也能安全关闭(self):
        collector = _collector()

        await collector.close()


class TestMetrics:
    """落库后喂指标"""

    @pytest.mark.asyncio
    async def test_落库时同步打点一次(self):
        config = AuditConfig(flush_interval=5)
        metrics = RecordingMetrics()
        collector = AuditCollector(config, RecordingStore(), metrics=metrics)
        collector.submit(_attempt("req1-1"))

        await collector._flush_once()

        assert len(metrics.requests) == 1
        assert metrics.requests[0]["status"] == "failure"

    @pytest.mark.asyncio
    async def test_prometheus关闭时不打点(self):
        config = AuditConfig(prometheus_enabled=False, flush_interval=5)
        metrics = RecordingMetrics()
        collector = AuditCollector(config, RecordingStore(), metrics=metrics)
        collector.submit(_attempt("req1-1"))

        await collector._flush_once()

        assert metrics.requests == []

    @pytest.mark.unit
    def test_故障转移计数(self):
        collector = _collector()

        collector.record_failover("example-platform|model-a", "example-platform|model-b")

        assert collector.stats()["failover_total"] == 1
