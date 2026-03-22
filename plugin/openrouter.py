import os
import json
import logging
import requests
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta

"""
OpenRouter 免费模型插件

这是一个用于从 OpenRouter API 动态获取免费模型列表的插件。
插件支持缓存机制、参数过滤和与静态模型配置的合并。

## 功能特性

- **动态模型发现**: 从 OpenRouter API 实时获取最新的免费模型列表
- **参数过滤**: 支持按类别（category）、输入模态（input_modalities）和输出模态（output_modalities）过滤模型
- **智能缓存**: 可配置的缓存机制，减少 API 调用频率，默认缓存5分钟
- **混合配置**: 支持与静态 models 配置共存，插件模型优先于静态模型

## 配置示例

```yaml
openrouter:
  baseUrl: "https://openrouter.ai/api/v1"
  apiKey: "${OPENROUTER_API_KEY}"
  plugin:
    code: "plugin.openrouter"
    args: # 参数可选
      category: "free"           # 模型类别：free(默认), programming, coding, coder
      input_modalities: ["text"] # 输入模态：text, image, 或 ["text", "image"]
      output_modalities: ["text"] # 输出模态：text, image, 或 ["text", "image"]
      cache_timeout: 300         # 缓存过期时间（秒），默认300秒（5分钟），设为0禁用缓存
  models: # 静态配置的模型会放在插件返回的模型后面
    - openrouter/free
  timeout: 10
  weight: 1
  enabled: true
```

## 参数说明

### category 参数
- `"free"`: 获取所有免费模型（默认行为）
- `"programming"`, `"coding"`, `"coder"`: 获取编程相关的免费模型
- 其他类别: 直接传递给 OpenRouter API 的 categories 参数

### input_modalities 参数
- `["text"]`: 仅文本输入模型
- `["image"]`: 仅图像输入模型  
- `["text", "image"]`: 支持文本和图像输入的模型
- 省略此参数: 获取所有输入模态的模型

### output_modalities 参数
- `["text"]`: 仅文本输出模型
- `["image"]`: 仅图像输出模型  
- `["text", "image"]`: 支持文本和图像输出的模型
- 省略此参数: 获取所有输出模态的模型

### cache_timeout 参数
- 正整数: 缓存过期时间（秒），例如 300 = 5分钟
- 0: 禁用缓存，每次都会调用 OpenRouter API 获取最新数据
- 默认值: 300秒（5分钟）

## 使用场景

### 1. 仅使用插件（推荐）
```yaml
openrouter:
  plugin:
    code: "plugin.openrouter"
    args:
      category: "free"
      input_modalities: ["text"]
      output_modalities: ["text"]
      cache_timeout: 600  # 10分钟缓存
```

### 2. 插件 + 静态模型（混合模式）
```yaml
openrouter:
  plugin:
    code: "plugin.openrouter"
    args:
      category: "programming"
      output_modalities: ["text"]
      cache_timeout: 300
  models:  # 这些会放在插件返回的模型后面
    - my-special-model
    - backup-model
```

### 3. 禁用缓存（实时获取）
```yaml
openrouter:
  plugin:
    code: "plugin.openrouter"
    args:
      output_modalities: ["text"]
      cache_timeout: 0  # 禁用缓存，每次都会调用API
```

## 注意事项

- 插件使用 OpenRouter 的 `/api/frontend/models/find` API 端点
- 通过 `max_price=0` 参数确保只获取免费模型
- 缓存键基于 category、input_modalities 和 output_modalities 参数生成，确保不同参数组合使用独立缓存
- 模型ID使用 API 返回的 `slug` 字段，确保与 OpenRouter 平台一致
- 当同时存在插件和静态 models 配置时，最终模型列表 = [插件模型...] + [静态模型...]
"""

import os
import json
import logging
import requests
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta

# 配置日志
logger = logging.getLogger(__name__)

# 全局缓存字典，存储 {cache_key: (timestamp, models_list)}
_cache = {}

def _get_cache_key(category: str, input_modalities: str, output_modalities: str) -> str:
    """生成缓存键"""
    return f"openrouter:{category or 'all'}:{input_modalities or 'all'}:{output_modalities or 'all'}"

def _is_cache_valid(timestamp: datetime, cache_timeout: int) -> bool:
    """检查缓存是否有效"""
    if cache_timeout <= 0:
        return False  # 缓存禁用
    return datetime.now() - timestamp < timedelta(seconds=cache_timeout)

def extract_free_models_from_api(category: str = None, input_modalities: str = None, output_modalities: str = None, cache_timeout: int = 300) -> List[str]:
    """
    从 OpenRouter API 提取所有免费模型（带缓存支持）

    Args:
        category: 可选的类别过滤，如 'programming'
        input_modalities: 可选的输入模态过滤，如 'text,image'
        output_modalities: 可选的输出模态过滤，如 'text,image'
        cache_timeout: 缓存过期时间（秒），默认300秒（5分钟）

    Returns:
        免费模型ID列表（slug字段 + ":free"后缀）
    """
    # 生成缓存键
    cache_key = _get_cache_key(category, input_modalities, output_modalities)
    
    # 检查缓存
    if cache_key in _cache:
        timestamp, cached_models = _cache[cache_key]
        if _is_cache_valid(timestamp, cache_timeout):
            logger.debug(f"使用缓存的OpenRouter模型列表 (key: {cache_key})")
            return cached_models.copy()
    
    # 使用您提供的正确API端点
    base_url = "https://openrouter.ai/api/frontend/models/find"
    params = {
        'fmt': 'cards',
        'max_price': 0,
        'order': 'top-weekly'
    }

    # 如果指定了输入模态，添加到查询参数
    if input_modalities:
        params['input_modalities'] = input_modalities
    
    # 如果指定了输出模态，添加到查询参数
    if output_modalities:
        params['output_modalities'] = output_modalities
    
    # 如果指定了类别，添加到查询参数
    if category:
        params['categories'] = category

    try:
        logger.info(f"正在获取OpenRouter免费模型列表...")
        response = requests.get(base_url, params=params, timeout=30)
        response.raise_for_status()

        data = response.json()
        models = data.get('data', {}).get('models', [])

        # 提取所有模型的slug（这就是模型ID），并添加":free"后缀
        model_ids = [f"{model.get('slug')}:free" for model in models if model.get('slug')]

        logger.info(f"找到 {len(model_ids)} 个免费模型")
        if model_ids:
            logger.debug(f"免费模型列表: {model_ids}")
        
        # 更新缓存
        _cache[cache_key] = (datetime.now(), model_ids.copy())
        logger.debug(f"更新缓存 (key: {cache_key}, timeout: {cache_timeout}s)")
        
        return model_ids

    except requests.exceptions.RequestException as e:
        logger.error(f"请求API时发生错误: {e}")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"解析JSON响应时发生错误: {e}")
        return []
    except Exception as e:
        logger.error(f"提取免费模型时发生未知错误: {e}")
        return []

def get_models(plugin_config: Dict[str, Any]) -> List[str]:
    """
    插件主函数：根据配置返回模型ID列表
    
    Args:
        plugin_config: 插件配置字典，包含 args 字段
        
    Returns:
        模型ID列表（每个ID都带有":free"后缀）
    """
    # 从配置中获取参数
    args = plugin_config.get('args', {})
    category = args.get('category')
    input_modalities = args.get('input_modalities')
    output_modalities = args.get('output_modalities')
    cache_timeout = args.get('cache_timeout', 300)  # 默认5分钟缓存
    
    # 处理 input_modalities - 如果是列表，转换为逗号分隔的字符串
    if isinstance(input_modalities, list):
        input_modalities = ','.join(input_modalities)
    elif input_modalities is None:
        input_modalities = None
    
    # 处理 output_modalities - 如果是列表，转换为逗号分隔的字符串
    if isinstance(output_modalities, list):
        output_modalities = ','.join(output_modalities)
    elif output_modalities is None:
        output_modalities = None
    
    # 特殊处理 category 参数
    if category == "free":
        # 如果 category 是 "free"，我们保持为 None，因为 max_price=0 已经过滤了免费模型
        category = None
    elif category in ['coder', 'coding', 'programming']:
        # 支持 coder/coding 编程相关的参数
        category = 'programming'
    
    # 调用核心函数获取模型列表
    model_ids = extract_free_models_from_api(category, input_modalities, output_modalities, cache_timeout)
    
    return model_ids