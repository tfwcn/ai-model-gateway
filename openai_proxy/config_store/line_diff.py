"""行级差异按「新增 / 修改 / 删除」分类

存在理由：unified diff 是给人读文本的，界面要的是「这一行是什么性质、原值是什么、新值是什么」，
有了这三样才能用同一套列宽把行纵向对齐，并按颜色区分。

配对规则：同一个差异块里成对出现的旧行与新行算「修改」，
只剩旧行算「删除」，只剩新行算「新增」——不会出现「一删一增」两条颜色。
"""

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List

ADD = "add"
MODIFY = "modify"
REMOVE = "remove"

# 改动块往上找「这一段的名字」最多看几行、名字最长多少个字符
CONTEXT_LOOKBACK = 6
CONTEXT_MAX_CHARS = 46


@dataclass
class DiffRow:
    """一行差异：kind 决定颜色，before/after 决定两列内容，context 说明它属于哪一段"""

    kind: str
    before: str = ""
    after: str = ""
    context: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "before": self.before,
            "after": self.after,
            "context": self.context,
        }


class LineDiff:
    """两份文本的行级差异"""

    def __init__(self, before: str, after: str):
        self._before = (before or "").splitlines()
        self._after = (after or "").splitlines()

    def rows(self) -> List[Dict[str, Any]]:
        """按文件里出现的顺序返回差异行（不含没有变化的行）"""
        rows: List[DiffRow] = []
        for tag, i1, i2, j1, j2 in SequenceMatcher(None, self._before, self._after).get_opcodes():
            if tag == "equal":
                continue
            context = self._section_of(i1)
            old = [line.strip() for line in self._before[i1:i2]]
            new = [line.strip() for line in self._after[j1:j2]]
            for index in range(max(len(old), len(new))):
                left = old[index] if index < len(old) else ""
                right = new[index] if index < len(new) else ""
                rows.append(DiffRow(self._kind(left, right), left, right, context))
        return [row.to_dict() for row in rows]

    @staticmethod
    def _kind(left: str, right: str) -> str:
        if left and right:
            return MODIFY
        return ADD if right else REMOVE

    def _section_of(self, line_index: int) -> str:
        """改动块上方最近的段名（yaml 的键或列表项），让读者知道这几行属于哪个平台/条目"""
        for offset in range(line_index - 1, max(line_index - 1 - CONTEXT_LOOKBACK, -1), -1):
            line = self._before[offset].strip()
            if line.endswith(":") or line.startswith("- "):
                return _clip(line)
        return ""


def _clip(text: str) -> str:
    return text if len(text) <= CONTEXT_MAX_CHARS else text[:CONTEXT_MAX_CHARS] + "…"
