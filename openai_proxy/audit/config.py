"""
审计配置 - 控制审计记录与用量统计的行为

配置来源（优先级从低到高）：
1. 内置默认值
2. 环境变量（AUDIT_*）
3. models.yaml 顶层 audit: 段（最高优先级）

设计要点：
- 关闭时（enabled=false）上层全部走空实现，请求热路径零开销
- 任何非法取值都归一化到安全范围，绝不因为配置写错而影响转发
"""
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

# 配置字段 → 环境变量名
_ENV_KEYS: Dict[str, str] = {
    "enabled": "AUDIT_ENABLED",
    "db_path": "AUDIT_DB_PATH",
    "retention_days": "AUDIT_RETENTION_DAYS",
    "queue_size": "AUDIT_QUEUE_SIZE",
    "batch_size": "AUDIT_BATCH_SIZE",
    "flush_interval": "AUDIT_FLUSH_INTERVAL",
    "record_failed_only": "AUDIT_RECORD_FAILED_ONLY",
    "include_request_meta": "AUDIT_INCLUDE_REQUEST_META",
    "prometheus_enabled": "AUDIT_PROMETHEUS",
    "metrics_label_model": "METRICS_LABEL_MODEL",
}

_TRUE_WORDS = {"1", "true", "yes", "on"}
_FALSE_WORDS = {"0", "false", "no", "off"}


def _to_bool(value: Any, default: bool) -> bool:
    """宽松解析布尔：支持 true/false/yes/no/1/0，无法识别时用默认值"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
    return default


def _to_int(value: Any, default: int, minimum: int = 1) -> int:
    """解析正整数，非法或过小时用默认值"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _to_float(value: Any, default: float, minimum: float = 0.1) -> float:
    """解析正浮点数，非法或过小时用默认值"""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


@dataclass
class AuditConfig:
    """审计与用量统计配置"""

    enabled: bool = True                      # 总开关
    db_path: str = "data/audit.db"            # SQLite 库文件路径
    retention_days: int = 30                  # 保留天数，启动时自动清理更早的记录
    queue_size: int = 10000                   # 内存队列容量，满了丢弃最旧记录
    batch_size: int = 200                     # 单次写库的最大行数
    flush_interval: float = 2.0               # 后台刷盘间隔（秒）
    record_failed_only: bool = False          # 只记失败尝试，省磁盘
    include_request_meta: bool = False        # 是否记录请求侧计数（消息条数），永不记录内容
    prometheus_enabled: bool = True           # 是否把审计数据同时喂给 Prometheus
    metrics_label_model: bool = False         # Prometheus 是否带 model 标签（爬虫模式下标签基数大，默认关闭）

    @classmethod
    def from_env(cls) -> "AuditConfig":
        """从环境变量构建配置，未设置的字段使用默认值"""
        return cls(
            enabled=_to_bool(os.getenv("AUDIT_ENABLED"), True),
            db_path=os.getenv("AUDIT_DB_PATH") or "data/audit.db",
            retention_days=_to_int(os.getenv("AUDIT_RETENTION_DAYS"), 30),
            queue_size=_to_int(os.getenv("AUDIT_QUEUE_SIZE"), 10000),
            batch_size=_to_int(os.getenv("AUDIT_BATCH_SIZE"), 200),
            flush_interval=_to_float(os.getenv("AUDIT_FLUSH_INTERVAL"), 2.0),
            record_failed_only=_to_bool(os.getenv("AUDIT_RECORD_FAILED_ONLY"), False),
            include_request_meta=_to_bool(os.getenv("AUDIT_INCLUDE_REQUEST_META"), False),
            prometheus_enabled=_to_bool(os.getenv("AUDIT_PROMETHEUS"), True),
            metrics_label_model=_to_bool(os.getenv("METRICS_LABEL_MODEL"), False),
        )

    def apply_yaml(self, section: Optional[Dict[str, Any]]) -> "AuditConfig":
        """应用 models.yaml 顶层 audit: 段（同名字段覆盖环境变量），原地返回自身便于链式调用"""
        if not isinstance(section, dict):
            return self

        if isinstance(section.get("db_path"), str) and section["db_path"].strip():
            self.db_path = section["db_path"].strip()

        for field_name in ("retention_days", "queue_size", "batch_size"):
            if field_name in section:
                setattr(self, field_name, _to_int(section[field_name], getattr(self, field_name)))

        if "flush_interval" in section:
            self.flush_interval = _to_float(section["flush_interval"], self.flush_interval)

        for field_name in (
            "enabled",
            "record_failed_only",
            "include_request_meta",
            "prometheus_enabled",
            "metrics_label_model",
        ):
            if field_name in section:
                setattr(self, field_name, _to_bool(section[field_name], getattr(self, field_name)))

        return self

    def to_dict(self) -> Dict[str, Any]:
        """用于 /admin 接口展示当前生效配置"""
        return {
            "enabled": self.enabled,
            "db_path": self.db_path,
            "retention_days": self.retention_days,
            "queue_size": self.queue_size,
            "batch_size": self.batch_size,
            "flush_interval": self.flush_interval,
            "record_failed_only": self.record_failed_only,
            "include_request_meta": self.include_request_meta,
            "prometheus_enabled": self.prometheus_enabled,
            "metrics_label_model": self.metrics_label_model,
        }
