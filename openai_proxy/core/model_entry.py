"""
平台下的模型条目解析 - 支持模型级配置覆盖

配置写法（平台下的 `models` 列表）：
    example-platform:
      baseUrl: "https://api.example.com/v1"
      apiKey: "${EXAMPLE_PLATFORM_API_KEY}"
      timeout: 120
      first_chunk_timeout: 20          # 平台级默认
      models:
        - "model-a"                    # 字符串：只声明模型，配置继承平台级
        - name: "model-b"              # 对象：声明模型 + 模型级覆盖
          first_chunk_timeout: 45
          connect_timeout: 3

设计要点：
- 只允许覆盖运行期参数（超时/重试/权重/开关等），连接信息（baseUrl/apiKey）由平台统一提供
- 同一模型同时来自插件与静态配置时按模型名去重，静态配置的覆盖优先生效，避免同一模型被重复调用
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# 允许在模型级别覆盖的字段（其余字段一律由平台统一提供）
OVERRIDABLE_FIELDS = (
    "timeout",
    "weight",
    "enabled",
    "quota_period",
    "retry_count",
    "stream_probe_timeout",
    "connect_timeout",
    "first_chunk_timeout",
    "enable_tool_call_conversion",
)

# 声明模型名的候选键
_NAME_KEYS = ("name", "model")


@dataclass
class ModelEntry:
    """平台下的单个模型条目：模型名 + 可选的模型级配置覆盖"""

    name: str
    overrides: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any) -> Optional["ModelEntry"]:
        """解析单条模型条目，字符串与对象两种写法都支持，无法解析时返回 None"""
        if isinstance(raw, str):
            name = raw.strip()
            if not name:
                logger.warning("跳过空的模型条目")
                return None
            return cls(name)

        if isinstance(raw, dict):
            name = cls._extract_name(raw)
            if not name:
                logger.warning(f"跳过缺少模型名的模型条目: {raw}")
                return None
            return cls(name, cls._extract_overrides(raw))

        logger.warning(f"跳过无法识别的模型条目（应为字符串或对象）: {raw}")
        return None

    @classmethod
    def parse_all(cls, raw_list: Any) -> List["ModelEntry"]:
        """解析模型列表，自动过滤无效条目"""
        if not isinstance(raw_list, (list, tuple)):
            return []
        entries: List["ModelEntry"] = []
        for raw in raw_list:
            entry = cls.parse(raw)
            if entry is not None:
                entries.append(entry)
        return entries

    @classmethod
    def merge(
        cls, plugin_entries: Iterable["ModelEntry"], static_entries: Iterable["ModelEntry"]
    ) -> List["ModelEntry"]:
        """合并插件与静态条目：按模型名去重，同名时静态条目的覆盖优先生效"""
        merged: Dict[str, "ModelEntry"] = {}
        for entry in plugin_entries:
            merged[entry.name] = entry
        for entry in static_entries:
            existing = merged.get(entry.name)
            if existing is None:
                merged[entry.name] = entry
            else:
                merged[entry.name] = cls(entry.name, {**existing.overrides, **entry.overrides})
        return list(merged.values())

    @staticmethod
    def _extract_name(raw: Dict[str, Any]) -> str:
        """从对象写法中取模型名"""
        for key in _NAME_KEYS:
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _extract_overrides(raw: Dict[str, Any]) -> Dict[str, Any]:
        """从对象写法中取模型级覆盖，遇到不支持的字段只告警不报错"""
        overrides: Dict[str, Any] = {}
        for key, value in raw.items():
            if key in _NAME_KEYS:
                continue
            if key in OVERRIDABLE_FIELDS:
                overrides[key] = value
            else:
                logger.warning(
                    f"忽略模型条目中不支持的字段: {key}"
                    f"（可覆盖字段: {', '.join(OVERRIDABLE_FIELDS)}）"
                )
        return overrides
