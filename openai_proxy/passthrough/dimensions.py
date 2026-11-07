"""
透传维度提取 - 从请求里取出配置声明的参数

支持四种来源：
    body.model               JSON 请求体字段（支持 data.items[0].name）
    query.size               查询参数
    header.x-model           请求头（大小写不敏感）
    path.0                   前缀之后的第 1 段路径

约定：
- 缺失值记空串，不编造默认值
- 只提取配置声明的字段，绝不记录整个请求体
- 单值截断到 200 字符，避免异常请求撑大维度基数
"""
import json
import re
from typing import Any, Dict, Iterable, Optional

from openai_proxy.passthrough.config import DimensionSpec

MAX_DIMENSION_VALUE = 200

_SEGMENT_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


class DimensionExtractor:
    """按一条路由的维度声明提取请求参数"""

    def __init__(self, specs: Iterable[DimensionSpec]):
        self.specs = tuple(specs)
        self._needs_body = any(spec.source == "body" for spec in self.specs)

    @property
    def needs_body(self) -> bool:
        """是否需要缓存/解析请求体"""
        return self._needs_body

    def extract(
        self,
        body: bytes,
        query: Dict[str, str],
        headers: Any,
        remainder_path: str,
    ) -> Dict[str, str]:
        """提取全部维度；任何单个维度失败只影响它自己"""
        payload = self._parse_body(body) if self._needs_body else None
        segments = [item for item in remainder_path.split("/") if item]
        result: Dict[str, str] = {}
        for spec in self.specs:
            try:
                value = self._resolve(spec, payload, query, headers, segments)
            except Exception:  # noqa: BLE001 - 单个维度失败不影响转发
                value = ""
            result[spec.name] = _to_text(value)
        return result

    # ---------- 各来源 ----------

    def _resolve(
        self,
        spec: DimensionSpec,
        payload: Any,
        query: Dict[str, str],
        headers: Any,
        segments: list,
    ) -> Any:
        if spec.source == "body":
            return _dig(payload, spec.path)
        if spec.source == "query":
            return query.get(spec.path, "")
        if spec.source == "header":
            return _header_value(headers, spec.path)
        if spec.source == "path":
            try:
                index = int(spec.path)
            except (TypeError, ValueError):
                return ""
            return segments[index] if 0 <= index < len(segments) else ""
        return ""

    @staticmethod
    def _parse_body(body: bytes) -> Any:
        """只在需要 body 维度时解析 JSON；解析失败返回空对象"""
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {}


def _header_value(headers: Any, name: str) -> str:
    """请求头大小写不敏感；兼容 Starlette Headers 与普通 dict"""
    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    if getter is not None:
        value = getter(name)
        if value is not None:
            return value
    target = str(name).lower()
    try:
        for key, value in headers.items():
            if str(key).lower() == target:
                return value
    except AttributeError:
        return ""
    return ""


def _dig(payload: Any, path: str) -> Any:
    """按点路径取值，支持 a.b[0].c 与 a.0.b"""
    current = payload
    for name, index in _SEGMENT_RE.findall(path or ""):
        if index:
            position = int(index)
            if not isinstance(current, (list, tuple)) or position >= len(current):
                return ""
            current = current[position]
            continue
        if isinstance(current, dict):
            if name not in current:
                return ""
            current = current[name]
        elif isinstance(current, (list, tuple)) and name.isdigit():
            position = int(name)
            if position >= len(current):
                return ""
            current = current[position]
        else:
            return ""
    return current


def _to_text(value: Any) -> str:
    """维度值统一成截断后的字符串"""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (int, float)):
        text = str(value)
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(value)
    return text[:MAX_DIMENSION_VALUE]
