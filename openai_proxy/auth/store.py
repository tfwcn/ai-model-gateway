"""
用户级密钥存储 - SQLite 落库

设计要点：
- 只保存密钥的 SHA-256 摘要与可读前缀，明文只在创建时返回一次。
- 与审计库分开存放：关闭审计（AUDIT_ENABLED=false）时鉴权仍然可用。
- 单写者模型：管理接口通过线程池调用，请求热路径只读内存快照。
"""
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TABLE = "api_users"

_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    key_prefix  TEXT NOT NULL,
    key_hash    TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
)
"""

_INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS idx_api_users_hash ON {TABLE}(key_hash)",
)

# 管理页面展示的列（key_hash 不返回给前端）
_PUBLIC_COLUMNS = "id, name, key_prefix, created_at"

MAX_NAME_LENGTH = 64


def utc_now_iso() -> str:
    """创建时间（UTC，毫秒精度）"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class UserKeyStore:
    """用户级密钥的 SQLite 存储"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)

        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")

    def init_schema(self) -> None:
        """建表建索引（幂等）"""
        with self._lock, self._conn:
            self._conn.execute(_CREATE_DDL)
            for ddl in _INDEX_DDL:
                self._conn.execute(ddl)

    # ---------- 读 ----------

    def list_users(self) -> List[Dict[str, Any]]:
        """列出全部用户（不含密钥摘要）"""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_PUBLIC_COLUMNS} FROM {TABLE} ORDER BY id ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def snapshot(self) -> Dict[str, str]:
        """返回 key_hash -> 用户标签的快照，供中间件在内存里查"""
        with self._lock:
            rows = self._conn.execute(f"SELECT key_hash, name FROM {TABLE}").fetchall()
        return {row["key_hash"]: row["name"] for row in rows}

    def exists_name(self, name: str) -> bool:
        """用户名是否已被占用"""
        with self._lock:
            row = self._conn.execute(f"SELECT 1 FROM {TABLE} WHERE name = ?", (name,)).fetchone()
        return row is not None

    # ---------- 写 ----------

    def create_user(self, name: str, key_prefix: str, key_hash: str) -> Dict[str, Any]:
        """写入一个用户；用户名重复时抛 sqlite3.IntegrityError，由上层转成 409"""
        clean_name = normalize_name(name)
        created_at = utc_now_iso()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                f"INSERT INTO {TABLE} (name, key_prefix, key_hash, created_at) VALUES (?, ?, ?, ?)",
                (clean_name, key_prefix, key_hash, created_at),
            )
            user_id = int(cursor.lastrowid)
        return {
            "id": user_id,
            "name": clean_name,
            "key_prefix": key_prefix,
            "created_at": created_at,
        }

    def delete_user(self, user_id: int) -> bool:
        """删除指定用户；返回是否真的删掉了一行"""
        with self._lock, self._conn:
            cursor = self._conn.execute(f"DELETE FROM {TABLE} WHERE id = ?", (int(user_id),))
            return cursor.rowcount > 0

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error as exc:  # noqa: BLE001 - 关闭失败只告警
                logger.debug(f"关闭用户密钥库失败: {exc}")


def normalize_name(name: str) -> str:
    """用户名只做长度与空白校验：它是审计标签，不是标识符"""
    clean = (name or "").strip()
    if not clean:
        raise ValueError("用户名不能为空")
    if len(clean) > MAX_NAME_LENGTH:
        raise ValueError(f"用户名最多 {MAX_NAME_LENGTH} 个字符")
    if any(char in clean for char in "\r\n\t"):
        raise ValueError("用户名不能包含换行或制表符")
    return clean


def parse_user_id(raw: str) -> Optional[int]:
    """解析路径里的用户 ID；非法值返回 None"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None
