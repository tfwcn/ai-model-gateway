"""命令行用量命令测试 - 读审计库、友好报错、JSON 输出

用量命令不依赖网关进程：它按运行时同样的规则还原审计配置（默认值 -> AUDIT_* -> audit: 段），
再直接打开审计库查询。
"""

import json
import os
import time

import pytest

from openai_proxy.audit.record import AuditAttempt
from openai_proxy.audit.store import SqliteAuditStore
from openai_proxy.cli.main import CliApp

CONFIG = """example-platform:
  baseUrl: "https://api.example.com/v1"
  apiKey: "${EXAMPLE_PLATFORM_API_KEY}"
  models:
    - "model-a"
    - name: "model-b"
      first_chunk_timeout: 45
2example:
  baseUrl: "https://api.example2.com/v1"
  apiKey: "${2EXAMPLE_API_KEY}"
  models:
    - "model-c"
"""

ROWS = (
    ("r1-1", "2026-01-01T10:00:00.000+00:00", "example-platform|model-a", "success"),
    ("r2-1", "2026-01-01T10:01:00.000+00:00", "example-platform|model-a", "failure"),
    ("r2-2", "2026-01-01T10:01:05.000+00:00", "example-platform|model-b", "success"),
    ("r3-1", "2026-01-02T09:00:00.000+00:00", "2example|model-c", "aborted"),
    ("r4-1", "2026-01-02T09:05:00.000+00:00", "2example|model-c", "stream_open"),
)


def _attempt(key: str, ts: str, model: str, outcome: str) -> AuditAttempt:
    """构造一条审计行（示例平台/示例模型）

    mark_* 会用「开始时刻」算耗时，所以这里把开始时刻往前推，让耗时可控：
    第 1 次尝试约 1000ms，第 2 次约 1100ms。
    """
    seq = int(key.rsplit("-", 1)[1])
    attempt = AuditAttempt(
        attempt_key=key,
        gateway_request_id=key.rsplit("-", 1)[0],
        endpoint="chat_completions",
        stream=True,
        selector="all",
        platform=model.split("|", 1)[0],
        model_name=model,
        model_id=model.split("|", 1)[-1],
        attempt_seq=seq,
        retry_seq=seq,
        ts=ts,
        started_at=time.time() - (1.0 + 0.1 * seq),
        ttft_ms=120 + seq * 10,
    )

    if outcome == "failure":
        attempt.mark_failure(_RateLimit(), http_status=429)
    elif outcome == "aborted":
        attempt.mark_aborted()
    elif outcome == "stream_open":
        attempt.mark_stream_open()
    else:
        # 这里的行都是流式：能记成 success 就意味着 [DONE] 已经转发出去
        attempt.done_sent = True
        attempt.mark_success({
            "id": f"upstream-{key}",
            "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
            "choices": [{"message": {"content": "ok"}}],
        })
    return attempt


class _RateLimit:
    """代替 ClassifiedError：只提供审计行需要的字段"""

    category = "rate_limit"
    metadata = {"code": "rate_limited"}


def _number(text: str) -> float:
    """表格里的数字（带千分位）转成可比较的浮点数"""
    return float(text.replace(",", ""))


def run_cli(capsys, *argv):
    code = CliApp().run(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return str(path)


@pytest.fixture
def audit_db(tmp_path):
    """一份写好用量记录的审计库"""
    path = str(tmp_path / "audit.db")
    store = SqliteAuditStore(path)
    store.init_schema()
    store.insert_many([_attempt(*row).to_row() for row in ROWS])
    store.aggregate_usage()
    store.close()
    return path


@pytest.fixture
def cli_args(config_file, audit_db, monkeypatch):
    monkeypatch.setenv("AUDIT_DB_PATH", audit_db)
    monkeypatch.delenv("AUDIT_ENABLED", raising=False)
    return ["--config", config_file]


class TestSummary:
    """聚合视图"""

    @pytest.mark.unit
    def test_按模型聚合用量与性能(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "usage", "summary", "--group-by", "model")

        assert code == 0
        assert "example-platform|model-a" in out
        assert "50.0%" in out  # model-a：一次成功一次失败
        assert "429" not in out  # 聚合视图不展开错误细节

    @pytest.mark.unit
    def test_按结局聚合(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "usage", "summary", "--group-by", "outcome")

        assert code == 0
        for outcome in ("success", "failure", "aborted", "stream_open"):
            assert outcome in out

    @pytest.mark.unit
    def test_按天分桶并按请求数排序(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "usage", "summary", "--group-by", "day", "--sort", "requests")

        assert code == 0
        assert "2026-01-01" in out and "2026-01-02" in out

    @pytest.mark.unit
    def test_时间过滤可以缩小范围(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "usage", "summary", "--group-by", "model",
                               "--since", "2026-01-02T00:00:00", "--until", "2026-01-03T00:00:00")

        assert code == 0
        assert "2example|model-c" in out
        assert "example-platform|model-a" not in out

    @pytest.mark.unit
    def test_模型榜按P95排序时榜单按耗时递减(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "usage", "models", "--sort", "p95")

        assert code == 0
        cells = [line.split() for line in out.splitlines() if "|" in line]
        p95 = [_number(row[6]) for row in cells if row[6] != "-"]  # 只有未收尾尝试的模型没有 P95
        assert p95 and p95 == sorted(p95, reverse=True)

    @pytest.mark.unit
    def test_模型榜按失败数排序把不稳定的模型排在最前(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "usage", "models", "--sort", "errors")

        assert code == 0
        cells = [line.split()[0] for line in out.splitlines() if "|" in line]
        assert cells[0] == "example-platform|model-a"  # 唯一有失败尝试的模型

    @pytest.mark.unit
    def test_时序按天分桶(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "usage", "timeseries", "--bucket", "day")

        assert code == 0
        assert "2026-01-01" in out and "2026-01-02" in out

    @pytest.mark.unit
    def test_全量日用量显示输入输出缓存token(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "usage", "daily")

        assert code == 0
        assert "全量日用量" in out
        assert "输入" in out and "输出" in out and "缓存" in out
        # 两条 success 各 200 输出 tokens
        assert "400" in out

    @pytest.mark.unit
    def test_没有记录时给出空提示而不是崩溃(self, capsys, cli_args, audit_db):
        run_cli(capsys, *cli_args, "usage", "prune", "--before", "2030-01-01")
        capsys.readouterr()

        code, out, _ = run_cli(capsys, *cli_args, "usage", "summary", "--group-by", "model")

        assert code == 0
        assert "没有记录" in out


class TestDetails:
    """明细与故障转移链"""

    @pytest.mark.unit
    def test_明细可以只看失败尝试并显示错误分类(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "usage", "attempts", "--outcome", "failure")

        assert code == 0
        assert "rate_limited" in out
        assert "429" in out

    @pytest.mark.unit
    def test_明细按平台过滤(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "usage", "attempts", "--platform", "2example", "--limit", "10")

        assert code == 0
        assert "2example|model-c" in out
        assert "example-platform|model-a" not in out

    @pytest.mark.unit
    def test_明细标出DONE是否已转发(self, capsys, cli_args):
        """aborted 只出现在「未发完」的行上，这一列在终端里也要看得懂"""
        code, out, _ = run_cli(capsys, *cli_args, "usage", "attempts", "--limit", "10")

        assert code == 0
        assert "[DONE]" in out
        assert "已发完" in out
        assert "未发完" in out

    @pytest.mark.unit
    def test_chain标出DONE是否已转发(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "usage", "chain", "r2")

        assert code == 0
        assert "[DONE]: 未发完" in out  # 第 1 次失败，没有走到终止标记
        assert "[DONE]: 已发完" in out  # 第 2 次成功，内容发完了

    @pytest.mark.unit
    def test_chain还原整条故障转移链(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "usage", "chain", "r2")

        assert code == 0
        assert "共 2 次尝试" in out
        assert "#1 example-platform|model-a failure" in out
        assert "#2 example-platform|model-b success" in out
        # 链里要能看出每次尝试对应哪个上游响应
        assert "上游id upstream-r2-2" in out
        assert "上游id -" in out

    @pytest.mark.unit
    def test_chain对不存在的请求给出明确答复(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "usage", "chain", "ghost")

        assert code == 0
        assert "没有这个请求" in out

    @pytest.mark.unit
    def test_stats报告审计库自身状态(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "usage", "stats")

        assert code == 0
        assert f"rows: {len(ROWS)}" in out
        assert "retention_days" in out

    @pytest.mark.unit
    def test_prune删除指定时间之前的记录(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "usage", "prune", "--before", "2026-01-02")

        assert code == 0
        assert "已删除 3 条" in out

        status, stats, _ = run_cli(capsys, *cli_args, "usage", "stats")
        assert status == 0
        assert "rows: 2" in stats

    @pytest.mark.unit
    def test_prune拒绝非法时间(self, capsys, cli_args):
        code, _, err = run_cli(capsys, *cli_args, "usage", "prune", "--before", "not-a-date")

        assert code == 1
        assert "时间格式不对" in err


class TestAvailability:
    """审计不可用时的友好报错"""

    @pytest.mark.unit
    def test_审计库不存在时不创建空库(self, capsys, config_file, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDIT_DB_PATH", str(tmp_path / "missing" / "audit.db"))

        code, _, err = run_cli(capsys, "--config", config_file, "usage", "summary")

        assert code == 1
        assert "审计库不存在" in err
        assert not os.path.exists(str(tmp_path / "missing" / "audit.db"))

    @pytest.mark.unit
    def test_审计被关闭时说明原因(self, capsys, config_file, audit_db, monkeypatch):
        monkeypatch.setenv("AUDIT_DB_PATH", audit_db)
        monkeypatch.setenv("AUDIT_ENABLED", "false")

        code, _, err = run_cli(capsys, "--config", config_file, "usage", "attempts")

        assert code == 1
        assert "审计已关闭" in err

    @pytest.mark.unit
    def test_配置文件里的audit段优先于环境变量(self, capsys, tmp_path, audit_db, monkeypatch):
        config_path = tmp_path / "models.yaml"
        config_path.write_text(CONFIG + "\naudit:\n  db_path: " + audit_db + "\n  retention_days: 7\n", encoding="utf-8")
        monkeypatch.setenv("AUDIT_DB_PATH", str(tmp_path / "环境变量里的库.db"))

        code, out, _ = run_cli(capsys, "--config", str(config_path), "usage", "stats")

        assert code == 0
        assert "retention_days: 7" in out
        assert audit_db in out


class TestJson:
    """机器可读输出"""

    @pytest.mark.unit
    def test_summary的json输出可解析(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "usage", "summary", "--group-by", "model", "--json")

        assert code == 0
        payload = json.loads(out)
        assert payload["group_by"] == "model"
        model_a = next(item for item in payload["items"] if item["bucket"] == "example-platform|model-a")
        assert model_a["success_rate"] == 0.5
        assert model_a["failure"] == 1

    @pytest.mark.unit
    def test_chain的json输出保留全部尝试(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "usage", "chain", "r2", "--json")

        assert code == 0
        payload = json.loads(out)
        assert [item["attempt_seq"] for item in payload["attempts"]] == [1, 2]
        assert payload["attempts"][0]["http_status"] == 429

    @pytest.mark.unit
    def test_json模式的prune也输出结构(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "usage", "prune", "--before", "2026-01-02", "--json")

        assert code == 0
        assert json.loads(out)["deleted"] == 3

    @pytest.mark.unit
    def test_审计不可用时json模式也返回失败码(self, capsys, config_file, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDIT_DB_PATH", str(tmp_path / "missing.db"))

        code, out, err = run_cli(capsys, "--config", config_file, "usage", "stats", "--json")

        assert code == 0  # stats 本身不失败，只是把原因放进结果里
        assert "审计库不存在" in json.loads(out)["store"]["error"]

    @pytest.mark.unit
    def test_维度汇总命令行输出组合(self, capsys, config_file, tmp_path, monkeypatch):
        path = str(tmp_path / "dimensions.db")
        store = SqliteAuditStore(path)
        store.init_schema()
        attempt = AuditAttempt(
            attempt_key="r1-1",
            gateway_request_id="r1",
            endpoint="passthrough:example-service",
            stream=False,
            dimensions={
                "platform": "passthrough", "model": "model-a", "model_id": "model-a",
                "selector": "example-service", "size": "1024x1024",
            },
            input_tokens=100,
            cached_tokens=10,
            output_tokens=20,
            ts="2026-01-01T10:00:00.000+00:00",
        )
        attempt.mark_success()
        store.insert_many([attempt.to_row()])
        store.aggregate_usage()
        store.close()
        monkeypatch.setenv("AUDIT_DB_PATH", path)

        code, out, _ = run_cli(capsys, "--config", config_file, "usage", "dimensions",
                               "--group-by", "model,size", "--json")

        assert code == 0
        payload = json.loads(out)
        assert payload["group_by"] == ["model", "size"]
        assert payload["items"][0]["values"] == {"model": "model-a", "size": "1024x1024"}
        assert payload["items"][0]["input_tokens"] == 100
        assert payload["items"][0]["output_tokens"] == 20
