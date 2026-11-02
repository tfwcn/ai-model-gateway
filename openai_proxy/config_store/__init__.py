"""
配置管理层 - 让 models.yaml 可以经审查、校验、备份后再写回

分工：
    schema     字段白名单与取值边界
    document   保留注释的 round-trip 加载与脱敏视图
    editor     补丁操作（只在内存副本上改）
    writer     加锁、备份、原子写盘、回滚
    service    把上面几步串成一条命令（供接口与 CLI 共用）
    routes     /admin/config 接口
"""
from openai_proxy.config_store.document import ConfigDocument, entry_name, model_selector
from openai_proxy.config_store.editor import OPS, PatchError, PatchResult, apply_patch
from openai_proxy.config_store.schema import Issue
from openai_proxy.config_store.writer import ConfigWriter

__all__ = [
    "ConfigDocument",
    "ConfigWriter",
    "Issue",
    "OPS",
    "PatchError",
    "PatchResult",
    "apply_patch",
    "entry_name",
    "model_selector",
]
