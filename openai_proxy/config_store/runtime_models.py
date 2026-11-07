"""
运行时模型清单 - 配置文件里没有、只有进程里才有的候选

存在理由：爬虫平台的模型是插件运行时灌进来的，`models.yaml` 里根本没有条目
（见 editor.py：这类条目不能删，只能用 blacklist 屏蔽）。
界面要让人「从下拉里勾一个模型」而不是在输入框里打字，就只能问进程当前加载了哪些模型。

这里只给模型名与 selector：绝不给密钥、baseUrl 或插件参数。
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List

# 进程内没加载任何模型时给界面的说明：这种情况下拉是空的，不是页面坏了
NOT_LOADED_NOTE = "进程里还没有加载任何模型（服务刚启动或模型加载失败），下拉里没有候选"


@dataclass
class RuntimeModel:
    """进程内的一条模型条目

    Attributes:
        platform: 平台名（blacklist 写在哪个平台段里）
        name: 模型条目名（blacklist 的 fnmatch 就是拿它去匹配的）
    """

    platform: str
    name: str

    @property
    def selector(self) -> str:
        """别名指向用的写法：`平台名|模型名`"""
        return f"{self.platform}|{self.name}"

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "selector": self.selector}


class RuntimeModelCatalog:
    """把进程内的模型表整理成界面可用的候选清单

    Args:
        provider: 返回进程内模型表的函数（`{平台名: [ModelConfig, ...]}`）。
                  用函数而不是字典：热重载会整张换掉模型表，界面每次都要看最新的。
    """

    def __init__(self, provider: Callable[[], Dict[str, Any]]):
        self._provider = provider

    def models(self) -> List[RuntimeModel]:
        """全部运行时模型条目（按平台名、模型名排序：界面下拉的顺序不能每次刷新都变）"""
        table = self._provider() or {}
        items: List[RuntimeModel] = []
        for platform in sorted(table):
            entries = table[platform]
            # 只加载平台、还没加载模型时，这一项是平台配置字典而不是模型列表（见 config_loader）
            if not isinstance(entries, (list, tuple)):
                continue
            # 插件可能把同一个条目灌两次：候选里出现两次只会让人以为有两个模型
            names = {name for name in (_entry_name(entry) for entry in entries) if name}
            for name in sorted(names):
                items.append(RuntimeModel(platform=platform, name=name))
        return items

    def payload(self) -> Dict[str, Any]:
        """给接口的形状：按平台分组的候选，加上「进程到底加载了没有」"""
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for item in self.models():
            grouped.setdefault(item.platform, []).append(item.to_dict())

        loaded = bool(grouped)
        return {
            "loaded": loaded,
            "total": sum(len(models) for models in grouped.values()),
            "platforms": [{"name": name, "models": models} for name, models in grouped.items()],
            "note": "" if loaded else NOT_LOADED_NOTE,
        }


def _entry_name(entry: Any) -> str:
    """取模型条目名

    `ModelConfig.model` 才是配置文件/插件给出的条目名（`ModelConfig.name` 是 `平台|条目名` 的内部标识），
    blacklist 的 fnmatch 用的也是它。
    """
    value = getattr(entry, "model", None) or getattr(entry, "name", None)
    return str(value).strip() if value else ""
