"""命令行配置命令测试 - 参数翻译、预演、写盘、退出码

约定：
- 预演（--dry-run）绝不写盘
- 写盘一定先备份
- 退出码：0 成功 / 1 校验或写盘失败 / 2 版本冲突 / 3 用法错误
"""

import json
import os

import pytest

from openai_proxy.cli.main import CliApp, main
from openai_proxy.config_store.service import ConfigCommandService

CONFIG = """all_aliases:
  alias-a:
    - "example-platform|model-a"
    - "2example|model-c"
example-platform:
  baseUrl: "https://api.example.com/v1"
  apiKey: "${EXAMPLE_PLATFORM_API_KEY}"
  timeout: 120
  weight: 10 # 优先级最高
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


def run_cli(capsys, *argv):
    """跑一次命令，返回 (退出码, 标准输出, 标准错误)"""
    code = CliApp().run(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return str(path)


@pytest.fixture
def cli_args(config_file, tmp_path):
    """每个命令都带上临时的配置文件与备份目录"""
    return ["--config", config_file, "--backup-dir", str(tmp_path / "backups")]


def text_of(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class TestRead:
    """只读命令"""

    @pytest.mark.unit
    def test_config_document列出平台与别名(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "config", "document")

        assert code == 0
        assert "example-platform" in out and "scraper-platform" in out
        assert "alias-a" in out

    @pytest.mark.unit
    def test_config_document可以只看一个平台(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "config", "document", "--platform", "example-platform")

        assert code == 0
        assert "example-platform" in out
        assert "scraper-platform" not in out

    @pytest.mark.unit
    def test_不存在的平台返回失败退出码(self, capsys, cli_args):
        code, _, err = run_cli(capsys, *cli_args, "config", "show", "ghost")

        assert code == 1
        assert "ghost" in err

    @pytest.mark.unit
    def test_config_show提示爬虫平台只能屏蔽模型(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "config", "show", "scraper-platform")

        assert code == 0
        assert "blacklist" in out

    @pytest.mark.unit
    def test_环境变量未设置时给出警告(self, capsys, cli_args, monkeypatch):
        monkeypatch.delenv("EXAMPLE_PLATFORM_API_KEY", raising=False)

        code, _, err = run_cli(capsys, *cli_args, "config", "document")

        assert code == 0
        assert "EXAMPLE_PLATFORM_API_KEY" in err

    @pytest.mark.unit
    def test_config_validate通过时退出码为零(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "config", "validate")

        assert code == 0
        assert "校验通过" in out

    @pytest.mark.unit
    def test_配置文件本身有硬错误时退出码为一(self, capsys, cli_args, config_file):
        with open(config_file, "w", encoding="utf-8") as handle:
            handle.write("broken-platform:\n  weight: 10\n")

        code, _, err = run_cli(capsys, *cli_args, "config", "validate")

        assert code == 1
        assert "baseUrl" in err


class TestWrite:
    """写盘命令"""

    @pytest.mark.unit
    def test_dry_run不写盘但给出差异(self, capsys, cli_args, config_file):
        code, out, _ = run_cli(capsys, *cli_args, "platform", "set", "example-platform",
                               "--field", "weight", "--value", "5", "--dry-run")

        assert code == 0
        assert "dry-run" in out
        assert "-  weight: 10" in out and "+  weight: 5" in out
        assert "weight: 10" in text_of(config_file)

    @pytest.mark.unit
    def test_写盘后文件变化且留下备份(self, capsys, cli_args, config_file):
        code, out, _ = run_cli(capsys, *cli_args, "platform", "set", "example-platform",
                               "--field", "weight", "--value", "5", "--note", "调权重")

        assert code == 0
        assert "weight: 5" in text_of(config_file)
        assert "备份" in out

        listed, backups, _ = run_cli(capsys, *cli_args, "config", "backups")
        assert listed == 0
        assert "调权重" in backups

    @pytest.mark.unit
    def test_行内注释在改取值后仍然保留(self, capsys, cli_args, config_file):
        code, _, _ = run_cli(capsys, *cli_args, "platform", "set", "example-platform",
                             "--field", "weight", "--value", "7")

        assert code == 0
        assert "优先级最高" in text_of(config_file)

    @pytest.mark.unit
    def test_取值越界时不写盘并返回失败(self, capsys, cli_args, config_file):
        code, _, err = run_cli(capsys, *cli_args, "platform", "set", "example-platform",
                               "--field", "timeout", "--value", "99999")

        assert code == 1
        assert "必须在" in err
        assert "timeout: 120" in text_of(config_file)

    @pytest.mark.unit
    def test_不存在的平台被拒绝(self, capsys, cli_args, config_file):
        code, _, err = run_cli(capsys, *cli_args, "platform", "set", "ghost", "--field", "weight", "--value", "1")

        assert code == 1
        assert "ghost" in err
        assert text_of(config_file) == CONFIG

    @pytest.mark.unit
    def test_列表型字段用_values(self, capsys, cli_args, config_file):
        code, _, _ = run_cli(capsys, *cli_args, "platform", "set", "example-platform",
                             "--field", "blacklist", "--values", "model-a", "model-x")

        assert code == 0
        content = text_of(config_file)
        assert 'blacklist:' in content and '"model-x"' in content

    @pytest.mark.unit
    def test_删除平台字段(self, capsys, cli_args, config_file):
        code, _, _ = run_cli(capsys, *cli_args, "platform", "unset", "example-platform", "--field", "timeout")

        assert code == 0
        content = text_of(config_file)
        assert "  timeout: 120" not in content
        # 模型级的 first_chunk_timeout 不该被连带删掉
        assert "first_chunk_timeout: 45" in content

    @pytest.mark.unit
    def test_model_add带覆盖项时写成对象条目(self, capsys, cli_args, config_file):
        code, _, _ = run_cli(capsys, *cli_args, "model", "add", "example-platform", "model-new",
                             "--field", "first_chunk_timeout", "--value", "30")

        assert code == 0
        content = text_of(config_file)
        assert '- name: "model-new"' in content
        assert "first_chunk_timeout: 30" in content

    @pytest.mark.unit
    def test_model_set与model_unset(self, capsys, cli_args, config_file):
        added, _, _ = run_cli(capsys, *cli_args, "model", "set", "example-platform", "model-a",
                              "--field", "weight", "--value", "5")
        assert added == 0
        assert "weight: 5" in text_of(config_file)

        removed, _, _ = run_cli(capsys, *cli_args, "model", "unset", "example-platform", "model-a",
                                "--field", "weight")
        assert removed == 0
        content = text_of(config_file)
        # 模型级覆盖没了，但平台自己的权重还在
        assert "weight: 5" not in content
        assert "weight: 10" in content

    @pytest.mark.unit
    def test_model_remove对爬虫平台给出blacklist指引(self, capsys, cli_args, config_file):
        code, _, err = run_cli(capsys, *cli_args, "model", "remove", "scraper-platform", "any-model")

        assert code == 1
        assert "blacklist" in err
        assert text_of(config_file) == CONFIG

    @pytest.mark.unit
    def test_alias_set覆盖selector(self, capsys, cli_args, config_file):
        code, _, _ = run_cli(capsys, *cli_args, "alias", "set", "alias-a",
                             "--selector", "all", "--selector", "2example|model-c")

        assert code == 0
        assert '- "all"' in text_of(config_file)

    @pytest.mark.unit
    def test_alias_list(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "alias", "list")

        assert code == 0
        assert "example-platform|model-a" in out

    @pytest.mark.unit
    def test_audit_set写入顶层audit段(self, capsys, cli_args, config_file):
        code, _, _ = run_cli(capsys, *cli_args, "audit", "set", "--field", "retention_days", "--value", "45")

        assert code == 0
        content = text_of(config_file)
        assert "audit:" in content and "retention_days: 45" in content

    @pytest.mark.unit
    def test_audit_set拒绝未知字段(self, capsys, cli_args):
        code, _, err = run_cli(capsys, *cli_args, "audit", "set", "--field", "nope", "--value", "1")

        assert code == 1
        assert "nope" in err

    @pytest.mark.unit
    def test_新增平台提示需要重启(self, capsys, cli_args, config_file):
        code, out, _ = run_cli(capsys, *cli_args, "platform", "add", "new-platform",
                               "--base-url", "https://api.new.example.com/v1",
                               "--api-key", "${NEW_PLATFORM_API_KEY}",
                               "--model", "model-x")

        assert code == 0
        assert "new-platform" in text_of(config_file)
        assert "重启" in out

    @pytest.mark.unit
    def test_命令行不假装已经让运行中的网关生效(self, capsys, cli_args):
        """没有热重载回调：命令行只写文件，不会谎称已进程内生效"""
        code, out, _ = run_cli(capsys, *cli_args, "platform", "set", "example-platform",
                               "--field", "weight", "--value", "5")

        assert code == 0
        assert "已在进程内重载" not in out


class TestExitCodes:
    """退出码"""

    @pytest.mark.unit
    def test_版本冲突时返回冲突码且不写盘(self, capsys, cli_args, config_file, monkeypatch):
        def stale(self):
            return {"path": self.config_file, "version": "别人已经改过了", "text": ""}

        monkeypatch.setattr(ConfigCommandService, "raw_text", stale)

        code, _, err = run_cli(capsys, *cli_args, "platform", "set", "example-platform",
                               "--field", "weight", "--value", "5")

        assert code == 2
        assert "刷新" in err or "被别人改过" in err
        assert "weight: 10" in text_of(config_file)

    @pytest.mark.unit
    def test_缺少动作时返回用法码(self, capsys):
        assert main([]) == 3

    @pytest.mark.unit
    def test_未知参数时返回用法码(self, capsys, cli_args):
        assert main(list(cli_args) + ["platform", "set", "x", "--nope"]) == 3


class TestJsonAndRollback:
    """机器可读输出与回滚"""

    @pytest.mark.unit
    def test_json输出可以直接解析(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "platform", "set", "example-platform",
                               "--field", "weight", "--value", "5", "--dry-run", "--json")

        assert code == 0
        payload = json.loads(out)
        assert payload["valid"] is True
        assert payload["dry_run"] is True
        assert payload["changes"][0]["after"] == 5

    @pytest.mark.unit
    def test_json模式下校验失败也输出结构(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "platform", "set", "ghost",
                               "--field", "weight", "--value", "5", "--json")

        assert code == 1
        assert json.loads(out)["valid"] is False

    @pytest.mark.unit
    def test_回滚恢复内容(self, capsys, cli_args, config_file):
        code, out, _ = run_cli(capsys, *cli_args, "platform", "set", "example-platform",
                               "--field", "weight", "--value", "5")
        assert code == 0

        name = _first_backup_name(out)
        rolled, rollback_out, _ = run_cli(capsys, *cli_args, "config", "rollback", name)

        assert rolled == 0
        assert text_of(config_file) == CONFIG
        assert name in rollback_out

    @pytest.mark.unit
    def test_回滚不存在的备份时返回失败(self, capsys, cli_args):
        code, _, err = run_cli(capsys, *cli_args, "config", "rollback", "models.yaml.19700101T000000Z.bak")

        assert code == 1
        assert "不存在" in err

    @pytest.mark.unit
    def test_备份目录不被配置文件路径污染(self, capsys, cli_args, tmp_path):
        run_cli(capsys, *cli_args, "platform", "set", "example-platform", "--field", "weight", "--value", "5")

        backups = os.listdir(str(tmp_path / "backups"))
        assert all(name.endswith(".bak") or name.endswith(".json") for name in backups)


class TestFieldDocs:
    """字段字典命令：命令行看到的说明与「模型配置」页完全同一份"""

    @pytest.mark.unit
    def test_config_fields解释每个可编辑字段(self, capsys, cli_args):
        from openai_proxy.config_store import schema

        code, out, _ = run_cli(capsys, *cli_args, "config", "fields")

        assert code == 0
        fields = sorted(set(schema.PLATFORM_FIELDS) | set(schema.MODEL_FIELDS) | set(schema.AUDIT_FIELDS))
        missing = [field for field in fields if f"  {field} — " not in out]
        assert not missing, f"命令行没有解释这些字段: {', '.join(missing)}"
        assert "结构说明" in out and "没有文件监听" in out

    @pytest.mark.unit
    def test_config_fields列出界面下拉里能选的取值(self, capsys, cli_args):
        """命令行看到的取值选项必须与界面上的下拉同一份，否则照着抄会抄错"""
        from openai_proxy.config_store import field_docs

        code, out, _ = run_cli(capsys, *cli_args, "config", "fields", "--scope", "platform")

        assert code == 0
        entry = field_docs.enum_fields()["quota_period"]
        for value in entry["values"]:
            assert value in out, f"命令行没有列出 quota_period 的取值 {value}"
        assert "可以不配置" in out, "命令行没写这个字段可以不配置"

    @pytest.mark.unit
    def test_config_fields可以只看一组字段(self, capsys, cli_args):
        code, out, _ = run_cli(capsys, *cli_args, "config", "fields", "--scope", "audit")

        assert code == 0
        assert "audit 段字段" in out
        assert "平台级字段" not in out

    @pytest.mark.unit
    def test_config_fields的json输出与接口契约一致(self, capsys, cli_args):
        from openai_proxy.config_store.routes import schema_payload

        code, out, _ = run_cli(capsys, *cli_args, "config", "fields", "--json")

        assert code == 0
        body = json.loads(out)
        expected = schema_payload()
        assert body["docs"] == expected["docs"], "命令行与接口给出的字段字典必须同一份"
        assert body["sections"] == expected["sections"]
        assert body["enums"] == expected["enums"], "命令行与接口给出的取值选项必须同一份"


def _first_backup_name(output: str) -> str:
    """从「已写盘，旧内容备份在: xxx」里取出备份名"""
    for line in output.splitlines():
        if "备份在:" in line:
            return line.split("备份在:", 1)[1].strip()
    raise AssertionError(f"输出里没有备份名: {output}")
