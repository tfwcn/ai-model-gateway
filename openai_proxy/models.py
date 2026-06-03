from dataclasses import dataclass
from typing import Optional, Dict, List, Any, Tuple


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
    retry_count: int = 3  # 单个模型调用失败时的重试次数


def _infer_capabilities(model_id: str) -> List[str]:
    """基于模型标识推断基础能力"""
    model_lower = model_id.lower()
    capabilities: List[str] = []

    if any(token in model_lower for token in ["embed", "embedding"]):
        capabilities.append("embedding")

    if any(token in model_lower for token in ["gpt", "llama", "claude", "mistral", "dolly", "falcon", "bloom", "palm"]):
        capabilities.append("chat")
        if "completion" not in capabilities:
            capabilities.append("completion")

    if not capabilities:
        capabilities.append("completion")

    return capabilities


def model_config_to_dict(platform: str, model_config: ModelConfig) -> Dict[str, Any]:
    return {
        "id": model_config.name,
        "name": model_config.model,
        "provider": platform,
        "capabilities": _infer_capabilities(model_config.model),
        "metadata": {
            "base_url": model_config.base_url,
            "timeout": model_config.timeout,
            "weight": model_config.weight,
            "enabled": model_config.enabled,
            "retry_count": model_config.retry_count,
            **({"quota_period": model_config.quota_period} if model_config.quota_period else {})
        }
    }


def list_available_models(
    models: Dict[str, List[ModelConfig]],
    provider: Optional[str] = None,
    capability: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    all_aliases: Optional[List[str]] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """按筛选条件聚合可用模型列表

    返回三种格式的模型：
    1. "all" - 所有平台所有模型
    2. "{platform}" - 指定平台的所有模型
    3. "{platform}|{model_name}" - 指定平台的指定模型
    """
    all_aliases = all_aliases or []
    all_items: List[Dict[str, Any]] = []

    # 构造所有可用的别名ID列表
    all_ids = ["all"] + all_aliases

    # 为每个别名添加 "all" 选项
    for alias_id in all_ids:
        all_items.append({
            "id": alias_id,
            "name": "所有平台所有模型",
            "provider": "all",
            "capabilities": ["chat", "completion"],
            "metadata": {
                "type": "group",
                "description": "遍历所有平台的所有模型，按权重排序"
            }
        })

    for platform, model_configs in models.items():
        if provider and platform.lower() != provider.lower():
            continue

        # 添加平台级别的 "{platform}" 选项
        all_items.append({
            "id": platform,
            "name": f"{platform} 平台所有模型",
            "provider": platform,
            "capabilities": ["chat", "completion"],
            "metadata": {
                "type": "platform_group",
                "description": f"遍历 {platform} 平台的所有模型"
            }
        })

        # 添加每个具体模型的 "{platform}|{model_name}" 选项
        for model_config in model_configs:
            item = model_config_to_dict(platform, model_config)
            # 修改 id 为 "{platform}|{model_name}" 格式
            item["id"] = f"{platform}|{model_config.name}"
            item["metadata"]["type"] = "model"
            all_items.append(item)

    def matches_query(item: Dict[str, Any]) -> bool:
        if q:
            q_lower = q.lower()
            if q_lower in item["id"].lower() or q_lower in item["name"].lower() or q_lower in item["provider"].lower():
                return True
            if any(q_lower in str(value).lower() for value in item["metadata"].values()):
                return True
            return False
        return True

    def matches_capability(item: Dict[str, Any]) -> bool:
        if capability:
            capability_lower = capability.lower()
            return any(capability_lower == cap.lower() or capability_lower in cap.lower() for cap in item["capabilities"])
        return True

    filtered = [item for item in all_items if matches_query(item) and matches_capability(item)]
    total = len(filtered)
    paged = filtered[offset:offset + limit] if limit >= 0 else filtered[offset:]

    return paged, total
