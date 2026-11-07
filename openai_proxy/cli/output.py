"""
命令行输出 - 同一份结果既能给人看也能给脚本用

规则：
- 默认输出对齐的文本表格
- 加 --json 时输出机器可读的 JSON
- 错误与警告一律走 stderr，方便管道
"""
import json
import sys
from typing import Any, Dict, Iterable, Optional, Sequence, TextIO

# 表格列宽上限：超过就截断，避免终端里一行配置把整屏挤散
_MAX_WIDTH = 46


class Console:
    """命令输出"""

    def __init__(self, as_json: bool = False, out: Optional[TextIO] = None, err: Optional[TextIO] = None):
        self.as_json = as_json
        self.out = out or sys.stdout
        self.err = err or sys.stderr

    # ---------- 结构化输出 ----------

    def emit(self, payload: Any) -> None:
        """--json 时原样输出；否则挑一个可读呈现"""
        if self.as_json:
            self.json(payload)
            return
        if isinstance(payload, dict) and "changes" in payload:
            self.summary(payload)
        else:
            self.json(payload)

    def json(self, payload: Any) -> None:
        self._write(self.out, json.dumps(payload, ensure_ascii=False, indent=2))

    def summary(self, result: Dict[str, Any]) -> None:
        """配置写入结果：变更清单 + 行级差异 + 备份位置"""
        if self.as_json:
            self.json(result)
            return

        changes: Sequence[Dict[str, Any]] = result.get("changes") or []
        if result.get("dry_run"):
            self.line("（--dry-run：没有写盘）")

        self.line(f"变更 {len(changes)} 处：")
        for change in changes:
            self.line(f"  {change.get('path')}")
            self.line(f"    之前: {_brief(change.get('before'))}")
            self.line(f"    之后: {_brief(change.get('after'))}")

        if result.get("diff"):
            self.line("行级差异：")
            self.diff(result["diff"])

        self.issues(result.get("errors") or [], result.get("warnings") or [])

        if not result.get("valid", True):
            self.line("校验未通过，未写盘。", to_err=True)
            return
        if result.get("backup"):
            self.line(f"已写盘，旧内容备份在: {result['backup']}")
        if result.get("needs_restart"):
            self.line("这些改动涉及插件/爬虫结构，需要你自行重启服务才会生效。")
        elif result.get("reloaded"):
            self.line("已在进程内重载模型配置，无需重启。")

    # ---------- 表格 ----------

    def table(self, rows: Iterable[Dict[str, Any]], columns: Sequence[tuple]) -> None:
        """
        输出对齐表格

        Args:
            rows: 数据行
            columns: [(字段名, 表头), ...]
        """
        rows = list(rows)
        if not rows:
            self.line("（没有记录）")
            return

        head = [title for _, title in columns]
        body = [[_cell(row.get(field)) for field, _ in columns] for row in rows]
        widths = [
            max(len(head[index]), max(len(line[index]) for line in body))
            for index in range(len(head))
        ]

        self.line("  ".join(title.ljust(widths[index]) for index, title in enumerate(head)))
        self.line("  ".join("-" * width for width in widths))
        for line in body:
            self.line("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(line)))

    def diff(self, lines: Sequence[str]) -> None:
        """行级差异：纯文本里用 +/- 前缀就够，不追求颜色"""
        for line in lines:
            self.line(line)

    def issues(self, errors: Sequence[Dict[str, str]], warnings: Sequence[Dict[str, str]]) -> None:
        """错误影响退出码，警告只提示"""
        for issue in errors:
            self.line(f"错误 [{issue.get('path') or '配置'}] {issue.get('message')}", to_err=True)
        for issue in warnings:
            self.line(f"警告 [{issue.get('path') or '配置'}] {issue.get('message')}", to_err=True)

    # ---------- 文本 ----------

    def line(self, text: str = "", to_err: bool = False) -> None:
        self._write(self.err if to_err else self.out, text)

    def block(self, title: str, lines: Sequence[str]) -> None:
        self.line(f"{title}:")
        for line in lines:
            self.line(f"  {line}")

    def _write(self, stream: TextIO, text: str) -> None:
        # 先冲掉标准输出，否则管道里警告会插到正文中间
        if stream is self.err:
            self.out.flush()
        stream.write(text + "\n")


# ---------- 工具 ----------


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        # 1e+03 这种科学计数法在终端里很难读
        return f"{value:,.0f}" if value.is_integer() else f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    text = str(value)
    return text if len(text) <= _MAX_WIDTH else text[: _MAX_WIDTH - 1] + "…"


def _brief(value: Any) -> str:
    """把 before/after 的取值压成一行"""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return "（无）"
    return str(value)
