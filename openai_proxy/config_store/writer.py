"""
配置写盘 - 加锁、备份、原子替换

安全约束：
- 写之前先备份当前文件，任何一次改动都能回滚
- 用「同目录临时文件 + os.replace」原子替换，进程被杀也不会留下半截文件
- 用文件锁串行化写入，避免 CLI 与接口同时改同一份配置时互相覆盖
- 写之前重新解析一遍文本：解析不了就不写，绝不把配置文件写成不可加载的状态
"""
import fcntl
import json
import logging
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List

from openai_proxy.config_store.document import ConfigDocument

logger = logging.getLogger(__name__)

# 备份文件名后缀与命名规则
BACKUP_SUFFIX = ".bak"
_BACKUP_NAME_RE = re.compile(r"^(?P<stem>.+)\.(?P<stamp>\d{8}T\d{6}Z)(?:-\d+)?(?P<suffix>\.bak)$")


class ConfigWriteError(Exception):
    """写盘被拒绝（内容不可解析、备份缺失等）"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ConfigWriter:
    """配置文件写入器（带备份与回滚）"""

    def __init__(self, path: str, backup_dir: str = "data/config_backups", max_backups: int = 20):
        self.path = path
        self.backup_dir = backup_dir
        self.max_backups = max(1, int(max_backups))

    # ---------- 写入 ----------

    def write_text(self, text: str, note: str = "") -> Dict[str, Any]:
        """
        把新文本写回配置文件

        流程：加锁 → 备份旧文件 → 校验新文本可解析 → 原子替换 → 清理过期备份

        Returns:
            {"backup": 备份文件名, "version_before": ..., "version_after": ...}
        """
        document = _parse_or_reject(text)

        with self._lock():
            version_before = _current_version(self.path)
            backup_name = self._backup_current(note)
            _atomic_write(self.path, text)

        logger.info(f"配置已写回: {self.path}（备份 {backup_name}）")
        return {
            "backup": backup_name,
            "version_before": version_before,
            "version_after": document.version,
        }

    # ---------- 备份 ----------

    def backups(self) -> List[Dict[str, Any]]:
        """列出可用备份（按时间倒序）"""
        if not os.path.isdir(self.backup_dir):
            return []

        items: List[Dict[str, Any]] = []
        for name in os.listdir(self.backup_dir):
            if not name.endswith(BACKUP_SUFFIX):
                continue
            items.append(self._backup_info(name))

        # 同一秒内的多次写入用文件修改时间兜底，避免清理时误删最新的那份
        items.sort(key=lambda item: (str(item.get("created_at") or ""), float(item.get("modified_at") or 0.0)), reverse=True)
        return items

    def read_backup(self, name: str) -> str:
        """读取指定备份的内容"""
        return _read_text(self._backup_path(name))

    def rollback(self, name: str) -> Dict[str, Any]:
        """
        用指定备份覆盖当前配置

        当前配置也会先备份一份，因此回滚本身可以再次回滚。
        """
        text = self.read_backup(name)
        stamp = _timestamp_of(name)
        return self.write_text(text, note=f"回滚到 {stamp or name}")

    def _backup_current(self, note: str) -> str:
        """把当前配置文件复制到备份目录；文件不存在时返回空字符串"""
        if not os.path.exists(self.path):
            return ""

        stem = os.path.basename(self.path)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        os.makedirs(self.backup_dir, exist_ok=True)
        name = self._unique_backup_name(stem, stamp)
        target = os.path.join(self.backup_dir, name)

        text = _read_text(self.path)
        _atomic_write(target, text)
        _write_sidecar(target, {
            "note": note,
            "version": ConfigDocument.from_text(self.path, text).version,
            "bytes": len(text.encode("utf-8")),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })

        self._prune_backups()
        return name

    def _unique_backup_name(self, stem: str, stamp: str) -> str:
        """同一秒内多次写入时加序号，避免覆盖掉刚生成的那份备份"""
        name = f"{stem}.{stamp}{BACKUP_SUFFIX}"
        index = 1
        while os.path.exists(os.path.join(self.backup_dir, name)):
            name = f"{stem}.{stamp}-{index}{BACKUP_SUFFIX}"
            index += 1
        return name

    def _backup_info(self, name: str) -> Dict[str, Any]:
        """备份条目的展示信息"""
        path = self._backup_path(name)
        match = _BACKUP_NAME_RE.match(name)
        meta = _read_sidecar(path)

        return {
            "name": name,
            "created_at": meta.get("created_at") or _mtime_iso(path),
            # 同一秒内多次写入时靠它区分先后（排序用，界面可以不显示）
            "modified_at": os.path.getmtime(path),
            "stamp": match.group("stamp") if match else None,
            "bytes": os.path.getsize(path),
            "note": meta.get("note") or "",
            "version": (meta.get("version") or "")[:12],
        }

    def _prune_backups(self) -> None:
        """只保留最近 N 份备份"""
        for item in self.backups()[self.max_backups:]:
            path = os.path.join(self.backup_dir, os.path.basename(str(item["name"])))
            try:
                os.remove(path)
            except OSError as exc:
                logger.debug(f"清理旧备份失败: {item['name']}: {exc}")
                continue

            sidecar = _sidecar_path(path)
            try:
                if os.path.exists(sidecar):
                    os.remove(sidecar)
            except OSError as exc:
                logger.debug(f"清理备份元数据失败: {item['name']}: {exc}")

    def _backup_path(self, name: str) -> str:
        """备份文件的绝对定位（拒绝路径穿越）"""
        if not name or os.path.basename(name) != name:
            raise ConfigWriteError(f"备份名不合法: {name}")
        path = os.path.join(self.backup_dir, name)
        if not os.path.exists(path):
            raise ConfigWriteError(f"备份不存在: {name}")
        return path

    # ---------- 锁与旁路元数据 ----------

    @contextmanager
    def _lock(self) -> Iterator[None]:
        """
        跨进程文件锁

        CLI 与接口可能同时写同一份配置，用锁保证「读-备份-写」这段不被打断。
        """
        lock_path = f"{self.path}.lock"
        os.makedirs(os.path.dirname(os.path.abspath(lock_path)) or ".", exist_ok=True)

        handle = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


# ---------- 内部工具 ----------


def _write_sidecar(backup_path: str, meta: Dict[str, Any]) -> None:
    """记录备份的元数据（备注、版本、时间）；失败不影响写盘"""
    try:
        with open(_sidecar_path(backup_path), "w", encoding="utf-8") as handle:
            json.dump(meta, handle, ensure_ascii=False)
    except OSError as exc:
        logger.debug(f"写入备份元数据失败: {exc}")


def _atomic_write(path: str, text: str) -> None:
    """同目录临时文件 + os.replace：要么完整写入，要么保持原文件不动"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)

    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory, prefix=f".{os.path.basename(path)}.", delete=False
    )
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        # 保持原文件权限，避免写完后服务进程读不到
        try:
            os.chmod(handle.name, os.stat(path).st_mode & 0o7777)
        except OSError:
            os.chmod(handle.name, 0o644)
        os.replace(handle.name, path)
    except BaseException:
        if os.path.exists(handle.name):
            os.unlink(handle.name)
        raise


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _sidecar_path(backup_path: str) -> str:
    return f"{backup_path}.json"


def _parse_or_reject(text: str) -> ConfigDocument:
    """
    解析新配置文本，解析不了就直接拒绝写盘

    配置文件被写成不可加载的状态会导致服务起不来，所以这层拦截放在最前面。
    """
    try:
        document = ConfigDocument.from_text("pending", text)
    except Exception as exc:  # noqa: BLE001 - YAML 异常种类多，统一转成写盘拒绝
        raise ConfigWriteError(f"新配置无法解析，已拒绝写盘: {exc}") from exc

    if not isinstance(document.raw, dict):
        raise ConfigWriteError("新配置的根节点必须是对象，已拒绝写盘")
    return document


def _read_sidecar(backup_path: str) -> Dict[str, Any]:
    """读取备份的元数据；缺失或损坏时返回空字典"""
    try:
        with open(_sidecar_path(backup_path), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _current_version(path: str) -> str:
    """当前配置文件的版本号（文件不存在时返回空串）"""
    if not os.path.exists(path):
        return ""
    return ConfigDocument.load(path).version


def _timestamp_of(name: str) -> str:
    match = _BACKUP_NAME_RE.match(name)
    return match.group("stamp") if match else ""


def _mtime_iso(path: str) -> str:
    return datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).isoformat(timespec="seconds")
