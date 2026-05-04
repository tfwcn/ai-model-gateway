# 工具调用格式转换器

## 概述

`ToolCallConverter` 是一个专门用于将非标准格式的工具调用响应转换为标准 OpenAI `tool_calls` 格式的实用工具。

## 支持的格式

### 1. NVIDIA JSON 格式

某些模型（如 `meta/llama-4-maverick-17b-128e-instruct`）会将工具调用信息以 JSON 字符串形式返回在 `content` 字段中：

```json
{
  "name": "get_weather",
  "parameters": {
    "location": "here"
  }
}
```

### 2. Minimax XML 格式

Minimax 平台使用自定义的 XML 标签格式：

```xml
<minimax:tool_call>
  <invoke name="_test_tool">
    {}
  </invoke>
</minimax:tool_call>
```

### 3. 标准 OpenAI 格式

如果已经是标准的 `tool_calls` 数组格式，则直接返回，不做转换。

## 使用方法

### 基本用法

```python
from openai_proxy.utils.tool_call_converter import ToolCallConverter

# 转换非标准格式
content = '{"name": "get_weather", "parameters": {"location": "here"}}'
tool_calls, remaining_content = ToolCallConverter.convert_to_standard_format(content)

# tool_calls 现在是标准格式：
# [{
#   "id": "call_xxx",
#   "type": "function",
#   "function": {
#     "name": "get_weather",
#     "arguments": "{\"location\": \"here\"}"
#   }
# }]
# remaining_content 是空字符串（因为已提取到 tool_calls）
```

### 检测非标准格式

```python
# 快速检测内容是否包含非标准格式的工具调用
if ToolCallConverter.is_non_standard_format(content):
    print("检测到非标准格式")
```

### 与现有 tool_calls 结合使用

```python
# 如果已有标准格式的 tool_calls，会直接返回
existing_tool_calls = [...]
tool_calls, remaining = ToolCallConverter.convert_to_standard_format(
    content="some text",
    existing_tool_calls=existing_tool_calls
)
# 返回的 tool_calls 就是 existing_tool_calls
```

## 集成点

转换器已在以下组件中集成：

1. **ResponsesAdapter** (`openai_proxy/adapter/responses.py`)
   - `build_response_object()`: 转换非流式响应
   - `convert_stream_event()`: 转换流式响应

2. **ToolCapabilityTester** (`openai_proxy/model/capability/tester.py`)
   - `_test_non_streaming()`: 检测非流式测试中的非标准格式
   - `_test_streaming()`: 检测流式测试中的非标准格式

## 扩展支持新格式

要添加对新格式的支持，只需在 `ToolCallConverter` 类中添加新的检测方法：

```python
@staticmethod
def _try_new_format(content: str) -> Optional[List[Dict]]:
    """尝试解析新格式"""
    # 实现解析逻辑
    if matches:
        return [ToolCallConverter._create_standard_tool_call(...)]
    return None
```

然后在 `convert_to_standard_format()` 方法中调用它。

## 优势

1. **模块化**: 所有格式转换逻辑集中在一个地方
2. **可扩展**: 易于添加对新格式的支持
3. **可测试**: 独立的单元测试
4. **向后兼容**: 不影响现有的标准格式处理
5. **清晰的责任分离**: 适配器只负责调用转换器，不关心具体实现
