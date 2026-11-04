"""
配置校验规则 - 定义 models.yaml 里允许编辑的字段与取值范围

设计要点：
- 校验规则与 ConfigLoader 的读取语义保持一致，避免「界面能保存、启动时却被忽略」
- 一律用示例占位符演示（example-platform / https://api.example.com/v1 / EXAMPLE_PLATFORM_API_KEY）
- 允许以数字开头的环境变量名（例如 1EXAMPLE_API_KEY），否则已有平台无法保存
- 未知字段只告警不报错：配置文件里常有手工加的注释性字段，不应阻断保存
"""
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from openai_proxy.core.model_entry import OVERRIDABLE_FIELDS

# 平台名/别名：允许数字开头（真实配置里存在数字开头的平台名）
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
# 环境变量占位符 ${VAR}：变量名允许数字开头
_ENV_PLACEHOLDER_RE = re.compile(r"^\$\{([^}]+)\}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_MODEL_NAME_RE = re.compile(r"^[^\s|]+$")

# 顶层保留键：不是平台
RESERVED_TOP_KEYS = ("all_aliases", "audit", "passthrough")

# 字段名 → 取值类型（平台级与模型级共用同一套类型定义）
_FIELD_KINDS: Dict[str, str] = {
    "timeout": "int",
    "weight": "number",
    "enabled": "bool",
    "quota_period": "optional_str",
    "retry_count": "nonneg_int",
    "stream_probe_timeout": "optional_seconds",
    "connect_timeout": "optional_seconds",
    "first_chunk_timeout": "optional_seconds",
    "enable_tool_call_conversion": "bool",
}

# 平台级可编辑字段 → 字段类型
PLATFORM_FIELDS: Dict[str, str] = {
    "baseUrl": "url",
    "apiKey": "secret",
    "models": "model_list",
    "blacklist": "list_str",
    "plugin": "plugin",
    **_FIELD_KINDS,
}

# 模型级可覆盖字段（与 ModelEntry.OVERRIDABLE_FIELDS 同源，保证界面能改的字段加载时也认）
MODEL_FIELDS: Dict[str, str] = {field: _FIELD_KINDS[field] for field in OVERRIDABLE_FIELDS}

# audit 段可编辑字段 → 字段类型
AUDIT_FIELDS: Dict[str, str] = {
    "enabled": "bool",
    "db_path": "str",
    "retention_days": "int",
    "queue_size": "int",
    "batch_size": "int",
    "flush_interval": "seconds",
    "record_failed_only": "bool",
    "include_request_meta": "bool",
    "prometheus_enabled": "bool",
    "metrics_label_model": "bool",
}

# passthrough 段可编辑字段 → 字段类型
PASSTHROUGH_FIELDS: Dict[str, str] = {
    "enabled": "bool",
    "prefix": "str",
    "upstream": "url",
    "apiKey": "secret",
    "auth_header": "str",
    "auth_prefix": "optional_str",
    "timeout": "seconds",
    "connect_timeout": "optional_seconds",
    "body_limit": "int",
    "forward_headers": "list_str",
    "extra_headers": "map_str",
    "dimensions": "map_str",
}

# 数值字段的边界：越界一律拒绝，避免把转发调到不可用的状态
_LIMITS: Dict[str, Tuple[float, float]] = {
    "timeout": (1, 3600),
    # 下限是 1 不是 0：retry_count 为 0 时重试分支没有兜底赋值，非流式请求会变成 500
    "retry_count": (1, 20),
    "weight": (-1000, 1000),
    "stream_probe_timeout": (0.1, 600),
    "connect_timeout": (0.1, 600),
    "first_chunk_timeout": (0.1, 600),
    "flush_interval": (0.1, 600),
    "retention_days": (1, 3650),
    "queue_size": (100, 1_000_000),
    "batch_size": (1, 10_000),
    "body_limit": (1, 268_435_456),
}

# 为什么这个字段的边界是这样：越界报错时一并说出来，免得用户试半天
_LIMIT_NOTES: Dict[str, str] = {
    "retry_count": "（只试一次请写 1：0 会走到没有兜底的重试分支）",
}


@dataclass
class Issue:
    """一条校验结果：error 阻止保存，warning 只提示"""

    level: str          # error | warning
    path: str           # 例如 platforms.example-platform.timeout
    message: str

    def to_dict(self) -> Dict[str, str]:
        return {"level": self.level, "path": self.path, "message": self.message}


def valid_name(value: Any) -> bool:
    """平台名/别名是否合法"""
    return isinstance(value, str) and bool(_NAME_RE.match(value.strip()))


def valid_model_name(value: Any) -> bool:
    """模型名是否合法（不允许空白与 selector 分隔符 |）"""
    return isinstance(value, str) and bool(_MODEL_NAME_RE.match(value.strip()))


def check_value(kind: str, field: str, value: Any, path: str) -> List[Issue]:
    """按字段类型检查单个取值"""
    if kind == "url":
        return _check_url(value, path)
    if kind == "secret":
        return _check_secret(value, path)
    if kind == "bool":
        return [] if isinstance(value, bool) else [_error(path, f"{field} 必须是 true/false")]
    if kind == "int":
        return _check_number(value, path, field, integer=True, minimum=_limits_min(field))
    if kind == "nonneg_int":
        # 默认下限 0；_LIMITS 里有更高的下限时以 _LIMITS 为准（例如 retry_count 不允许 0）
        return _check_number(value, path, field, integer=True, minimum=max(0.0, _limits_min(field)))
    if kind == "number":
        return _check_number(value, path, field, integer=False, minimum=_limits_min(field))
    if kind == "seconds":
        return _check_number(value, path, field, integer=False, minimum=0.1)
    if kind == "optional_seconds":
        if value is None:
            return []
        return _check_number(value, path, field, integer=False, minimum=0.1)
    if kind == "optional_str":
        return [] if value is None or isinstance(value, str) else [_error(path, f"{field} 必须是字符串或 null")]
    if kind == "str":
        return [] if isinstance(value, str) and value.strip() else [_error(path, f"{field} 必须是非空字符串")]
    if kind == "list_str":
        return _check_list_str(value, path, field)
    if kind == "model_list":
        return _check_model_list(value, path)
    if kind == "map_str":
        return _check_map_str(value, path, field)
    if kind == "plugin":
        return _check_plugin(value, path)
    return [_error(path, f"{field} 字段类型不受支持")]


def check_platform(name: str, config: Any) -> List[Issue]:
    """校验一个平台配置块"""
    issues: List[Issue] = []
    path = f"platforms.{name}"

    if not valid_name(name):
        issues.append(_error(path, f"平台名不合法: {name}"))
    if not isinstance(config, dict):
        return issues + [_error(path, "平台配置必须是对象")]

    for key, value in config.items():
        if key not in PLATFORM_FIELDS:
            issues.append(_warning(f"{path}.{key}", f"未知字段 {key}：加载时会被忽略"))
            continue
        issues.extend(check_value(PLATFORM_FIELDS[key], key, value, f"{path}.{key}"))

    issues.extend(_check_platform_completeness(name, config, path))
    return issues


def check_model_entry(raw: Any, path: str) -> List[Issue]:
    """校验 models 列表里的单个条目（字符串或对象两种写法）"""
    if isinstance(raw, str):
        return [] if valid_model_name(raw) else [_error(path, f"模型名不合法: {raw}")]

    if isinstance(raw, dict):
        issues: List[Issue] = []
        name = raw.get("name") or raw.get("model")
        if not valid_model_name(name):
            issues.append(_error(path, "模型条目缺少合法的 name 字段"))
        for key, value in raw.items():
            if key in ("name", "model"):
                continue
            if key not in MODEL_FIELDS:
                issues.append(_warning(f"{path}.{key}", f"模型级不支持的字段: {key}"))
                continue
            issues.extend(check_value(MODEL_FIELDS[key], key, value, f"{path}.{key}"))
        return issues

    return [_error(path, "模型条目必须是字符串或对象")]


def check_alias_selectors(selectors: Any, path: str) -> List[Issue]:
    """校验别名指向的 selector 列表"""
    if not isinstance(selectors, list) or not selectors:
        return [_error(path, "别名的 selector 列表必须是非空数组")]

    issues: List[Issue] = []
    for index, selector in enumerate(selectors):
        issues.extend(check_selector(selector, f"{path}[{index}]"))
    return issues


def check_alias_targets(aliases: Dict[str, Any], platform_names: set) -> List[Issue]:
    """
    别名指向的平台是否存在

    只查平台段：爬虫平台的模型是运行时灌进来的，查模型段会对合法配置误报。
    指向不存在的平台不会让配置不可用，但这条指向永远不会命中，所以报 warning。
    """
    issues: List[Issue] = []

    for alias_name, selectors in aliases.items():
        if not isinstance(selectors, list):
            continue
        for index, selector in enumerate(selectors):
            if not isinstance(selector, str) or selector.strip() == "all":
                continue
            platform_name = selector.split("|", 1)[0].strip()
            if platform_name and platform_name not in platform_names:
                issues.append(_warning(
                    f"all_aliases.{alias_name}[{index}]",
                    f"别名 {alias_name} 指向的平台 {platform_name} 不存在，这条指向不会命中任何模型",
                ))

    return issues


def check_selector(selector: Any, path: str) -> List[Issue]:
    """selector 只允许三种写法：all / 平台名 / 平台名|模型名"""
    if not isinstance(selector, str) or not selector.strip():
        return [_error(path, "selector 必须是非空字符串")]

    text = selector.strip()
    if text == "all":
        return []

    if "|" in text:
        platform_name, _, model_name = text.partition("|")
        issues: List[Issue] = []
        if not valid_name(platform_name):
            issues.append(_error(path, f"selector 的平台段不合法: {platform_name}"))
        if not valid_model_name(model_name):
            issues.append(_error(path, f"selector 的模型段不合法: {model_name}"))
        return issues

    if not valid_name(text):
        return [_error(path, f"selector 只允许 all / 平台名 / 平台名|模型名，收到: {text}")]
    return []


def check_audit(section: Any, path: str = "audit") -> List[Issue]:
    """校验顶层 audit: 段"""
    if not isinstance(section, dict):
        return [_error(path, "audit 必须是对象")]

    issues: List[Issue] = []
    for key, value in section.items():
        if key not in AUDIT_FIELDS:
            issues.append(_warning(f"{path}.{key}", f"未知的审计字段: {key}"))
            continue
        issues.extend(check_value(AUDIT_FIELDS[key], key, value, f"{path}.{key}"))
    return issues


# 透传维度名与表达式：与 openai_proxy.passthrough.config 的规则保持一致
_PASSTHROUGH_DIMENSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PASSTHROUGH_EXPRESSION_RE = re.compile(r"^(body|query|header|path)\..+$")
_PASSTHROUGH_RESERVED_DIMENSIONS = {
    "attempt_key", "gateway_request_id", "api_user", "request_id", "endpoint",
    "stream", "attempt_seq", "retry_seq", "ts", "outcome", "finalized",
    "http_status", "error_category", "error_code", "disabled_model",
    "duration_ms", "probe_ms", "ttft_ms", "has_usage", "upstream_id",
    "input_tokens", "cached_tokens", "output_tokens", "output_chars",
    "throughput", "tool_call_converted", "keepalive_count", "msg_count",
    "done_sent", "aggregated",
}


def check_passthrough(section: Any, path: str = "passthrough") -> List[Issue]:
    """校验顶层 passthrough: 段"""
    if not isinstance(section, dict):
        return [_error(path, "passthrough 必须是对象")]

    issues: List[Issue] = []
    prefixes: Dict[str, str] = {}
    for name, config in section.items():
        route_path = f"{path}.{name}"
        if not valid_name(str(name)):
            issues.append(_error(route_path, f"透传路由名不合法: {name}"))
            continue
        if not isinstance(config, dict):
            issues.append(_error(route_path, "透传路由配置必须是对象"))
            continue
        issues.extend(_check_passthrough_route(str(name), config, route_path))

        prefix = str(config.get("prefix") or f"/proxy/{name}").rstrip("/")
        if prefix in prefixes:
            issues.append(_error(f"{route_path}.prefix", f"前缀与 {prefixes[prefix]} 重复: {prefix}"))
        else:
            prefixes[prefix] = str(name)
    return issues


def _check_passthrough_route(name: str, config: Dict[str, Any], path: str) -> List[Issue]:
    """校验单条透传路由"""
    issues: List[Issue] = []
    for key, value in config.items():
        if key not in PASSTHROUGH_FIELDS:
            issues.append(_warning(f"{path}.{key}", f"未知的透传字段: {key}"))
            continue
        issues.extend(check_value(PASSTHROUGH_FIELDS[key], key, value, f"{path}.{key}"))

    upstream = config.get("upstream")
    if not isinstance(upstream, str) or not upstream.strip():
        issues.append(_error(f"{path}.upstream", "缺少 upstream，透传路由不会生效"))

    prefix = config.get("prefix")
    if prefix is not None:
        if not isinstance(prefix, str) or not prefix.startswith("/proxy/"):
            issues.append(_error(f"{path}.prefix", "prefix 必须以 /proxy/ 开头"))

    if not config.get("apiKey"):
        issues.append(_warning(f"{path}.apiKey", "没有配置 apiKey：上游请求不会带网关配置的鉴权"))

    forbidden_headers = {"authorization", "cookie", "host", "content-length", "connection",
                         "transfer-encoding", "accept-encoding", "proxy-authorization"}
    for index, header in enumerate(config.get("forward_headers") or []):
        if isinstance(header, str) and header.strip().lower() in forbidden_headers:
            issues.append(_error(
                f"{path}.forward_headers[{index}]",
                f"请求头 {header} 永远不允许透传到上游（属于网关凭据或由 aiohttp 重新生成）",
            ))

    for header in (config.get("extra_headers") or {}):
        if isinstance(header, str) and header.strip().lower() in forbidden_headers:
            issues.append(_error(
                f"{path}.extra_headers.{header}",
                f"请求头 {header} 不能由透传配置固定写入（由运行时重新生成）",
            ))

    dimensions = config.get("dimensions")
    if dimensions is not None:
        if not isinstance(dimensions, dict):
            issues.append(_error(f"{path}.dimensions", "dimensions 必须是对象"))
        else:
            if len(dimensions) > 32:
                issues.append(_error(f"{path}.dimensions", "dimensions 最多 32 个"))
            for dimension, expression in dimensions.items():
                key = str(dimension)
                if not _PASSTHROUGH_DIMENSION_RE.match(key):
                    issues.append(_error(f"{path}.dimensions.{key}", f"维度名不合法: {key}"))
                elif key in _PASSTHROUGH_RESERVED_DIMENSIONS:
                    issues.append(_error(f"{path}.dimensions.{key}", f"维度名 {key} 与审计核心字段重名"))
                if not isinstance(expression, str) or not _PASSTHROUGH_EXPRESSION_RE.match(expression.strip()):
                    issues.append(_error(
                        f"{path}.dimensions.{key}",
                        "维度表达式必须是 body./query./header./path. 开头",
                    ))
    return issues


def check_document(config: Any) -> List[Issue]:
    """整份配置文件的校验：顶层保留段 + 每个平台"""
    if not isinstance(config, dict):
        return [_error("", "配置根节点必须是对象")]

    issues: List[Issue] = []

    platform_names = {name for name in config if name not in RESERVED_TOP_KEYS}

    aliases = config.get("all_aliases") or {}
    if aliases:
        if not isinstance(aliases, dict):
            issues.append(_error("all_aliases", "all_aliases 必须是对象"))
        else:
            for alias_name, selectors in aliases.items():
                if not valid_name(alias_name):
                    issues.append(_error(f"all_aliases.{alias_name}", f"别名不合法: {alias_name}"))
                issues.extend(check_alias_selectors(selectors, f"all_aliases.{alias_name}"))
            issues.extend(check_alias_targets(aliases, platform_names))

    audit = config.get("audit")
    if audit is not None:
        issues.extend(check_audit(audit))

    passthrough = config.get("passthrough")
    if passthrough is not None:
        issues.extend(check_passthrough(passthrough))

    for platform_name, platform_config in config.items():
        if platform_name in RESERVED_TOP_KEYS:
            continue
        issues.extend(check_platform(platform_name, platform_config))

    return issues


def split_issues(issues: List[Issue]) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """拆成 (errors, warnings) 两个可直接返回给前端的列表"""
    errors = [issue.to_dict() for issue in issues if issue.level == "error"]
    warnings = [issue.to_dict() for issue in issues if issue.level == "warning"]
    return errors, warnings


def env_status(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    列出配置引用的环境变量与是否已设置

    只报告「是否存在」，绝不返回密钥内容。
    """
    report: List[Dict[str, Any]] = []
    seen = set()

    for platform_name, platform_config in config.items():
        if platform_name in RESERVED_TOP_KEYS or not isinstance(platform_config, dict):
            continue
        api_key = platform_config.get("apiKey")
        placeholder = _ENV_PLACEHOLDER_RE.match(api_key) if isinstance(api_key, str) else None

        for name in _referenced_env_names(platform_name, placeholder):
            if name in seen:
                continue
            seen.add(name)
            report.append({
                "env": name,
                "platform": platform_name,
                "source": "yaml_placeholder" if placeholder else "platform_name_convention",
                "set": bool(os.getenv(name)),
            })
    return report


# ---------- 内部 ----------


def _referenced_env_names(platform_name: str, placeholder: Optional[re.Match]) -> List[str]:
    """一个平台可能用到的环境变量名：yaml 占位符 + 平台名约定"""
    names: List[str] = []
    if placeholder:
        var_name = placeholder.group(1)
        names.append(var_name)
        if not _ENV_NAME_RE.match(var_name):
            names.pop()
    names.append(f"{platform_name.upper()}_API_KEY")
    return [name for name in names if _ENV_NAME_RE.match(name)]


def _check_url(value: Any, path: str) -> List[Issue]:
    if not isinstance(value, str) or not value.strip():
        return [_error(path, "baseUrl 必须是非空字符串")]
    if not value.startswith(("http://", "https://")):
        return [_error(path, 'baseUrl 必须以 http:// 或 https:// 开头')]
    return []


def _check_secret(value: Any, path: str) -> List[Issue]:
    """apiKey 允许裸串或 ${VAR} 占位符；占位符缺失时只告警（运行时环境变量可能已经设置）"""
    if not isinstance(value, str) or not value.strip():
        return [_error(path, "apiKey 必须是非空字符串或 ${ENV_VAR} 占位符")]

    placeholder = _ENV_PLACEHOLDER_RE.match(value.strip())
    if placeholder:
        var_name = placeholder.group(1)
        if not _ENV_NAME_RE.match(var_name):
            return [_error(path, f"环境变量名不合法: {var_name}")]
        if not os.getenv(var_name):
            return [_warning(path, f"环境变量 {var_name} 当前未设置，启动时该平台可能被判为无效")]
    return []


def _check_number(value: Any, path: str, field: str, integer: bool, minimum: float) -> List[Issue]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return [_error(path, f"{field} 必须是数字")]
    maximum = _limits_max(field)
    if value < minimum or value > maximum:
        return [_error(path, f"{field} 必须在 {minimum} 到 {maximum} 之间{_limit_note(field)}")]
    if integer and float(value) != int(value):
        return [_error(path, f"{field} 必须是整数")]
    return []


def _check_list_str(value: Any, path: str, field: str) -> List[Issue]:
    if not isinstance(value, list):
        return [_error(path, f"{field} 必须是数组")]
    return [
        _error(f"{path}[{index}]", "必须是字符串")
        for index, item in enumerate(value)
        if not isinstance(item, str) or not item.strip()
    ]


def _check_map_str(value: Any, path: str, field: str) -> List[Issue]:
    """键值对字段：键和值都必须是字符串（维度表达式由 check_passthrough 单独校验）"""
    if not isinstance(value, dict):
        return [_error(path, f"{field} 必须是对象")]
    issues: List[Issue] = []
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            issues.append(_error(f"{path}", "键必须是非空字符串"))
        if not isinstance(item, str):
            issues.append(_error(f"{path}.{key}", "取值必须是字符串"))
    return issues


def _check_model_list(value: Any, path: str) -> List[Issue]:
    if not isinstance(value, list):
        return [_error(path, "models 必须是数组")]
    issues: List[Issue] = []
    for index, item in enumerate(value):
        issues.extend(check_model_entry(item, f"{path}[{index}]"))
    return issues


def _check_plugin(value: Any, path: str) -> List[Issue]:
    if not isinstance(value, dict):
        return [_error(path, "plugin 必须是对象")]
    issues: List[Issue] = []
    if not isinstance(value.get("code"), str) or not value["code"].strip():
        issues.append(_error(f"{path}.code", "plugin.code 必须是非空字符串"))
    args = value.get("args", {})
    if args and not isinstance(args, dict):
        issues.append(_error(f"{path}.args", "plugin.args 必须是对象"))
    if "scrape_url" in (args or {}) and not isinstance(args["scrape_url"], str):
        issues.append(_error(f"{path}.args.scrape_url", "scrape_url 必须是字符串"))
    return issues


def _check_platform_completeness(name: str, config: Dict[str, Any], path: str) -> List[Issue]:
    """复刻 ConfigLoader 的跳过条件：让用户在保存前就看到「这个平台其实不会生效」"""
    issues: List[Issue] = []
    plugin = config.get("plugin")
    is_scraper = isinstance(plugin, dict) and "scrape_url" in (plugin.get("args") or {})

    if not config.get("baseUrl"):
        issues.append(_error(f"{path}.baseUrl", "缺少 baseUrl，启动时该平台会被跳过"))
    if not is_scraper and not config.get("apiKey"):
        issues.append(_error(f"{path}.apiKey", "缺少 apiKey 且不是爬虫模式，启动时该平台会被跳过"))
    if not plugin and not config.get("models"):
        issues.append(_warning(path, "既没有 plugin 也没有 models：启动时该平台会被跳过"))

    models = config.get("models")
    if isinstance(models, list):
        names = [item.get("name") or item.get("model") for item in models if isinstance(item, dict)]
        names += [item for item in models if isinstance(item, str)]
        duplicates = {item for item in names if item and list(names).count(item) > 1}
        for duplicate in sorted(duplicates):
            issues.append(_warning(f"{path}.models", f"模型 {duplicate} 重复声明，实际只会调用一次"))
    return issues


def _limits_min(field: str) -> float:
    return _LIMITS.get(field, (float("-inf"),))[0]


def _limits_max(field: str) -> float:
    return _LIMITS.get(field, (float("-inf"), float("inf")))[1]


def _limit_note(field: str) -> str:
    """边界背后的原因，没有就返回空串"""
    return _LIMIT_NOTES.get(field, "")


def _error(path: str, message: str) -> Issue:
    return Issue("error", path, message)


def _warning(path: str, message: str) -> Issue:
    return Issue("warning", path, message)
