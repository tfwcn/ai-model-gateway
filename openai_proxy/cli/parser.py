"""
命令行参数解析

用法一律是 `manage.py <组> <动作> [参数]`，例如：
    manage.py config document
    manage.py platform set example-platform --field weight --value 5
    manage.py model set example-platform model-a --field first_chunk_timeout --value 45
    manage.py usage summary --group-by model --since 24h --json

组：
    config   看配置、校验、备份与回滚
    platform 平台级字段
    model    模型条目与模型级覆盖
    alias    模型组别名
    audit    审计段
    usage    用量与性能统计

公共参数（--config / --backup-dir / --json / --verbose）放在组前面或动作后面都能识别，
否则用户写 `usage summary --json` 会因为参数出现在子命令之后而报错。
"""
import argparse
import sys
from typing import Any, List, Optional

from openai_proxy.cli.codes import EXIT_USAGE

# 用量统计支持的排序键与聚合维度（与 /admin/usage 接口保持一致）
SORT_CHOICES = ("requests", "p95", "duration", "ttft", "throughput", "failover", "errors", "tokens")
GROUP_CHOICES = ("model", "platform", "user", "endpoint", "selector", "outcome", "error", "day", "hour")


class CliArgumentParser(argparse.ArgumentParser):
    """用法错误统一用退出码 3：argparse 默认用 2，会和「版本冲突」撞车"""

    def error(self, message: str) -> None:  # pragma: no cover - 由 main 捕获 SystemExit
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: 参数不对: {message}\n")


# 公共参数：每个动作都挂一份，用户放在哪个位置都能用
def _add_globals(parser: argparse.ArgumentParser, suppress_defaults: bool) -> None:
    """
    挂公共参数

    子命令里的默认值必须用 SUPPRESS：argparse 会把子命令解析出的默认值写回主命名空间，
    否则 `manage.py --config /path/to/models.yaml usage stats` 里的路径会被子命令的默认值冲掉。
    """
    blank = argparse.SUPPRESS if suppress_defaults else None
    off = argparse.SUPPRESS if suppress_defaults else False

    parser.add_argument("--config", default=blank, help="配置文件路径（默认 models.yaml）")
    parser.add_argument("--backup-dir", default=blank, help="配置备份目录（默认 data/config_backups）")
    parser.add_argument("--json", action="store_true", default=off, help="以 JSON 输出，便于脚本处理")
    parser.add_argument("--verbose", action="store_true", default=off, help="显示服务日志，默认只输出命令结果")


def build_parser() -> argparse.ArgumentParser:
    """构建参数树"""
    parser = CliArgumentParser(
        prog="manage.py",
        description="AI Model Gateway 管理命令行：配置模型、查看用量与性能审计",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_globals(parser, suppress_defaults=False)
    parser.set_defaults(handler=None)
    groups = parser.add_subparsers(dest="group", metavar="<组>")

    _add_config_group(groups.add_parser("config", help="查看/校验/回滚配置"))
    _add_platform_group(groups)
    _add_model_group(groups)
    _add_alias_group(groups)
    _add_audit_group(groups)
    _add_usage_group(groups)

    return parser


# ---------- 各动作的参数 ----------


def _add_config_group(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", metavar="<动作>")

    document = _action(actions, "document", "打印当前配置（密钥只显示引用的环境变量）", "config_document")
    document.add_argument("--platform", help="只看某个平台")

    show = _action(actions, "show", "看某个平台的完整取值", "config_show")
    show.add_argument("platform")

    fields = _action(actions, "fields", "打印字段字典与结构说明（与管理台「模型配置」页同一份文档）", "config_fields")
    fields.add_argument("--scope", choices=("platform", "model", "audit"), help="只看某一组字段")

    _action(actions, "validate", "校验当前配置文件", "config_validate")
    _action(actions, "backups", "列出可回滚的备份", "config_backups")

    rollback = _action(actions, "rollback", "回滚到指定备份", "config_rollback")
    rollback.add_argument("name")
    rollback.add_argument("--no-reload", action="store_true", help="回滚后不尝试进程内重载")


def _add_platform_group(groups) -> None:
    parser = groups.add_parser("platform", help="平台级字段")
    actions = parser.add_subparsers(dest="action", metavar="<动作>")

    set_action = _action(actions, "set", "改平台字段", "platform_set")
    _add_target(set_action, platform=True)
    _add_field(set_action)
    _add_write(set_action)

    unset = _action(actions, "unset", "删除平台字段（回落到全局默认）", "platform_unset")
    _add_target(unset, platform=True)
    unset.add_argument("--field", "-f", required=True)
    _add_write(unset)

    add = _action(actions, "add", "新增平台（改完需要重启）", "platform_add")
    add.add_argument("platform")
    add.add_argument("--base-url", required=True, help="例如 https://api.example.com/v1")
    add.add_argument("--api-key", help="密钥占位符，例如 ${EXAMPLE_PLATFORM_API_KEY}")
    add.add_argument("--model", action="append", default=[], help="静态模型条目，可重复")
    add.add_argument("--weight", type=int)
    add.add_argument("--timeout", type=int)
    add.add_argument("--retry-count", type=int)
    add.add_argument("--connect-timeout", type=float)
    add.add_argument("--first-chunk-timeout", type=float)
    add.add_argument("--stream-probe-timeout", type=float)
    add.add_argument("--blacklist", action="append", default=[], help="屏蔽的模型，可重复")
    add.add_argument("--disabled", action="store_true", help="新增即停用")
    _add_write(add)

    remove = _action(actions, "remove", "删除平台（改完需要重启）", "platform_remove")
    remove.add_argument("platform")
    _add_write(remove)


def _add_model_group(groups) -> None:
    parser = groups.add_parser("model", help="模型条目与模型级覆盖")
    actions = parser.add_subparsers(dest="action", metavar="<动作>")

    add = _action(actions, "add", "新增静态模型条目", "model_add")
    _add_target(add, platform=True, model=True)
    add.add_argument("--field", "-f", action="append", default=[], help="新增时就带的覆盖字段，可重复")
    add.add_argument("--value", "-v", action="append", default=[], help="与 --field 按顺序对应")
    _add_write(add)

    remove = _action(actions, "remove", "删除静态模型条目（爬虫平台的模型请用 blacklist）", "model_remove")
    _add_target(remove, platform=True, model=True)
    _add_write(remove)

    set_action = _action(actions, "set", "改模型级覆盖字段", "model_set")
    _add_target(set_action, platform=True, model=True)
    _add_field(set_action)
    _add_write(set_action)

    unset = _action(actions, "unset", "删除模型级覆盖字段（回落到平台取值）", "model_unset")
    _add_target(unset, platform=True, model=True)
    unset.add_argument("--field", "-f", required=True)
    _add_write(unset)


def _add_alias_group(groups) -> None:
    parser = groups.add_parser("alias", help="模型组别名")
    actions = parser.add_subparsers(dest="action", metavar="<动作>")

    _action(actions, "list", "列出别名指向的 selector", "alias_list")

    set_action = _action(actions, "set", "覆盖别名指向的 selector 列表", "alias_set")
    set_action.add_argument("alias")
    set_action.add_argument(
        "--selector",
        action="append",
        default=[],
        help="selector：all / 平台名 / 平台名|模型名，可重复",
    )
    _add_write(set_action)


def _add_audit_group(groups) -> None:
    parser = groups.add_parser("audit", help="审计段")
    actions = parser.add_subparsers(dest="action", metavar="<动作>")

    _action(actions, "show", "查看审计配置与审计库状态", "audit_show")

    set_action = _action(actions, "set", "改审计字段", "audit_set")
    _add_field(set_action)
    _add_write(set_action)


def _add_usage_group(groups) -> None:
    parser = groups.add_parser("usage", help="用量与性能统计")
    actions = parser.add_subparsers(dest="action", metavar="<动作>")

    summary = _action(actions, "summary", "按维度聚合用量与性能", "usage_summary")
    summary.add_argument("--group-by", choices=GROUP_CHOICES, default="model")
    _add_filters(summary)
    summary.add_argument("--sort", choices=SORT_CHOICES, default="requests")
    summary.add_argument("--limit", type=int, default=20)

    ranking = _action(actions, "models", "模型对比榜：慢模型、不稳定模型、用量大户", "usage_models")
    ranking.add_argument("--sort", choices=SORT_CHOICES, default="requests")
    _add_filters(ranking)
    ranking.add_argument("--limit", type=int, default=20)

    timeseries = _action(actions, "timeseries", "按小时/天分桶的时序", "usage_timeseries")
    timeseries.add_argument("--bucket", choices=("hour", "day"), default="hour")
    _add_filters(timeseries)

    daily = _action(actions, "daily", "全量日用量（按天/用户，不受明细保留期影响）", "usage_daily")
    _add_filters(daily)
    daily.add_argument("--limit", type=int, default=200)

    attempts = _action(actions, "attempts", "审计明细下钻", "usage_attempts")
    _add_filters(attempts)
    attempts.add_argument("--outcome", choices=("success", "failure", "aborted", "stream_open"))
    attempts.add_argument("--limit", type=int, default=20)
    attempts.add_argument("--offset", type=int, default=0)

    chain = _action(actions, "chain", "还原一次请求的完整故障转移链", "usage_chain")
    chain.add_argument("gateway_request_id")

    _action(actions, "stats", "审计自身的健康状态", "usage_stats")

    prune = _action(actions, "prune", "删除指定时间之前的审计记录", "usage_prune")
    prune.add_argument("--before", required=True, help="ISO 时间、日期或 30d")


# ---------- 参数片段 ----------


def _action(actions, name: str, help_text: str, handler: str) -> argparse.ArgumentParser:
    """新建一个动作：动作名给用户看，handler 名给派发层用"""
    parser = actions.add_parser(name, help=help_text)
    _add_globals(parser, suppress_defaults=True)
    parser.set_defaults(handler=handler)
    return parser


def _add_target(parser: argparse.ArgumentParser, platform: bool = False, model: bool = False) -> None:
    if platform:
        parser.add_argument("platform", help="平台名")
    if model:
        parser.add_argument("model", help="模型条目名（与配置文件里写的一致）")


def _add_field(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--field", "-f", required=True, help="字段名")
    parser.add_argument("--value", "-v", help="取值：数字/true/false/字符串")
    parser.add_argument("--values", nargs="+", help="列表型取值（例如 blacklist）")


def _add_write(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="只预演，不写盘")
    parser.add_argument("--note", default="", help="写进备份元数据的备注")
    parser.add_argument("--no-reload", action="store_true", help="写盘后不尝试进程内重载")


def _add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--since", help="起始时间：ISO 时间、日期或 7d/24h/30m")
    parser.add_argument("--until", help="结束时间")
    parser.add_argument("--platform", help="只看某个平台")
    parser.add_argument("--model", help="只看某个模型（模型名，不是 selector）")
    parser.add_argument("--endpoint", help="只看某个入口：chat_completions/responses")
    parser.add_argument("--user", help="只看某个用户标签（GATEWAY_USER_KEYS 里配的名称）")


# ---------- 取值解析 ----------


def parse_value(text: Optional[str]) -> Any:
    """把命令行里的字符串转成合适的类型"""
    if text is None:
        return None

    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def parse_field_values(args: argparse.ArgumentParser) -> Any:
    """--value 与 --values 二选一"""
    values: List[Any] = getattr(args, "values", None) or []
    if values:
        return [parse_value(item) for item in values]
    return parse_value(getattr(args, "value", None))


def parse_overrides(fields: List[str], values: List[str]) -> dict:
    """`--field a --field b --value 1 --value 2` 按顺序配对"""
    if len(fields) != len(values):
        raise ValueError("--field 与 --value 的数量必须一致")
    return {field: parse_value(value) for field, value in zip(fields, values)}
