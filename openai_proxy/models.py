from dataclasses import dataclass
from typing import Optional


@dataclass
class ModelConfig:
    """模型配置"""
    name: str
    api_key: str
    base_url: str
    model: str
    timeout: int = 30
    weight: int = 1  # 权重，用于负载均衡（可选）
    enabled: bool = True
    quota_period: Optional[str] = None  # 额度刷新周期
    enable_tool_call_conversion: bool = True  # 是否启用工具调用格式转换