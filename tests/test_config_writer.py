"""配置写盘单元测试 - 备份、原子替换、回滚、拒绝不可加载的内容

这一层是唯一真正碰 models.yaml 的地方，测试的重点是「出错时文件必须完好」。
"""

import os
import stat

import pytest

from openai_proxy.config_store.document import ConfigDocument
from openai_proxy.config_store.writer import BACKUP_SUFFIX, ConfigWriteError, ConfigWriter

ORIGINAL = """example-platform:
  baseUrl: "https://api.example.com/v1"
  apiKey: "${EXAMPLE_PLATFORM_API_KEY}" # 密钥放在环境变量里
  models:
    - "model-a"
"""

UPDATED = """example-platform:
  baseUrl: "https://api.example.com/v1"
  apiKey: "${EXAMPLE_PLATFORM_API_KEY}" # 密钥放在环境变量里
  timeout: 60
  models:
    - "model-a"
"""


@pytest.fixture
def config_file(tmp_path):
    """一份可写的临时配置文件"""
    path = tmp_path / "models.yaml"
    path.write_text(ORIGINAL, encoding="utf-8")
    return str(path)


@pytest.fixture
def writer(config_file):
    """带独立备份目录的写入器"""
    return ConfigWriter(config_file, backup_dir=os.path.join(os.path.dirname(config_file), "backups"))


class TestWrite:
    """写盘"""

    @pytest.mark.unit
    def test_写入前先备份且备份内容与旧文件一致(self, writer, config_file):
        result = writer.write_text(UPDATED, note="加超时")

        assert writer.read_backup(result["backup"]) == ORIGINAL
        assert open(config_file, encoding="utf-8").read() == UPDATED

    @pytest.mark.unit
    def test_返回的前后版本号对应真实文件内容(self, writer, config_file):
        before = ConfigDocument.load(config_file).version

        result = writer.write_text(UPDATED)
        after = ConfigDocument.load(config_file).version

        assert result["version_before"] == before
        assert result["version_after"] == after
        assert before != after

    @pytest.mark.unit
    def test_备份名带时间戳且可以列出来(self, writer):
        result = writer.write_text(UPDATED, note="加超时")
        items = writer.backups()

        assert items[0]["name"] == result["backup"]
        assert items[0]["note"] == "加超时"
        assert items[0]["stamp"].endswith("Z")
        assert items[0]["bytes"] == len(ORIGINAL.encode("utf-8"))

    @pytest.mark.unit
    def test_写完不留下临时文件(self, writer, config_file):
        writer.write_text(UPDATED)

        leftovers = [name for name in os.listdir(os.path.dirname(config_file)) if name.startswith(".models.yaml.")]
        assert leftovers == []

    @pytest.mark.unit
    def test_保留原文件的权限(self, writer, config_file):
        os.chmod(config_file, 0o640)
        writer.write_text(UPDATED)

        assert stat.S_IMODE(os.stat(config_file).st_mode) == 0o640

    @pytest.mark.unit
    def test_配置文件不存在时也能写并且不产生空备份(self, tmp_path):
        path = str(tmp_path / "models.yaml")
        writer = ConfigWriter(path, backup_dir=str(tmp_path / "backups"))

        result = writer.write_text(UPDATED)

        assert result["backup"] == ""
        assert open(path, encoding="utf-8").read() == UPDATED


class TestReject:
    """拒绝写盘：绝不能把配置文件写成不可加载的状态"""

    @pytest.mark.unit
    def test_语法错误被拒绝且原文件完好(self, writer, config_file):
        with pytest.raises(ConfigWriteError) as exc:
            writer.write_text("models: [未闭合", note="故意的")

        assert "无法解析" in exc.value.message
        assert open(config_file, encoding="utf-8").read() == ORIGINAL

    @pytest.mark.unit
    def test_根节点不是对象时被拒绝(self, writer, config_file):
        with pytest.raises(ConfigWriteError) as exc:
            writer.write_text("- 只是\n- 一个列表\n")

        assert "根节点必须是对象" in exc.value.message
        assert open(config_file, encoding="utf-8").read() == ORIGINAL

    @pytest.mark.unit
    def test_拒绝写盘时不会留下备份(self, writer, config_file):
        with pytest.raises(ConfigWriteError):
            writer.write_text("models: [未闭合")

        assert writer.backups() == []

    @pytest.mark.unit
    def test_备份名不允许路径穿越(self, writer):
        with pytest.raises(ConfigWriteError) as exc:
            writer.rollback("../etc/passwd")

        assert "不合法" in exc.value.message

    @pytest.mark.unit
    def test_备份不存在时报错(self, writer):
        with pytest.raises(ConfigWriteError) as exc:
            writer.read_backup(f"missing{BACKUP_SUFFIX}")

        assert "不存在" in exc.value.message


class TestRollback:
    """回滚"""

    @pytest.mark.unit
    def test_回滚后内容回到改动前(self, writer, config_file):
        written = writer.write_text(UPDATED, note="加超时")
        writer.rollback(written["backup"])

        assert open(config_file, encoding="utf-8").read() == ORIGINAL

    @pytest.mark.unit
    def test_回滚本身也被备份所以可以再回滚(self, writer, config_file):
        first = writer.write_text(UPDATED, note="加超时")
        second = writer.rollback(first["backup"])
        third = writer.rollback(second["backup"])

        # 回滚前当前内容也被备份，因此能一路撤销回去
        assert open(config_file, encoding="utf-8").read() == UPDATED
        assert third["version_after"] == first["version_after"]

    @pytest.mark.unit
    def test_回滚的备份备注里写明回滚目标(self, writer):
        first = writer.write_text(UPDATED, note="加超时")
        writer.rollback(first["backup"])

        notes = {item["name"]: item["note"] for item in writer.backups()}
        assert any(note.startswith("回滚到 ") for note in notes.values())


class TestPrune:
    """备份数量上限"""

    @pytest.mark.unit
    def test_只保留最近若干份备份(self, config_file, tmp_path):
        writer = ConfigWriter(config_file, backup_dir=str(tmp_path / "backups"), max_backups=3)

        text = ORIGINAL
        for index in range(6):
            text = text.replace("model-a", f"model-a{index}")
            writer.write_text(text, note=f"第 {index} 次")

        items = writer.backups()
        assert len(items) == 3
        assert len(writer.read_backup(items[0]["name"])) > 0

    @pytest.mark.unit
    def test_同一秒内多次写入不会互相覆盖备份(self, writer):
        for index in range(3):
            writer.write_text(UPDATED.replace("timeout: 60", f"timeout: {60 + index}"))

        names = [item["name"] for item in writer.backups()]
        assert len(set(names)) == 3


class TestLock:
    """并发写入：CLI 与管理接口可能同时改同一份配置"""

    @pytest.mark.unit
    def test_两个写入器同时写不会留下半成品(self, config_file, tmp_path):
        import threading

        backup_dir = str(tmp_path / "backups")
        writers = [ConfigWriter(config_file, backup_dir=backup_dir) for _ in range(2)]
        texts = [UPDATED.replace("timeout: 60", f"timeout: {60 + index}") for index in range(2)]

        threads = [threading.Thread(target=writer.write_text, args=(text,)) for writer, text in zip(writers, texts)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        content = open(config_file, encoding="utf-8").read()
        # 谁后写谁生效，但文件一定是两次写入中完整的那一次
        assert content in texts
        assert len(ConfigDocument.load(config_file).platforms) == 1
        # 两次写入各自留了一份备份
        assert len(writers[0].backups()) == 2
