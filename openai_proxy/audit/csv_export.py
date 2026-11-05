"""维度汇总 CSV 导出

把 UsageQueryService.dimension_usage() 的结果渲染成可被 Excel 直接打开的 CSV：
- 列 = 用户选择的维度名 + 固定指标列
- 空值写成空串，不拿 0 冒充「未知」
- 带 UTF-8 BOM，避免中文表头在 Excel 里显示成乱码
"""
import csv
import io
from typing import Any, Dict, Iterable, List, Sequence, Tuple

# 固定指标列：(取值字段, 表头)
_METRIC_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("requests", "请求"),
    ("attempts", "尝试"),
    ("success", "成功"),
    ("failure", "失败"),
    ("aborted", "中断"),
    ("input_tokens", "输入tokens"),
    ("cached_tokens", "缓存tokens"),
    ("output_tokens", "输出tokens"),
    ("success_rate", "成功率"),
)


class DimensionCsvExporter:
    """把维度日汇总结果渲染为 CSV 文本"""

    def __init__(self, group_by: Sequence[str], items: Iterable[Dict[str, Any]]):
        """保存维度组合与待导出的行"""
        self.group_by: List[str] = list(group_by)
        self.items: List[Dict[str, Any]] = list(items)

    def render(self) -> str:
        """生成完整 CSV 文本（含 BOM）"""
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(self._header())
        for item in self.items:
            writer.writerow(self._row(item))
        return "\ufeff" + buffer.getvalue()

    def _header(self) -> List[str]:
        """表头：维度在前，固定指标在后"""
        return [*self.group_by, *(label for _, label in _METRIC_COLUMNS)]

    def _row(self, item: Dict[str, Any]) -> List[str]:
        """一行数据：维度值从 values 取，指标值从行本身取"""
        values = item.get("values") or {}
        return [
            *(_cell(values.get(name)) for name in self.group_by),
            *(_cell(item.get(key), percent=key == "success_rate") for key, _ in _METRIC_COLUMNS),
        ]


def _cell(value: Any, percent: bool = False) -> str:
    """单元格转字符串：None 留空，成功率转百分比，其余原样交给 csv 模块处理引号与逗号"""
    if value is None:
        return ""
    if percent and isinstance(value, (int, float)):
        return f"{float(value) * 100:.1f}%"
    return str(value)
