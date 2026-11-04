"""
配置补丁 - 把「改一个字段」这样的意图应用到保留注释的文档上

补丁只有 10 种操作，前端与 CLI 共用同一套：
    set_platform_field   改平台级字段
    unset_platform_field 删除平台级字段（回落到全局默认）
    add_platform         新增平台
    remove_platform      删除平台
    add_model            新增静态模型条目
    remove_model         删除静态模型条目
    set_model_field      改模型级覆盖字段（字符串条目自动升级为对象条目）
    unset_model_field    删除模型级覆盖字段（回落到平台级取值）
    set_alias_selectors  覆盖别名指向的 selector 列表
    set_audit_field      改审计段字段
    add_passthrough      新增透传路由
    remove_passthrough   删除透传路由
    set_passthrough_field 改透传字段
    unset_passthrough_field 删除透传字段

约束：
- 只在内存副本上操作，写盘交给 ConfigWriter
- 爬虫平台由插件灌入模型，不能当静态条目删除，只能进 blacklist
- 每一步都记录 before/after，便于前端展示与审计留痕
"""
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from openai_proxy.config_store import comments, schema
from openai_proxy.config_store.document import ConfigDocument

# 支持的补丁操作
OPS = (
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
    "add_passthrough",
    "remove_passthrough",
    "set_passthrough_field",
    "unset_passthrough_field",
)

# 不允许通过界面增删的结构字段（增删走各自的专用操作）
_PLATFORM_FIELD_OPS = {
    "models": "add_model / remove_model",
    "plugin": "插件结构变化需要重启，界面不提供增删",
}

# 模型级不允许覆盖的字段（连接信息由平台统一提供）
_MODEL_FORBIDDEN_FIELDS = ("baseUrl", "apiKey", "models", "plugin", "blacklist", "name")


class PatchError(Exception):
    """补丁本身不成立（找不到目标、字段不允许等）"""

    def __init__(self, message: str, path: str = ""):
        super().__init__(message)
        self.message = message
        self.path = path

    def to_dict(self) -> Dict[str, str]:
        return {"level": "error", "path": self.path, "message": self.message}


class PatchResult:
    """一次补丁的结果"""

    def __init__(
        self,
        document: ConfigDocument,
        changes: List[Dict[str, Any]],
        issues: List[schema.Issue],
        version_before: str = "",
    ):
        self.document = document
        self.changes = changes
        self.issues = issues
        self.version_before = version_before

    @property
    def errors(self) -> List[Dict[str, str]]:
        return [issue.to_dict() for issue in self.issues if issue.level == "error"]

    @property
    def valid(self) -> bool:
        """没有 error 才允许写盘"""
        return not self.errors

    @property
    def warnings(self) -> List[Dict[str, str]]:
        return [issue.to_dict() for issue in self.issues if issue.level == "warning"]

    def summary(self) -> Dict[str, Any]:
        """给接口/CLI 直接返回的摘要"""
        return {
            "changes": self.changes,
            "errors": self.errors,
            "warnings": self.warnings,
            "valid": not self.errors,
            "version_before": self.version_before,
            # 内存文档被改过之后 version 仍然代表旧内容，写盘后的版本要看序列化结果
            "version_after": self.document.dump_version,
        }


def apply_patch(source: ConfigDocument, ops: List[Dict[str, Any]]) -> PatchResult:
    """
    在源文档的副本上依次应用补丁（源文档本身不被修改）

    Returns:
        PatchResult：新文档 + 变更列表 + 校验结果；任一操作失败即整体放弃，
        由调用方决定是否写盘，保证「要么全改、要么不改」。
    """
    document = ConfigDocument.from_text(source.path, source.text)
    changes: List[Dict[str, Any]] = []

    for index, op in enumerate(ops):
        try:
            changes.extend(_apply_one(document, op, index))
        except PatchError as exc:
            # 补丁失败时不做部分写盘：把补丁错误并入整份文档的校验结果
            issues = document.validate() + [schema.Issue("error", exc.path, exc.message)]
            return PatchResult(document, changes, issues, source.version)

    issues = document.validate() + _serialize_check(document) + _comment_warnings(source, document, changes)
    return PatchResult(document, changes, issues, source.version)


def _comment_warnings(source: ConfigDocument, document: ConfigDocument, changes: List[Dict[str, Any]]) -> List[schema.Issue]:
    """
    把「改动顺带删掉的注释」如实报告出来，别让用户以为注释还在

    两类来源：
    - 补丁自己知道丢的是哪条（删条目、条目退回字符串写法），带准确路径
    - 覆盖整段（别名 selector）时注释挂在被清掉的条目上，只能靠前后对比发现
    """
    issues = [
        schema.Issue(
            "warning",
            str(change.get("path") or ""),
            f"该条目附带的注释已随改动删除: {change['lost_comment']}",
        )
        for change in changes
        if change.get("lost_comment")
    ]

    reported = {str(change.get("lost_comment")) for change in changes if change.get("lost_comment")}
    remaining = Counter(_comment_lines(document.dump()))

    for line in _comment_lines(source.text):
        if line in reported:
            continue
        if remaining[line] > 0:
            remaining[line] -= 1
            continue
        issues.append(schema.Issue("warning", "", f"改动后这条注释消失了: {line}"))

    # 同一条注释可能被多处引用，只报一次
    seen: List[schema.Issue] = []
    unique = set()
    for issue in issues:
        if issue.message in unique:
            continue
        unique.add(issue.message)
        seen.append(issue)
    return seen


def _comment_lines(text: str) -> List[str]:
    """提取文件里出现过的注释内容（去掉缩进与 # 号），用于前后对比"""
    lines: List[str] = []
    for raw in text.splitlines():
        position = raw.find("#")
        if position < 0:
            continue
        content = raw[position:].strip().lstrip("#").strip()
        if content:
            lines.append(content)
    return lines


def _serialize_check(document: ConfigDocument) -> List[schema.Issue]:
    """
    写回前自检：序列化再读回来，内容必须一模一样

    注释搬运出错通常表现为「读回来少了个模型」或「格式不稳定」，
    这里拦下来，宁可拒绝写盘也不写出语义变了的配置文件。
    """
    try:
        text = document.dump()
    except Exception as exc:  # noqa: BLE001 - 序列化失败必须拒绝写盘
        return [schema.Issue("error", "", f"配置序列化失败，已拒绝写盘: {exc}")]

    again = ConfigDocument.from_text(document.path, text)
    if _plain(again.raw) != _plain(document.raw):
        return [schema.Issue("error", "", "配置写回后内容与预期不一致，已拒绝写盘")]
    if again.dump() != text:
        return [schema.Issue("error", "", "配置格式不稳定（重复保存会得到不同结果），已拒绝写盘")]
    return []


# ---------- 各操作 ----------


def _apply_one(document: ConfigDocument, op: Dict[str, Any], index: int) -> List[Dict[str, Any]]:
    name = op.get("op")
    if name not in OPS:
        raise PatchError(f"不支持的操作: {name}（可用: {', '.join(OPS)}）", path=f"ops[{index}]")

    handlers = {
        "set_platform_field": _set_platform_field,
        "unset_platform_field": _unset_platform_field,
        "add_platform": _add_platform,
        "remove_platform": _remove_platform,
        "add_model": _add_model,
        "remove_model": _remove_model,
        "set_model_field": _set_model_field,
        "unset_model_field": _unset_model_field,
        "set_alias_selectors": _set_alias_selectors,
        "set_audit_field": _set_audit_field,
        "add_passthrough": _add_passthrough,
        "remove_passthrough": _remove_passthrough,
        "set_passthrough_field": _set_passthrough_field,
        "unset_passthrough_field": _unset_passthrough_field,
    }
    return handlers[name](document, op)


def _set_platform_field(document: ConfigDocument, op: Dict[str, Any]) -> List[Dict[str, Any]]:
    platform_name, field = _require(op, "platform", "field")
    platform = _require_platform(document, platform_name)
    _check_platform_field(field, platform_name)

    value = op.get("value")
    _validate_field(schema.PLATFORM_FIELDS, field, value, f"platforms.{platform_name}.{field}")

    before = platform.get(field)
    platform[field] = normalize_value(value)
    return [_change(op, f"platforms.{platform_name}.{field}", before, value)]


def _unset_platform_field(document: ConfigDocument, op: Dict[str, Any]) -> List[Dict[str, Any]]:
    platform_name, field = _require(op, "platform", "field")
    platform = _require_platform(document, platform_name)
    _check_platform_field(field, platform_name)

    if field not in platform:
        raise PatchError(f"平台 {platform_name} 没有字段 {field}", path=f"platforms.{platform_name}.{field}")

    before = platform.get(field)
    del platform[field]
    return [_change(op, f"platforms.{platform_name}.{field}", before, None)]


def _add_platform(document: ConfigDocument, op: Dict[str, Any]) -> List[Dict[str, Any]]:
    platform_name = _require(op, "platform")
    config = op.get("config")
    if not isinstance(config, dict):
        raise PatchError("新增平台必须提供 config 对象", path=f"platforms.{platform_name}")
    if not schema.valid_name(platform_name):
        raise PatchError(f"平台名不合法: {platform_name}", path=f"platforms.{platform_name}")
    if document.platform(platform_name) is not None:
        raise PatchError(f"平台 {platform_name} 已存在", path=f"platforms.{platform_name}")

    _validate_platform_config(config, platform_name)
    document.raw[platform_name] = normalize_value(config)
    return [_change(op, f"platforms.{platform_name}", None, config)]


def _remove_platform(document: ConfigDocument, op: Dict[str, Any]) -> List[Dict[str, Any]]:
    platform_name = _require(op, "platform")
    platform = _require_platform(document, platform_name)

    # 别名指向该平台的 selector 会失效：只告警，由校验环节给出
    before = {key: value for key, value in platform.items()}
    del document.raw[platform_name]
    return [_change(op, f"platforms.{platform_name}", before, None)]


def _add_model(document: ConfigDocument, op: Dict[str, Any]) -> List[Dict[str, Any]]:
    platform_name, model_name = _require(op, "platform", "model")
    platform = _require_platform(document, platform_name)
    if not schema.valid_model_name(model_name):
        raise PatchError(f"模型名不合法: {model_name}", path=f"platforms.{platform_name}.models")

    overrides = op.get("overrides") or {}
    if not isinstance(overrides, dict):
        raise PatchError("overrides 必须是对象", path=f"platforms.{platform_name}.models.{model_name}")
    _validate_overrides(overrides, platform_name, model_name)

    models = platform.get("models")
    if models is None or not isinstance(models, list):
        models = _new_models_list(document, platform, platform_name)

    existing, _ = document.find_model(platform_name, model_name)
    if existing is not None:
        raise PatchError(f"模型 {model_name} 已存在于平台 {platform_name}", path=f"platforms.{platform_name}.models.{model_name}")

    # 无覆盖时用字符串写法，并保持与文件中其他条目一致的双引号风格
    entry: Any = quoted(model_name) if not overrides else _model_map(model_name, overrides)
    models.append(entry)
    return [_change(op, f"platforms.{platform_name}.models.{model_name}", None, _plain(entry))]


def _remove_model(document: ConfigDocument, op: Dict[str, Any]) -> List[Dict[str, Any]]:
    platform_name, model_name = _require(op, "platform", "model")
    platform = _require_platform(document, platform_name)

    plugin = platform.get("plugin")
    if isinstance(plugin, dict) and "scrape_url" in (plugin.get("args") or {}):
        raise PatchError(
            f"平台 {platform_name} 的模型由插件爬取，不能删除条目；请改用 blacklist 屏蔽该模型",
            path=f"platforms.{platform_name}.blacklist",
        )

    entry, position = document.find_model(platform_name, model_name)
    if entry is None:
        raise PatchError(f"模型 {model_name} 不在平台 {platform_name} 的静态条目里", path=f"platforms.{platform_name}.models")

    models = platform.get("models")
    before = _plain(entry)

    # 注释挂在被删条目上：删掉之后必须把其余条目的注释下标修正，否则注释会串到别的模型上
    lost_comment = comments.pop_comment(models, position)
    del models[position]
    comments.shift_item_comments(models, position)

    change = _change(op, f"platforms.{platform_name}.models.{model_name}", before, None)
    if lost_comment:
        change["lost_comment"] = lost_comment
    return [change]


def _set_model_field(document: ConfigDocument, op: Dict[str, Any]) -> List[Dict[str, Any]]:
    platform_name, model_name, field = _require(op, "platform", "model", "field")
    platform = _require_platform(document, platform_name)
    _check_model_field(field, platform_name, model_name)

    value = op.get("value")
    _validate_field(schema.MODEL_FIELDS, field, value, f"platforms.{platform_name}.models.{model_name}.{field}")

    entry, position = document.find_model(platform_name, model_name)
    if entry is None:
        raise PatchError(f"模型 {model_name} 不在平台 {platform_name} 的静态条目里", path=f"platforms.{platform_name}.models")

    models = platform.get("models")
    path = f"platforms.{platform_name}.models.{model_name}.{field}"

    if isinstance(entry, dict):
        before = entry.get(field)
        entry[field] = normalize_value(value)
        return [_change(op, path, before, value)]

    # 字符串条目升级为对象条目：原来没有任何模型级覆盖
    upgraded = _model_map(model_name, {field: value})
    # 挂在旧条目上的注释保不住位置：留在原处会写出错乱的排版，
    # 搬到新字段行尾又会让注释看起来像在解释这个字段，所以直接丢弃并如实报告
    lost = comments.pop_comment(models, position)
    models[position] = upgraded

    change = _change(op, path, None, value)
    if lost:
        change["lost_comment"] = lost
    return [change]


def _unset_model_field(document: ConfigDocument, op: Dict[str, Any]) -> List[Dict[str, Any]]:
    platform_name, model_name, field = _require(op, "platform", "model", "field")
    _check_model_field(field, platform_name, model_name)

    platform = _require_platform(document, platform_name)
    entry, position = document.find_model(platform_name, model_name)
    if entry is None:
        raise PatchError(f"模型 {model_name} 不在平台 {platform_name} 的静态条目里", path=f"platforms.{platform_name}.models")
    if not isinstance(entry, dict) or field not in entry:
        raise PatchError(f"模型 {model_name} 没有模型级字段 {field}", path=f"platforms.{platform_name}.models.{model_name}.{field}")

    models = platform.get("models")
    before = entry.get(field)
    # 字段上的注释先摘下来：条目退回字符串写法后没有可以挂注释的键
    lost = comments.pop_comment(entry, field)
    del entry[field]

    remaining = {key: value for key, value in entry.items() if key not in ("name", "model")}
    path = f"platforms.{platform_name}.models.{model_name}.{field}"
    change = _change(op, path, before, None)

    if not remaining:
        # 没有任何覆盖了就退回字符串写法，保持文件简洁
        models[position] = quoted(model_name)
        if lost:
            change["lost_comment"] = lost
    return [change]


def _set_alias_selectors(document: ConfigDocument, op: Dict[str, Any]) -> List[Dict[str, Any]]:
    alias_name = _require(op, "alias")
    selectors = op.get("selectors")
    if not schema.valid_name(alias_name):
        raise PatchError(f"别名不合法: {alias_name}", path=f"all_aliases.{alias_name}")
    # 别名指向的 selector 有专门的校验规则（all / 平台名 / 平台名|模型名）
    issues = schema.check_alias_selectors(selectors, f"all_aliases.{alias_name}")
    errors = [issue for issue in issues if issue.level == "error"]
    if errors:
        raise PatchError(errors[0].message, path=errors[0].path)

    aliases = document.raw.get("all_aliases")
    if aliases is None or not isinstance(aliases, dict):
        aliases = _new_aliases(document)

    before = [str(item) for item in (aliases.get(alias_name) or [])]
    replace_sequence(aliases, alias_name, selectors)
    return [_change(op, f"all_aliases.{alias_name}", before, [str(item) for item in selectors])]


def _set_audit_field(document: ConfigDocument, op: Dict[str, Any]) -> List[Dict[str, Any]]:
    field = _require(op, "field")
    if field not in schema.AUDIT_FIELDS:
        raise PatchError(
            f"不支持的审计字段: {field}（可用: {', '.join(sorted(schema.AUDIT_FIELDS))}）", path=f"audit.{field}"
        )
    value = op.get("value")
    _validate_field(schema.AUDIT_FIELDS, field, value, f"audit.{field}")

    audit = document.raw.get("audit")
    if audit is None or not isinstance(audit, dict):
        audit = _new_audit(document)

    before = audit.get(field)
    audit[field] = value
    return [_change(op, f"audit.{field}", before, value)]


# ---------- passthrough 段 ----------


def _add_passthrough(document: ConfigDocument, op: Dict[str, Any]) -> List[Dict[str, Any]]:
    """新增一条透传路由"""
    name = _require(op, "name")
    config = op.get("config")
    if not isinstance(config, dict):
        raise PatchError("新增透传路由必须提供 config 对象", path=f"passthrough.{name}")
    if not schema.valid_name(name):
        raise PatchError(f"透传路由名不合法: {name}", path=f"passthrough.{name}")
    if document.passthrough_route(name) is not None:
        raise PatchError(f"透传路由 {name} 已存在", path=f"passthrough.{name}")

    _validate_passthrough_config(name, config)
    section = _require_passthrough_section(document)
    section[name] = normalize_value(config)
    return [_change(op, f"passthrough.{name}", None, config)]


def _remove_passthrough(document: ConfigDocument, op: Dict[str, Any]) -> List[Dict[str, Any]]:
    """删除一条透传路由"""
    name = _require(op, "name")
    config = document.passthrough_route(name)
    if config is None:
        raise PatchError(f"透传路由不存在: {name}", path=f"passthrough.{name}")

    before = {key: value for key, value in config.items()} if isinstance(config, dict) else config
    del document.passthrough[name]
    return [_change(op, f"passthrough.{name}", before, None)]


def _set_passthrough_field(document: ConfigDocument, op: Dict[str, Any]) -> List[Dict[str, Any]]:
    """修改透传路由的一个字段"""
    name, field = _require(op, "name", "field")
    route = _require_passthrough(document, name)
    if field not in schema.PASSTHROUGH_FIELDS:
        raise PatchError(
            f"不支持的透传字段: {field}（可用: {', '.join(sorted(schema.PASSTHROUGH_FIELDS))}）",
            path=f"passthrough.{name}.{field}",
        )

    value = op.get("value")
    _validate_field(schema.PASSTHROUGH_FIELDS, field, value, f"passthrough.{name}.{field}")

    before = route.get(field)
    route[field] = normalize_value(value)
    _validate_passthrough_config(name, route)
    return [_change(op, f"passthrough.{name}.{field}", before, value)]


def _unset_passthrough_field(document: ConfigDocument, op: Dict[str, Any]) -> List[Dict[str, Any]]:
    """删除透传路由的一个字段（回落到默认值）"""
    name, field = _require(op, "name", "field")
    route = _require_passthrough(document, name)
    if field not in schema.PASSTHROUGH_FIELDS:
        raise PatchError(
            f"不支持的透传字段: {field}（可用: {', '.join(sorted(schema.PASSTHROUGH_FIELDS))}）",
            path=f"passthrough.{name}.{field}",
        )
    if field not in route:
        raise PatchError(f"透传路由 {name} 没有字段 {field}", path=f"passthrough.{name}.{field}")

    before = route.get(field)
    del route[field]
    return [_change(op, f"passthrough.{name}.{field}", before, None)]


# ---------- 校验与工具 ----------


def _require(op: Dict[str, Any], *keys: str) -> Tuple[Any, ...]:
    """取补丁的必填参数"""
    values = []
    for key in keys:
        value = op.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise PatchError(f"操作 {op.get('op')} 缺少参数: {key}")
        values.append(value)
    return tuple(values) if len(values) > 1 else values[0]


def _require_platform(document: ConfigDocument, platform_name: str) -> Any:
    platform = document.platform(platform_name)
    if platform is None:
        raise PatchError(f"平台不存在: {platform_name}", path=f"platforms.{platform_name}")
    if not isinstance(platform, dict):
        raise PatchError(f"平台 {platform_name} 的配置不是对象", path=f"platforms.{platform_name}")
    return platform


def _check_platform_field(field: str, platform_name: str) -> None:
    if field in _PLATFORM_FIELD_OPS:
        raise PatchError(_PLATFORM_FIELD_OPS[field], path=f"platforms.{platform_name}.{field}")
    if field not in schema.PLATFORM_FIELDS:
        editable = sorted(set(schema.PLATFORM_FIELDS) - set(_PLATFORM_FIELD_OPS))
        raise PatchError(
            f"不支持的平台字段: {field}（可用: {', '.join(editable)}）",
            path=f"platforms.{platform_name}.{field}",
        )


def _check_model_field(field: str, platform_name: str, model_name: str) -> None:
    path = f"platforms.{platform_name}.models.{model_name}.{field}"
    if field in _MODEL_FORBIDDEN_FIELDS:
        raise PatchError(f"字段 {field} 由平台统一提供，不能在模型级覆盖", path=path)
    if field not in schema.MODEL_FIELDS:
        raise PatchError(
            f"不支持的模型字段: {field}（可用: {', '.join(sorted(schema.MODEL_FIELDS))}）", path=path
        )


def _validate_field(field_kinds: Dict[str, str], field: str, value: Any, path: str) -> None:
    issues = schema.check_value(field_kinds[field], field, value, path)
    errors = [issue for issue in issues if issue.level == "error"]
    if errors:
        raise PatchError(errors[0].message, path=path)


def _validate_overrides(overrides: Dict[str, Any], platform_name: str, model_name: str) -> None:
    for field, value in overrides.items():
        _check_model_field(field, platform_name, model_name)
        _validate_field(schema.MODEL_FIELDS, field, value, f"platforms.{platform_name}.models.{model_name}.{field}")


def _validate_platform_config(config: Dict[str, Any], platform_name: str) -> None:
    issues = schema.check_platform(platform_name, config)
    errors = [issue for issue in issues if issue.level == "error"]
    if errors:
        raise PatchError(errors[0].message, path=errors[0].path)


def _model_map(model_name: str, overrides: Dict[str, Any]) -> Any:
    """构造对象写法的模型条目（模型名用双引号，与现有文件的书写风格一致）"""
    entry = CommentedMap()
    entry["name"] = DoubleQuotedScalarString(model_name)
    for field, value in overrides.items():
        entry[field] = normalize_value(value)
    return entry


def _new_models_list(document: ConfigDocument, platform: Any, platform_name: str) -> Any:
    """给没有 models 键的平台新建块式列表"""
    models = block_sequence()
    platform["models"] = models
    return models


def block_sequence(values: Optional[List[Any]] = None) -> CommentedSeq:
    """块式列表：写出 `- 值` 的形式，而不是流式的 [值, 值]"""
    sequence = CommentedSeq()
    sequence.fa.set_block_style()
    for value in values or []:
        # 列表里也可能混着对象条目（模型条目带覆盖项），一律走同一套写法
        sequence.append(normalize_value(value) if isinstance(value, (dict, list)) else quoted(value))
    return sequence


def replace_sequence(container: Any, key: Any, values: List[Any]) -> Any:
    """
    替换某个键下的列表内容

    能原地清空再填值就不要换新对象：换对象会让 ruamel 丢掉这一段的排版信息，
    写出来的缩进会变成 `-` 挤在键名后面那种难看的形状。
    """
    existing = container.get(key)
    if isinstance(existing, list):
        del existing[:]
        existing.extend(quoted_list(values))
        return existing
    sequence = block_sequence(values)
    container[key] = sequence
    return sequence


def normalize_value(value: Any) -> Any:
    """
    把补丁传进来的普通 Python 值转成 round-trip 写法

    列表转成块式、字符串带双引号，否则新写出的行会变成 `models: [a, b]` 这种
    与文件里其它地方不一致的流式风格。
    """
    if isinstance(value, dict):
        result = CommentedMap()
        for key, item in value.items():
            result[key] = normalize_value(item)
        return result
    if isinstance(value, list):
        return block_sequence(value)
    return quoted(value)


def _new_aliases(document: ConfigDocument) -> Any:
    """新建 all_aliases 段"""
    aliases = CommentedMap()
    document.raw["all_aliases"] = aliases
    return aliases


def _new_audit(document: ConfigDocument) -> Any:
    """新建顶层 audit 段"""
    audit = CommentedMap()
    document.raw["audit"] = audit
    return audit


def _require_passthrough_section(document: ConfigDocument) -> Any:
    """取顶层 passthrough 段，不存在时建一个块式映射"""
    if not isinstance(document.raw, dict):
        document.raw = CommentedMap()
    section = document.raw.get("passthrough")
    if section is None or not isinstance(section, dict):
        section = CommentedMap()
        document.raw["passthrough"] = section
    return section


def _require_passthrough(document: ConfigDocument, name: str) -> Any:
    """取一条透传路由，不存在时给出明确错误"""
    route = document.passthrough_route(name)
    if route is None:
        raise PatchError(f"透传路由不存在: {name}", path=f"passthrough.{name}")
    if not isinstance(route, dict):
        raise PatchError(f"透传路由 {name} 的配置不是对象", path=f"passthrough.{name}")
    return route


def _validate_passthrough_config(name: str, config: Dict[str, Any]) -> None:
    """复用整份文档的校验规则，只报第一条错误"""
    issues = schema.check_passthrough({name: config})
    errors = [issue for issue in issues if issue.level == "error"]
    if errors:
        raise PatchError(errors[0].message, path=errors[0].path)


def quoted(value: Any) -> Any:
    """字符串转双引号写法，保证新写入的行与原文件风格一致"""
    return DoubleQuotedScalarString(str(value)) if isinstance(value, str) else value


def quoted_list(values: List[Any]) -> List[Any]:
    """字符串列表转成双引号写法，避免新写出的行与文件里其他行风格不一致"""
    return [quoted(value) for value in values]


def _plain(value: Any) -> Any:
    """把 ruamel 的容器转成普通 Python 对象，便于放进 JSON 响应"""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
        return value
    return str(value) if value is not None else None


def _change(op: Dict[str, Any], path: str, before: Any, after: Any) -> Dict[str, Any]:
    """一条变更记录"""
    return {
        "op": op.get("op"),
        "path": path,
        "before": _plain(before),
        "after": _plain(after),
    }
