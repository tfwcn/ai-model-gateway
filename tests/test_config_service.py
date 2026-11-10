"""配置命令服务单元测试 - 预演、写盘、并发检查、回滚、审计留痕

接口与 CLI 共用这一层，所以这里的行为就是用户在界面和命令行看到的行为。
"""

import os

import pytest

from openai_proxy.config_store.service import ConfigCommandService, ConfigConflict, ConfigInvalid

CONFIG = """example-platform:
  baseUrl: "https://api.example.com/v1"
  apiKey: "${EXAMPLE_PLATFORM_API_KEY}"
  timeout: 120
  weight: 10
  models:
    - "model-a"
    - name: "model-b"
      first_chunk_timeout: 45
scraper-platform:
  baseUrl: "https://api.scraper.example.com/v1"
  apiKey: "${SCRAPER_PLATFORM_API_KEY}"
  plugin:
    code: "plugin.example-scraper"
    args:
      scrape_url: "https://models.example.com/list"
"""


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return str(path)


@pytest.fixture
def reloaded(monkeypatch):
    """记录热重载被调用了几次（服务不会自己重启进程）"""
    calls = []

    def callback():
        calls.append(True)
        return {"reloaded": True, "needs_restart": False, "message": "已重载"}

    return calls, callback


@pytest.fixture
def service(config_file, reloaded):
    calls, callback = reloaded
    recorder = ConfigRecorder()
    return ConfigCommandService(
        config_file=config_file,
        backup_dir=os.path.join(os.path.dirname(config_file), "backups"),
        reload_callback=callback,
        audit_recorder=recorder.record,
    ), recorder, calls


class ConfigRecorder:
    """代替审计管理器，只关心记了哪些配置变更"""

    def __init__(self):
        self.records = []

    def record(self, ops, detail="", success=True):
        self.records.append({"ops": ops, "detail": detail, "success": success})
        return True


def text_of(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class TestRead:
    """只读接口"""

    @pytest.mark.unit
    def test_视图不暴露密钥内容(self, service, monkeypatch):
        service, _, _ = service
        monkeypatch.setenv("EXAMPLE_PLATFORM_API_KEY", "super-secret-value")

        view = service.view()

        assert "super-secret-value" not in str(view)
        assert view["editable"]["ops"]

    @pytest.mark.unit
    def test_原始文本与版本号一致(self, service):
        service, _, _ = service
        raw = service.raw_text()

        assert raw["text"] == CONFIG
        assert raw["version"] == service.document().version

    @pytest.mark.unit
    def test_校验当前文件(self, service):
        service, _, _ = service
        result = service.validate()

        assert result["valid"]
        assert result["errors"] == []

    @pytest.mark.unit
    def test_文件里有硬错误时validate会拒绝(self, service, config_file):
        service, _, _ = service
        with open(config_file, "w", encoding="utf-8") as handle:
            handle.write("broken-platform:\n  weight: 10\n")

        result = service.validate()

        assert not result["valid"]
        assert any("baseUrl" in issue["message"] for issue in result["errors"])


class TestPlan:
    """预演"""

    @pytest.mark.unit
    def test_预演不写盘(self, service, config_file):
        service, _, _ = service
        before = text_of(config_file)

        summary = service.plan([{"op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 3}])

        assert summary["valid"]
        assert text_of(config_file) == before
        assert "backup" not in summary

    @pytest.mark.unit
    def test_预演给出行级差异(self, service):
        service, _, _ = service
        summary = service.plan([{"op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 3}])

        assert any(line.startswith("-  weight: 10") for line in summary["diff"])
        assert any(line.startswith("+  weight: 3") for line in summary["diff"])

    @pytest.mark.unit
    def test_预演不触发热重载也不记审计(self, service, config_file, reloaded):
        service, recorder, calls = service
        service.plan([{"op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 3}])

        assert calls == []
        assert recorder.records == []


class TestWrite:
    """写盘"""

    @pytest.mark.unit
    def test_写盘后文件备份热重载审计都到位(self, service, config_file):
        service, recorder, calls = service
        version = service.raw_text()["version"]

        summary = service.write(
            [{"op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 3}],
            version=version,
            note="调权重",
        )

        assert "weight: 3" in text_of(config_file)
        assert summary["backup"]
        assert summary["reloaded"] is True
        assert calls == [True]
        assert summary["version_after"] == service.raw_text()["version"]
        assert recorder.records[0]["ops"] == "set_platform_field"

    @pytest.mark.unit
    def test_版本号过期时拒绝写入(self, service, config_file):
        service, _, _ = service
        with pytest.raises(ConfigConflict) as exc:
            service.write(
                [{"op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 3}],
                version="别人已经改过了",
            )

        assert exc.value.actual == service.raw_text()["version"]
        assert "weight: 10" in text_of(config_file)

    @pytest.mark.unit
    def test_校验不通过时不写盘不备份(self, service, config_file):
        service, recorder, calls = service
        with pytest.raises(ConfigInvalid) as exc:
            service.write([{"op": "set_platform_field", "platform": "example-platform", "field": "timeout", "value": 99999}])

        assert "必须在" in exc.value.errors[0]["message"]
        assert text_of(config_file) == CONFIG
        assert service.backups()["items"] == []
        assert calls == []
        assert recorder.records == []

    @pytest.mark.unit
    def test_没有操作时拒绝写入(self, service):
        service, _, _ = service
        with pytest.raises(ConfigInvalid):
            service.write([])

    @pytest.mark.unit
    def test_当前文件已有硬错误时拒绝继续改(self, service, config_file):
        service, _, _ = service
        with open(config_file, "w", encoding="utf-8") as handle:
            handle.write("broken-platform:\n  weight: 10\n")

        with pytest.raises(ConfigInvalid):
            service.write([{"op": "set_platform_field", "platform": "broken-platform", "field": "weight", "value": 1}])

    @pytest.mark.unit
    def test_reloadFalse时不触发热重载(self, service, config_file):
        service, _, calls = service
        summary = service.write(
            [{"op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 3}],
            reload=False,
        )

        assert summary["reloaded"] is False
        assert calls == []

    @pytest.mark.unit
    def test_审计记录失败不影响写盘结果(self, config_file):
        def broken(ops, detail="", success=True):
            raise RuntimeError("审计库不可用")

        service = ConfigCommandService(config_file=config_file, backup_dir=os.path.join(os.path.dirname(config_file), "backups"))
        service.audit_recorder = broken

        summary = service.write([{"op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 3}])

        assert summary["valid"]
        assert "weight: 3" in text_of(config_file)


class TestNeedsRestart:
    """哪些改动必须用户自己重启"""

    @pytest.mark.unit
    def test_新增平台不尝试进程内重载(self, service, config_file):
        service, _, calls = service
        summary = service.write([{
            "op": "add_platform",
            "platform": "new-platform",
            "config": {"baseUrl": "https://api.new.example.com/v1", "apiKey": "${NEW_PLATFORM_API_KEY}", "models": ["model-x"]},
        }])

        assert summary["needs_restart"] is True
        assert summary["reloaded"] is False
        assert calls == []
        names = {item["name"] for item in service.view()["platforms"]}
        assert "new-platform" in names

    @pytest.mark.unit
    def test_删除平台同样需要重启(self, service):
        service, _, calls = service
        summary = service.write([{"op": "remove_platform", "platform": "scraper-platform"}])

        assert summary["needs_restart"] is True
        assert calls == []

    @pytest.mark.unit
    def test_audit段改动同样需要重启(self, service, config_file):
        service, _, calls = service
        summary = service.write([{
            "op": "set_audit_field",
            "field": "retention_days",
            "value": 45,
        }])

        assert summary["needs_restart"] is True
        assert summary["reloaded"] is False
        assert calls == []
        assert "retention_days: 45" in text_of(config_file)

    @pytest.mark.unit
    def test_跳过重载时通知文件监听器(self, service):
        service, _, _ = service
        seen = []
        service.write_observer = seen.append

        summary = service.write([{
            "op": "set_audit_field",
            "field": "retention_days",
            "value": 45,
        }])

        assert seen and seen[0]["reloaded"] is False
        assert summary["needs_restart"] is True

    @pytest.mark.unit
    def test_重载器发现更早的待重启改动时同步到摘要(self, config_file):
        service = ConfigCommandService(
            config_file=config_file,
            backup_dir=os.path.join(os.path.dirname(config_file), "backups"),
            reload_callback=lambda: {
                "reloaded": True,
                "needs_restart": True,
                "message": "已重载；audit 段有变化，请自行重启服务后完全生效",
            },
        )

        summary = service.write([{
            "op": "set_model_field",
            "platform": "example-platform",
            "model": "model-b",
            "field": "first_chunk_timeout",
            "value": 30,
        }])

        assert summary["reloaded"] is True
        assert summary["needs_restart"] is True
        assert "audit" in summary["message"]

    @pytest.mark.unit
    def test_模型级参数改动可以进程内生效(self, service):
        service, _, calls = service
        summary = service.write([{"op": "set_model_field", "platform": "example-platform", "model": "model-b", "field": "first_chunk_timeout", "value": 30}])

        assert summary["needs_restart"] is False
        assert calls == [True]


class TestRollback:
    """回滚"""

    @pytest.mark.unit
    def test_回滚恢复内容并触发重载(self, service, config_file):
        service, recorder, calls = service
        written = service.write([{"op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 3}])

        result = service.rollback(written["backup"])

        assert text_of(config_file) == CONFIG
        assert result["rolled_back_to"] == written["backup"]
        assert result["version_after"] == written["version_before"]
        assert calls == [True, True]
        assert recorder.records[-1]["ops"] == "rollback"

    @pytest.mark.unit
    def test_回滚不存在的备份会报错(self, service):
        service, _, _ = service
        from openai_proxy.config_store.writer import ConfigWriteError

        with pytest.raises(ConfigWriteError):
            service.rollback("models.yaml.19700101T000000Z.bak")
