# OpenAI Responses API 流式调用协议总结

## 📋 文档概述

本文档总结了 OpenAI Responses API v2 规范（2026年5月）的流式调用协议，包括完整的事件流程、工具调用机制以及两种不同的工具类型对比。

---

## 🎯 核心特性

### 1. 基础信息
- **API版本**: OpenAI Responses API v2
- **协议类型**: SSE (Server-Sent Events)
- **内容类型**: `text/event-stream; charset=utf-8`
- **事件顺序**: 通过 `sequence_number` 保证单调递增

### 2. HTTP请求格式
```http
POST https://api.openai.com/v1/responses
Authorization: Bearer {API_KEY}
Content-Type: application/json
Accept: text/event-stream
```

**请求体关键字段**:
- `model`: 模型名称（如 gpt-4o-2025-05-13）
- `stream`: 必须设置为 `true`
- `input`: 用户输入文本
- `tools`: 可选的工具定义数组
- `tool_choice`: 工具选择策略（auto/required/none）

---

## 🔄 标准SSE事件流程

### 完整事件序列

```
1. response.queued          → 响应排队中
2. response.created         → 响应创建完成
3. response.in_progress     → 处理进行中
4. response.output_item.added    → 输出项添加
5. response.content_part.added   → 内容部分添加
6. response.text.delta           → 文本增量（多次）
7. response.text.done            → 文本完成
8. response.content_part.done    → 内容部分完成
9. response.output_item.done     → 输出项完成
10. response.completed      → 响应完成
11. data: [DONE]            → 流结束标记
```

### 关键事件说明

| 事件类型 | 用途 | 关键字段 |
|---------|------|---------|
| `response.queued` | 初始状态 | response.id, status="queued" |
| `response.created` | 开始处理 | status="in_progress" |
| `response.text.delta` | 文本流式输出 | item_id, delta |
| `response.text.done` | 文本完成 | item_id, 完整文本 |
| `response.completed` | 最终结果 | 包含完整output和usage统计 |

---

## 🛠️ 工具调用机制

### Function 工具调用流程

当模型决定调用工具时，会在文本输出后插入工具调用事件：

```
response.output_item.added (type: function_call)
↓
response.function_call_arguments.delta (多次，流式参数)
↓
response.function_call_arguments.done (完整JSON参数)
↓
response.output_item.done
```

**重要**: 必须等待 `response.function_call_arguments.done` 事件后再解析JSON参数。

### 工具执行结果返回

客户端需要将工具执行结果作为新的输入发送回API：

```json
{
  "type": "function_call_output",
  "call_id": "call_xyz1234",
  "output": "15"
}
```

---

## 🔀 两种工具类型对比

### Function vs Custom 工具

| 特性 | Function 工具 | Custom 工具 |
|------|--------------|-------------|
| **输入格式** | 严格JSON Schema | 任意纯文本 |
| **参数验证** | 支持 strict 模式 | 无内置验证 |
| **输出类型** | `function_call` | `custom_tool_call` |
| **流式事件** | `response.function_call_arguments.*` | `response.custom_tool_call_input.*` |
| **模型支持** | 所有GPT-4o/GPT-4.1/o系列 | 仅GPT-5+ |
| **适用场景** | API调用、数据库查询 | 代码执行、自由文本 |

### Function 工具定义示例
```json
{
  "type": "function",
  "name": "get_weather",
  "description": "Get weather info",
  "parameters": {
    "type": "object",
    "properties": {
      "location": {"type": "string"}
    },
    "required": ["location"]
  },
  "strict": true
}
```

### Custom 工具定义示例
```json
{
  "type": "custom",
  "name": "code_exec",
  "description": "Execute Python code"
}
```

---

## ⚠️ 特殊场景处理

### 1. 模型拒绝回答
当事件中出现 `response.refusal.delta` 和 `response.refusal.done` 时，表示模型拒绝回答：

```json
{
  "type": "response.refusal.done",
  "item_id": "msg_78901",
  "refusal": "I'm sorry, but I can't assist with that request."
}
```

### 2. 错误响应
```
event: response.failed
data: {
  "type": "response.failed",
  "response": {
    "status": "failed",
    "error": {
      "code": "invalid_model",
      "message": "The model does not exist"
    }
  }
}
```

---

## 🔑 关键协议细节

### 1. 事件顺序保证
- 所有事件的 `sequence_number` 从0开始单调递增
- 若发现不连续则表示有事件丢失

### 2. 流混淆保护
- OpenAI会在delta事件中添加随机 `obfuscation` 字段
- 用于防止侧信道攻击，客户端应忽略该字段

### 3. 断点续传
连接中断后可通过以下方式继续：
```
GET /v1/responses/{id}?stream=true&starting_after={last_sequence_number}
```

### 4. Token统计
- 准确的token使用统计**仅**在最后的 `response.completed` 事件中提供
- 包含 `input_tokens`, `output_tokens`, `total_tokens`

### 5. 事件分隔符
- 每个事件块之间用**两个换行符**(`\n\n`)分隔
- 最后以 `data: [DONE]` 结束

---

## 💡 最佳实践

### Function 工具适用场景
✅ 需要严格结构化输入的API调用  
✅ 数据库查询和操作  
✅ 有明确参数要求的系统调用  
✅ 需要保证参数正确性的关键业务逻辑  

### Custom 工具适用场景
✅ 代码执行（Python、JavaScript等）  
✅ 自然语言处理任务  
✅ 自由格式的文本输入  
✅ 不需要严格参数验证的简单工具  

### 开发注意事项
1. **严格模式**: Function工具支持 `strict: true`，保证参数完全符合JSON Schema
2. **模型限制**: Custom工具仅支持GPT-5系列及以上模型
3. **事件处理**: 两种工具类型使用完全不同的流式事件，需分别处理
4. **兼容性**: Responses API中的function工具定义不再需要嵌套在`function`字段下

---

## 📊 响应头信息

标准HTTP响应头包含：
```http
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache
Connection: keep-alive
Transfer-Encoding: chunked
X-Request-ID: req_abc123def456
OpenAI-Organization: org-xyz789
OpenAI-Processing-MS: 123
```

---

## 🎓 总结

OpenAI Responses API v2 提供了强大的流式调用能力，支持：
- 实时文本生成
- 结构化函数调用
- 自由格式自定义工具
- 完善的错误处理和拒绝机制
- 可靠的断点续传功能

开发者需要根据具体场景选择合适的工具类型，并正确处理各种SSE事件，以实现稳定可靠的应用集成。
