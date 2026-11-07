"""
透传路由配置 - 把 models.yaml 的 passthrough 段解析成运行期对象

设计要点：
- 透传只做「前缀重写 + 原样转发」，不参与模型选择与故障转移
- 维度是唯一可扩展的分组概念：配置里的键名就是审计与汇总里的键名
- 标准维度（platform/model/model_id/selector）由转发器给默认值，配置同名键可覆盖
- 任何非法路由只跳过自己，不阻塞服务启动
"""
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 对外前缀必须在这个命名空间下：既能避免与 /v1、/admin 冲突，也能复用转发接口的错误形状
PREFIX_ROOT = "/proxy"
DEFAULT_FORWARD_HEADERS = ("accept", "content-type", "user-agent", "accept-language")

# 这些头永远不允许从客户端透传到上游：要么由 aiohttp 重新生成，要么属于网关自身凭据
FORBIDDEN_FORWARD_HEADERS = {
    "authorization", "cookie", "host", "content-length", "connection",
    "transfer-encoding", "accept-encoding", "proxy-authorization",
    "x-gateway-user",
}

# 维度名不能和审计核心列重名，避免同一个名字在查询时有两套含义
RESERVED_DIMENSION_NAMES = {
    "attempt_key", "gateway_request_id", "api_user", "request_id", "endpoint",
    "stream", "attempt_seq", "retry_seq", "ts", "outcome", "finalized",
    "http_status", "error_category", "error_code", "disabled_model",
    "duration_ms", "probe_ms", "ttft_ms", "has_usage", "upstream_id",
    "input_tokens", "cached_tokens", "output_tokens", "output_chars",
    "throughput", "tool_call_converted", "keepalive_count", "msg_count",
    "done_sent", "aggregated",
}

_DIMENSION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_EXPRESSION_RE = re.compile(r"^(body|query|header|path)\.(.+)$")
_ENV_RE = re.compile(r"^\$\{([A-Za-z0-9_]+)\}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

DEFAULT_BODY_LIMIT = 16 * 1024 * 1024


class PassthroughConfigError(ValueError):
    """一条透传路由配置不合法"""

    def __init__(self, route: str, message: str):
        super().__init__(f"透传路由 {route}: {message}")
        self.route = route
        self.message = message


@dataclass(frozen=True)
class DimensionSpec:
    """一个维度：键名 + 取值来源"""

    name: str
    source: str          # body | query | header | path
    path: str            # 点路径或分隔符之后的字段名
    expression: str      # 原始写法，例如 body.data.model

    def describe(self) -> Dict[str, str]:
        return {"name": self.name, "from": self.source, "path": self.path}


@dataclass
class PassthroughRoute:
    """一条透传路由的不可变运行配置"""

    name: str
    prefix: str
    upstream: str
    api_key: str = ""
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    timeout: float = 300.0
    connect_timeout: float = 10.0
    enabled: bool = True
    forward_headers: Tuple[str, ...] = DEFAULT_FORWARD_HEADERS
    extra_headers: Dict[str, str] = field(default_factory=dict)
    dimensions: Tuple[DimensionSpec, ...] = ()
    body_limit: int = DEFAULT_BODY_LIMIT

    @property
    def endpoint(self) -> str:
        """审计口径里的入口名：一个透传路由一个入口"""
        return f"passthrough:{self.name}"

    def matches(self, path: str) -> Optional[str]:
        """命中前缀时返回剩余路径；未命中返回 None"""
        if path == self.prefix:
            return ""
        if path.startswith(self.prefix + "/"):
            return path[len(self.prefix):]
        return None

    def target_url(self, remainder: str, query: str = "") -> str:
        """把剩余路径拼到上游地址后面，并保留原始查询串"""
        base = self.upstream.rstrip("/")
        suffix = "/" + remainder.lstrip("/") if remainder else ""
        return f"{base}{suffix}" + (f"?{query}" if query else "")

    def default_dimensions(self) -> Dict[str, str]:
        """标准维度默认值；配置里同名的维度会覆盖它们"""
        return {
            "platform": "passthrough",
            "model": self.name,
            "model_id": self.name,
            "selector": self.name,
        }

    def describe(self) -> Dict[str, Any]:
        """管理接口展示用（不含上游地址与密钥）"""
        return {
            "route": self.name,
            "prefix": self.prefix,
            "endpoint": self.endpoint,
            "enabled": self.enabled,
            "dimensions": [spec.name for spec in self.dimensions],
        }


def parse_routes(
    raw: Optional[Dict[str, Any]],
    env_resolver: Optional[Callable[[str], str]] = None,
) -> List[PassthroughRoute]:
    """解析整个 passthrough 段；单条非法只跳过自己"""
    if not isinstance(raw, dict):
        return []

    routes: List[PassthroughRoute] = []
    seen_prefixes = set()
    for name, config in raw.items():
        try:
            route = _parse_route(str(name), config, env_resolver or _default_env_resolver)
            if route.prefix in seen_prefixes:
                raise PassthroughConfigError(route.name, f"前缀重复: {route.prefix}")
            seen_prefixes.add(route.prefix)
            routes.append(route)
        except PassthroughConfigError as exc:
            logger.error(f"跳过无效透传路由: {exc}")
        except Exception as exc:  # noqa: BLE001 - 配置错误不能阻塞启动
            logger.error(f"跳过无法解析的透传路由 {name}: {exc}")
    return routes


def _parse_route(
    name: str,
    config: Any,
    env_resolver: Callable[[str], str],
) -> PassthroughRoute:
    """解析单条路由"""
    if not _NAME_RE.match(name):
        raise PassthroughConfigError(name, "路由名不合法")
    if not isinstance(config, dict):
        raise PassthroughConfigError(name, "配置必须是对象")

    upstream = str(config.get("upstream") or "").strip()
    if not upstream.startswith(("http://", "https://")):
        raise PassthroughConfigError(name, "upstream 必须以 http:// 或 https:// 开头")

    prefix = str(config.get("prefix") or f"{PREFIX_ROOT}/{name}").strip().rstrip("/")
    if not prefix.startswith(PREFIX_ROOT + "/"):
        raise PassthroughConfigError(name, f"prefix 必须以 {PREFIX_ROOT}/ 开头")
    if prefix == PREFIX_ROOT:
        raise PassthroughConfigError(name, "prefix 不能只有 /proxy")

    dimensions = _parse_dimensions(name, config.get("dimensions"))
    forward_headers = _parse_headers(config.get("forward_headers"))
    extra_headers = _parse_extra_headers(config.get("extra_headers"))

    return PassthroughRoute(
        name=name,
        prefix=prefix,
        upstream=upstream,
        api_key=_resolve_secret(name, config.get("apiKey"), env_resolver),
        auth_header=str(config.get("auth_header") or "Authorization"),
        auth_prefix=str(config.get("auth_prefix") if config.get("auth_prefix") is not None else "Bearer "),
        timeout=_positive_float(config.get("timeout"), 300.0),
        connect_timeout=_positive_float(config.get("connect_timeout"), 10.0),
        enabled=_to_bool(config.get("enabled", True)),
        forward_headers=forward_headers,
        extra_headers=extra_headers,
        dimensions=tuple(dimensions),
        body_limit=int(config.get("body_limit") or DEFAULT_BODY_LIMIT),
    )


def _parse_dimensions(name: str, raw: Any) -> List[DimensionSpec]:
    """解析维度映射：键名 -> 取值表达式"""
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise PassthroughConfigError(name, "dimensions 必须是对象")
    if len(raw) > 32:
        raise PassthroughConfigError(name, "dimensions 最多 32 个")

    specs: List[DimensionSpec] = []
    for dimension_name, expression in raw.items():
        key = str(dimension_name).strip()
        if not _DIMENSION_NAME_RE.match(key):
            raise PassthroughConfigError(name, f"维度名不合法: {key}")
        if key in RESERVED_DIMENSION_NAMES:
            raise PassthroughConfigError(name, f"维度名 {key} 与审计核心字段重名")
        text = str(expression or "").strip()
        matched = _EXPRESSION_RE.match(text)
        if not matched:
            raise PassthroughConfigError(
                name, f"维度 {key} 的表达式必须是 body./query./header./path. 开头，收到: {text}"
            )
        specs.append(DimensionSpec(name=key, source=matched.group(1), path=matched.group(2), expression=text))
    return specs


def _parse_headers(raw: Any) -> Tuple[str, ...]:
    """请求头白名单：额外放行客户端请求头（敏感头一律过滤掉）"""
    if not isinstance(raw, list) or not raw:
        return DEFAULT_FORWARD_HEADERS
    names = [
        str(item).strip().lower() for item in raw
        if str(item).strip() and str(item).strip().lower() not in FORBIDDEN_FORWARD_HEADERS
    ]
    return tuple(dict.fromkeys(list(DEFAULT_FORWARD_HEADERS) + names))


def _parse_extra_headers(raw: Any) -> Dict[str, str]:
    """固定附加到上游请求的头（逐跳头与网关凭据相关头一律过滤）"""
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if str(key).strip() and str(key).strip().lower() not in FORBIDDEN_FORWARD_HEADERS
    }


def _resolve_secret(name: str, raw: Any, env_resolver: Callable[[str], str]) -> str:
    """密钥支持 ${VAR} 占位符，也支持按路由名约定的环境变量"""
    text = str(raw or "").strip()
    if text:
        return env_resolver(text)
    conventional = f"PASSTHROUGH_{re.sub(r'[^A-Za-z0-9]', '_', name).upper()}_API_KEY"
    return os.getenv(conventional, "")


def _default_env_resolver(value: str) -> str:
    """解析 ${VAR}；变量没设置时返回空串，避免把字面量当密钥发给上游"""
    matched = _ENV_RE.match(value.strip())
    if matched:
        return os.getenv(matched.group(1), "")
    return value


def _to_bool(raw: Any, default: bool = True) -> bool:
    """宽松解析布尔：支持 true/false/yes/no/1/0"""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _positive_float(raw: Any, default: float) -> float:
    """正数解析，非法值回落默认"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default
