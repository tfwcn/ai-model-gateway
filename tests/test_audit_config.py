"""审计配置单元测试

覆盖：
- 环境变量默认值与解析
- models.yaml 顶层 audit: 段覆盖环境变量（YAML 优先）
- 非法取值归一化到安全范围
"""

import pytest

from openai_proxy.audit.config import AuditConfig


class TestAuditConfigDefaults:
    """默认值与关闭开关"""

    @pytest.mark.unit
    def test_默认开启审计并使用默认库路径(self, monkeypatch):
        for key in ("AUDIT_ENABLED", "AUDIT_DB_PATH", "AUDIT_RETENTION_DAYS", "AUDIT_FLUSH_INTERVAL"):
            monkeypatch.delenv(key, raising=False)

        config = AuditConfig.from_env()

        assert config.enabled is True
        assert config.db_path == "data/audit.db"
        assert config.retention_days == 30
        assert config.batch_size == 200
        # 默认不带 model 标签：爬虫模式下标签基数会失控
        assert config.metrics_label_model is False

    @pytest.mark.unit
    def test_关闭开关支持多种写法(self, monkeypatch):
        monkeypatch.setenv("AUDIT_ENABLED", "false")
        assert AuditConfig.from_env().enabled is False

        monkeypatch.setenv("AUDIT_ENABLED", "0")
        assert AuditConfig.from_env().enabled is False

        monkeypatch.setenv("AUDIT_ENABLED", "no")
        assert AuditConfig.from_env().enabled is False

    @pytest.mark.unit
    def test_非法数值回落到默认值(self, monkeypatch):
        monkeypatch.setenv("AUDIT_RETENTION_DAYS", "abc")
        monkeypatch.setenv("AUDIT_BATCH_SIZE", "-5")
        monkeypatch.setenv("AUDIT_FLUSH_INTERVAL", "0")

        config = AuditConfig.from_env()

        assert config.retention_days == 30
        assert config.batch_size == 200
        assert config.flush_interval == 2.0


class TestAuditConfigYaml:
    """配置文件优先级"""

    @pytest.mark.unit
    def test_yaml段覆盖环境变量(self, monkeypatch):
        monkeypatch.setenv("AUDIT_RETENTION_DAYS", "7")

        config = AuditConfig.from_env().apply_yaml({"retention_days": 90, "db_path": "data/custom.db"})

        assert config.retention_days == 90
        assert config.db_path == "data/custom.db"

    @pytest.mark.unit
    def test_yaml非法值不破坏既有配置(self):
        config = AuditConfig.from_env().apply_yaml({"retention_days": "not-a-number", "batch_size": None})

        assert config.retention_days == 30
        assert config.batch_size == 200

    @pytest.mark.unit
    def test_retention_days写0不会变成永不清理(self):
        """字段字典写着「下限是 1」：小于 1 的取值一律退回默认 30，不会出现「以为不清理」或「其实全删」"""
        assert AuditConfig.from_env().apply_yaml({"retention_days": 0}).retention_days == 30
        assert AuditConfig.from_env().apply_yaml({"retention_days": -5}).retention_days == 30

    @pytest.mark.unit
    def test_缺少或非字典的audit段时保持原配置(self):
        config = AuditConfig.from_env()

        assert config.apply_yaml(None) is config
        assert config.apply_yaml("oops") is config

    @pytest.mark.unit
    def test_to_dict暴露全部字段供管理接口展示(self):
        payload = AuditConfig.from_env().to_dict()

        assert payload["enabled"] is True
        assert payload["db_path"] == "data/audit.db"
        assert "retention_days" in payload and "flush_interval" in payload
