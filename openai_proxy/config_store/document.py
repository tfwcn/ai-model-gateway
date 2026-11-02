"""
配置文档 - 用 round-trip 方式加载 models.yaml

为什么用 round-trip 而不是 safe_load：
- safe_load 会把注释、引号风格全部丢掉，写回等于把用户手工维护的文件重写一遍
- round-trip 只改动需要改的那几行，行内注释与被注释掉的历史条目都原样保留

同时提供脱敏视图：密钥只暴露它引用的环境变量名与「是否已设置」，绝不返回密钥内容。
"""
import hashlib
import os
from datetime import datetime, timezone
from io import StringIO
from typing import Any, Dict, List, Optional, Tuple

from ruamel.yaml import YAML

from openai_proxy.config_store import schema

# 与现有 models.yaml 的书写风格保持一致：映射缩进 2、列表项再缩进 2
_MAP_INDENT = 2
_BLOCK_SEQ_INDENT = 2
_SEQUENCE_INDENT = 4


def new_yaml() -> YAML:
    """构造保留注释的 YAML 处理器（每次新建，避免并发编辑互相污染）"""
    handler = YAML(typ="rt")
    handler.preserve_quotes = True
    # 不折行：长 URL 被折行后用户很难读
    handler.width = 4096
    handler.map_indent = _MAP_INDENT
    handler.block_seq_indent = _BLOCK_SEQ_INDENT
    handler.sequence_indent = _SEQUENCE_INDENT
    return handler


class ConfigDocument:
    """一份可安全写回的模型配置文档"""

    def __init__(self, path: str, text: str, raw: Any = None):
        self.path = path
        self.text = text
        self.raw = raw if raw is not None else new_yaml().load(StringIO(text or ""))
        self.version = hashlib.sha256(text.encode("utf-8")).hexdigest()

    # ---------- 构造 ----------

    @classmethod
    def load(cls, path: str) -> "ConfigDocument":
        """读取配置文件（不存在时抛 FileNotFoundError，由上层转成 404）"""
        if not os.path.exists(path):
            raise FileNotFoundError(f"配置文件不存在: {path}")
        with open(path, "r", encoding="utf-8") as handle:
            return cls(path, handle.read())

    @classmethod
    def from_text(cls, path: str, text: str) -> "ConfigDocument":
        """从文本构造（校验补丁结果、回滚备份时都用它）"""
        return cls(path, text)

    def dump(self) -> str:
        """序列化为 YAML 文本"""
        buffer = StringIO()
        new_yaml().dump(self.raw, buffer)
        return buffer.getvalue()

    @property
    def dump_version(self) -> str:
        """将要写盘的文本的版本号（内存文档被改过之后 version 已经代表旧内容了）"""
        return hashlib.sha256(self.dump().encode("utf-8")).hexdigest()

    # ---------- 结构访问 ----------

    @property
    def platforms(self) -> Dict[str, Any]:
        """平台名 → 平台配置（排除顶层保留段）"""
        if not isinstance(self.raw, dict):
            return {}
        return {key: value for key, value in self.raw.items() if key not in schema.RESERVED_TOP_KEYS}

    @property
    def aliases(self) -> Dict[str, Any]:
        if not isinstance(self.raw, dict):
            return {}
        aliases = self.raw.get("all_aliases")
        return aliases if isinstance(aliases, dict) else {}

    @property
    def audit(self) -> Dict[str, Any]:
        if not isinstance(self.raw, dict):
            return {}
        audit = self.raw.get("audit")
        return audit if isinstance(audit, dict) else {}

    def platform(self, name: str) -> Optional[Any]:
        """按名取平台配置块，不存在时返回 None"""
        if not isinstance(self.raw, dict) or name in schema.RESERVED_TOP_KEYS:
            return None
        return self.raw.get(name)

    def find_model(self, platform_name: str, model_name: str) -> Tuple[Optional[Any], int]:
        """
        定位平台 models 列表里的静态条目

        Returns:
            (条目, 下标)；找不到时 (None, -1)
        """
        platform = self.platform(platform_name)
        if not isinstance(platform, dict):
            return None, -1
        models = platform.get("models")
        if not isinstance(models, list):
            return None, -1

        for index, entry in enumerate(models):
            if entry_name(entry) == model_name:
                return entry, index
        return None, -1

    # ---------- 校验与展示 ----------

    def validate(self) -> List[schema.Issue]:
        """整份文档的校验"""
        return schema.check_document(self.raw)

    def errors(self) -> List[Dict[str, str]]:
        errors, _ = schema.split_issues(self.validate())
        return errors

    def env_status(self) -> List[Dict[str, Any]]:
        """配置引用的环境变量是否已设置（只报告存在性，不含值）"""
        return schema.env_status(self.platforms)

    def view(self) -> Dict[str, Any]:
        """脱敏的结构化视图，供前端与 CLI 展示"""
        errors, warnings = schema.split_issues(self.validate())
        return {
            "path": self.path,
            "version": self.version,
            "bytes": len(self.text.encode("utf-8")),
            "modified_at": _mtime_iso(self.path),
            "audit": _plain(self.audit),
            "aliases": [
                {"name": alias_name, "selectors": [str(item) for item in (selectors or [])]}
                for alias_name, selectors in self.aliases.items()
            ],
            "platforms": [self._platform_view(name, config) for name, config in self.platforms.items()],
            "editable": {
                "platform_fields": sorted(field for field in schema.PLATFORM_FIELDS if field not in ("models", "plugin")),
                "model_fields": sorted(schema.MODEL_FIELDS),
                "audit_fields": sorted(schema.AUDIT_FIELDS),
                "ops": list(_ops_hint()),
            },
            "errors": errors,
            "warnings": warnings,
        }

    def _platform_view(self, name: str, config: Any) -> Dict[str, Any]:
        """单个平台的脱敏视图"""
        if not isinstance(config, dict):
            return {"name": name, "invalid": True}

        models = config.get("models") or []
        plugin = config.get("plugin") if isinstance(config.get("plugin"), dict) else None
        args = (plugin or {}).get("args") or {}

        return {
            "name": name,
            "baseUrl": str(config.get("baseUrl") or ""),
            "apiKey": _api_key_ref(name, config.get("apiKey")),
            "enabled": bool(config.get("enabled", True)),
            "weight": config.get("weight", 1),
            "timeout": config.get("timeout", 30),
            "retry_count": config.get("retry_count", 3),
            "quota_period": config.get("quota_period"),
            "connect_timeout": config.get("connect_timeout"),
            "first_chunk_timeout": config.get("first_chunk_timeout"),
            "stream_probe_timeout": config.get("stream_probe_timeout"),
            "enable_tool_call_conversion": config.get("enable_tool_call_conversion", True),
            "blacklist": [str(item) for item in (config.get("blacklist") or [])],
            "models": [_model_view(name, entry) for entry in models],
            "plugin": {
                "code": str((plugin or {}).get("code") or ""),
                # 爬虫模式的模型由插件灌入，不能当静态条目删除
                "scraper": "scrape_url" in args,
                "args": sorted(args.keys()),
            } if plugin else None,
        }


# ---------- 内部 ----------


def entry_name(entry: Any) -> str:
    """取模型条目名：字符串写法即名字，对象写法取 name/model"""
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        value = entry.get("name") or entry.get("model")
        return str(value).strip() if value else ""
    return ""


def model_selector(platform_name: str, model_name: str) -> str:
    """拼出可直接用于请求 model 字段的 selector（与 ModelConfig.name 规则一致）"""
    return f"{platform_name}|{model_name.replace('/', '-')}"


def _model_view(platform_name: str, entry: Any) -> Dict[str, Any]:
    """单个模型条目的视图"""
    name = entry_name(entry)
    overrides = {}
    if isinstance(entry, dict):
        overrides = {key: value for key, value in entry.items() if key not in ("name", "model")}
    return {
        "name": name,
        "selector": model_selector(platform_name, name),
        "overrides": _plain(overrides),
        "writable": isinstance(entry, dict),
    }


def _api_key_ref(platform_name: str, value: Any) -> Dict[str, Any]:
    """密钥引用描述：只说明指向哪个环境变量，绝不返回密钥本身"""
    text = str(value) if value is not None else ""
    env_name = text[2:-1] if text.startswith("${") and text.endswith("}") else None
    convention_env = f"{platform_name.upper()}_API_KEY"

    return {
        "kind": "env_placeholder" if env_name else ("literal" if text else "missing"),
        "env": env_name or convention_env,
        # 加载时 {PLATFORM}_API_KEY 优先于 yaml 里的取值
        "convention_env": convention_env,
        "env_set": bool(os.getenv(env_name or convention_env)),
        "convention_env_set": bool(os.getenv(convention_env)),
        "preview": "****" if text and not env_name else "",
    }


def _ops_hint() -> Tuple[str, ...]:
    """可执行补丁操作列表（避免与 editor 循环导入，这里内联常量）"""
    return (
        "set_platform_field",
        "unset_platform_field",
        "add_platform",
        "remove_platform",
        "add_model",
        "remove_model",
        "set_model_field",
        "unset_model_field",
        "set_alias_selectors",
        "set_audit_field",
    )


def _plain(value: Any) -> Any:
    """把 ruamel 的容器转成普通 Python 对象，便于 JSON 序列化"""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _mtime_iso(path: str) -> Optional[str]:
    """文件修改时间（UTC ISO）"""
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")
