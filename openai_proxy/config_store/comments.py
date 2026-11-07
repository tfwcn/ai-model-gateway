"""
注释处理 - 让 ruamel 挂在「条目 + 下标」上的注释在改动后不会串位

ruamel 的注释位置信息很脆：改一个条目、删一个条目都可能让注释跑到别的模型身上。
这里的策略保守且诚实：

- 只改取值：不碰注释（round-trip 保证原样保留）
- 条目写法变化（字符串 <-> 对象）：先把注释摘下来，保住排版，再由补丁如实报告它消失了
- 删除条目：摘掉它身上的注释，并把其余条目的注释下标往前挪，避免注释串到别的模型
- 所有结果都要经过 editor 的写回自检，自检不过就拒绝写盘
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def comment_text(container: Any, key: Any) -> str:
    """读取挂在某个条目/字段上的注释文本（不修改）"""
    return _join(_slots_of(container, key))


def pop_comment(container: Any, key: Any) -> str:
    """把挂在某个条目/字段上的注释摘下来并返回文本"""
    return _join(_items(container).pop(key, None) or [])


def shift_item_comments(container: Any, removed_index: int) -> None:
    """
    删除列表条目后同步注释的下标

    ruamel 不会因为 del 而自动挪下标，不修正就会让注释挂到别的模型上。
    """
    items = _items(container)
    moved: Dict[int, Any] = {}
    for key, slots in list(items.items()):
        position = _as_index(key)
        if position is None:
            continue
        moved[position - 1 if position > removed_index else position] = slots
    items.clear()
    items.update(moved)


# ---------- 内部 ----------


def _items(container: Any) -> Any:
    """取容器的注释表（没有就返回空表）"""
    comment_attr = getattr(container, "ca", None)
    items = getattr(comment_attr, "items", None)
    return items if isinstance(items, dict) else {}


def _slots_of(container: Any, key: Any) -> List[Any]:
    return list(_items(container).get(key) or [])


def _as_index(key: Any) -> Optional[int]:
    try:
        return int(key)
    except (TypeError, ValueError):
        return None


def _join(slots: List[Any]) -> str:
    """把注释槽里散落的注释拼成一段文本"""
    parts = [getattr(slot, "value", None) for slot in slots if slot is not None]
    return " ".join(_one_line(part) for part in parts if part).strip()


def _one_line(text: Any) -> str:
    """注释压成一行：报告时只需要内容，不需要原排版"""
    return " ".join(str(text).split()).lstrip("#").strip()
