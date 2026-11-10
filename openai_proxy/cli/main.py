"""
命令行入口 - 把参数派发到配置命令或用量命令

与网关进程的关系：
- 命令行直接改配置文件，不会（也不该）去重启别人的进程
- 网关默认监听 models.yaml，写盘后通常会自动热重载；也可以手动调用 POST /admin/config/reload 或自行重启
"""
import logging
import os
import sys
from typing import List, Optional

from openai_proxy.cli.codes import EXIT_ERROR, EXIT_OK, EXIT_USAGE
from openai_proxy.cli.config_commands import ConfigCommands
from openai_proxy.cli.output import Console
from openai_proxy.cli.parser import build_parser
from openai_proxy.cli.usage_commands import AuditAccess, UsageCommands
from openai_proxy.config_store.service import ConfigCommandService
from openai_proxy.config_store.writer import ConfigWriteError

# 需要配置文件才能跑的动作
_CONFIG_ACTIONS = {
    "config_document", "config_show", "config_fields", "config_validate", "config_backups", "config_rollback",
    "platform_set", "platform_unset", "platform_add", "platform_remove",
    "model_add", "model_remove", "model_set", "model_unset",
    "alias_list", "alias_set", "audit_set", "audit_show",
}

# 只读动作不必提示「让改动生效」
_READ_ONLY_ACTIONS = {
    "config_document", "config_show", "config_fields", "config_validate", "config_backups",
    "alias_list", "audit_show",
}


class CliApp:
    """命令行应用"""

    def __init__(self, console: Optional[Console] = None):
        self.console = console
        self.parser = build_parser()

    def run(self, argv: Optional[List[str]] = None) -> int:
        args = self.parser.parse_args(argv)

        if not getattr(args, "handler", None):
            self.parser.print_help()
            return EXIT_USAGE

        self._configure_logging(args)
        console = self.console or Console(as_json=args.json)

        try:
            return self._dispatch(args, console)
        except FileNotFoundError as exc:
            console.line(f"文件不存在: {exc}", to_err=True)
            return EXIT_ERROR
        except ConfigWriteError as exc:
            console.line(f"写盘被拒绝: {exc.message}", to_err=True)
            return EXIT_ERROR
        except ValueError as exc:
            console.line(f"参数不对: {exc}", to_err=True)
            return EXIT_USAGE
        except KeyboardInterrupt:
            console.line("已取消", to_err=True)
            return EXIT_ERROR

    # ---------- 派发 ----------

    def _dispatch(self, args, console: Console) -> int:
        handler = args.handler

        if handler in _CONFIG_ACTIONS:
            commands = ConfigCommands(self._config_service(args), console)
        else:
            commands = UsageCommands(AuditAccess(self._config_path(args)), console)

        try:
            result = getattr(commands, handler)(args)
        finally:
            if isinstance(commands, UsageCommands):
                commands.access.close()

        if self._should_hint(args, handler, console, result):
            _hint_running_service(console)
        return result

    # ---------- 依赖 ----------

    def _config_path(self, args) -> str:
        return args.config or os.getenv("MODEL_CONFIG_FILE") or "models.yaml"

    def _config_service(self, args) -> ConfigCommandService:
        """构建配置命令服务

        命令行进程不是网关本身，所以不传 reload_callback；
        正在运行的网关默认会通过文件监听自动热重载，关闭监听时由用户手动调用 reload。
        """
        backup_dir = args.backup_dir or os.getenv("CONFIG_BACKUP_DIR") or "data/config_backups"
        return ConfigCommandService(
            config_file=self._config_path(args),
            backup_dir=backup_dir,
            reload_callback=None,
            audit_recorder=None,
        )

    def _configure_logging(self, args) -> None:
        """默认让命令输出干净：只有 --verbose 才打印服务日志"""
        root = logging.getLogger()
        for handler in root.handlers:
            # 文件日志保留，只静音控制台输出
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                handler.setLevel(logging.INFO if args.verbose else logging.CRITICAL)
        logging.getLogger("openai_proxy").setLevel(logging.INFO if args.verbose else logging.CRITICAL)

    def _should_hint(self, args, handler: str, console: Console, exit_code: int) -> bool:
        """预演、JSON 输出、只读动作、失败结果都不插提示文本"""
        return (
            exit_code == EXIT_OK
            and handler in _CONFIG_ACTIONS
            and handler not in _READ_ONLY_ACTIONS
            and not getattr(args, "dry_run", False)
            and not console.as_json
        )


def _hint_running_service(console: Console) -> None:
    console.line("")
    console.line("提示：网关默认会监听 models.yaml，写盘后通常会自动热重载；")
    console.line("  也可以手动调用 POST /admin/config/reload（带 GATEWAY_API_KEY），或自行重启网关进程。")


def main(argv: Optional[List[str]] = None) -> int:
    """入口函数"""
    try:
        return CliApp().run(argv)
    except SystemExit as exc:  # argparse 自己处理过的用法错误
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
