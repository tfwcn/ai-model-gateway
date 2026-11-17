# 错误分类系统说明

## 概述

代理服务现在实现了更精细的错误分类系统，能够根据不同类型的错误采取不同的处理策略。这提高了故障转移的智能化程度，避免了不必要的模型禁用，同时确保了对严重错误的快速响应。

## 错误分类体系

### 1. 网络相关错误

| 错误类型 | 描述 | 是否可重试 | 是否禁用模型 | 重试延迟 |
|---------|------|-----------|------------|---------|
| `network_timeout` | 网络超时 | ✅ 是 | ❌ 否 | 1秒 |
| `network_connection_error` | 网络连接错误 | ✅ 是 | ❌ 否 | 2秒 |
| `network_dns_error` | DNS解析错误 | ✅ 是 | ❌ 否 | 5秒 |

**特点**：这些通常是临时性问题，不会禁用模型，允许后续请求继续尝试。

### 2. HTTP 4xx 客户端错误

| 错误类型 | 描述 | 是否可重试 | 是否禁用模型 | 重试延迟 |
|---------|------|-----------|------------|---------|
| `http_400_bad_request` | 请求格式错误 | ❌ 否 | ❌ 否 | 0秒 |
| `http_401_unauthorized` | 认证失败（API密钥错误） | ❌ 否 | ✅ 是 | 0秒 |
| `http_403_forbidden` | 权限不足 | ❌ 否 | ✅ 是 | 0秒 |
| `http_404_not_found` | 资源不存在（模型不存在） | ❌ 否 | ✅ 是 | 0秒 |
| `http_429_rate_limit` | 速率限制 | ✅ 是 | ✅ 是 | 10秒 |

**特点**：
- 401/403/404 表示配置或资源问题，需要禁用模型
- 429 表示达到速率限制，短暂禁用后自动恢复

### 3. HTTP 5xx 服务器错误

| 错误类型 | 描述 | 是否可重试 | 是否禁用模型 | 重试延迟 |
|---------|------|-----------|------------|---------|
| `http_500_internal_error` | 服务器内部错误 | ✅ 是 | ❌ 否 | 2秒 |
| `http_502_bad_gateway` | 网关错误 | ✅ 是 | ❌ 否 | 3秒 |
| `http_503_service_unavailable` | 服务不可用 | ✅ 是 | ❌ 否 | 5秒 |
| `http_504_gateway_timeout` | 网关超时 | ✅ 是 | ❌ 否 | 3秒 |

**特点**：这些是服务器端临时问题，不会禁用模型，允许重试。

### 4. 业务逻辑错误

| 错误类型 | 描述 | 是否可重试 | 是否禁用模型 | 重试延迟 |
|---------|------|-----------|------------|---------|
| `invalid_response_format` | 响应格式无效 | ❌ 否 | ✅ 是 | 0秒 |
| `missing_content` | 缺少内容字段 | ❌ 否 | ✅ 是 | 0秒 |
| `invalid_model` | 模型无效 | ❌ 否 | ✅ 是 | 0秒 |

**特点**：这些通常表示模型本身的问题，需要禁用模型。

## 使用示例

### 日志输出示例

当发生错误时，系统会输出详细的分类信息：

```
WARNING: 模型 modelscope-Qwen-Max 返回错误: 429 - Rate limit exceeded
INFO: 错误分类结果: [速率限制] 可重试, 将禁用模型
WARNING: 模型 modelscope-Qwen-Max 被标记为周期内用完（错误类型: http_429_rate_limit）
DEBUG: 平台 modelscope 继续尝试下一个模型...
```

```
WARNING: 模型 nvidia-Llama-3-70B 请求超时 (耗时: 60.15秒, 超时阈值: 60秒)
INFO: 错误分类结果: [网络超时] 可重试, 不禁用模型
DEBUG: 模型 nvidia-Llama-3-70B 不禁用（错误类型: network_timeout，可重试）
DEBUG: 平台 nvidia 继续尝试下一个模型...
```

```
WARNING: 模型 openrouter-free 返回错误: 401 - Invalid API key
INFO: 错误分类结果: [认证失败] 不可重试, 将禁用模型
WARNING: 模型 openrouter-free 被标记为周期内用完（错误类型: http_401_unauthorized）
```

## 优势

### 1. 智能决策
- **网络波动**：不会因临时网络问题禁用模型
- **速率限制**：短暂禁用后自动恢复，避免永久失效
- **配置错误**：快速识别并禁用有问题的模型

### 2. 更好的可观测性
- 每个错误都有明确的分类和描述
- 日志中包含详细的错误类型和处理策略
- 便于问题诊断和监控

### 3. 提高可用性
- 减少不必要的模型禁用
- 对临时错误更加宽容
- 对严重错误快速响应

### 4. 可扩展性
- 易于添加新的错误类型
- 可以针对不同错误类型定制重试策略
- 支持动态调整错误处理策略

## 技术实现

### 核心组件

1. **ErrorClassifier** (`openai_proxy/model/error_classifier.py`)
   - 负责错误分类和策略决策
   - 提供多种分类方法（HTTP错误、超时、连接错误等）

2. **ClassifiedError** 数据类
   - 包含错误分类、消息、重试策略等信息
   - 统一错误表示格式

3. **ModelFailoverManager** 集成
   - 在 `call_model_stream` 和 `call_model_non_stream` 中使用分类器
   - 根据分类结果决定是否禁用模型

### 代码示例

```python
# 错误分类器的使用
try:
    async with session.post(url, json=request_body, headers=headers) as response:
        if response.status == 200:
            # 处理成功响应
            ...
        else:
            error_text = await response.text()
            # 分类HTTP错误
            classified_error = ErrorClassifier.classify_http_error(
                response.status, error_text, model_config.name
            )
            return False, classified_error
except asyncio.TimeoutError:
    # 分类超时错误
    classified_error = ErrorClassifier.classify_timeout_error(
        model_config.name, elapsed_time, model_config.timeout
    )
    return False, classified_error
except aiohttp.ClientError as e:
    # 分类连接错误
    classified_error = ErrorClassifier.classify_connection_error(e, model_config.name)
    return False, classified_error
```

## 未来改进方向

1. **指数退避重试**：为可重试错误添加指数退避机制
2. **熔断器模式**：为频繁失败的模型添加熔断器
3. **监控指标**：收集各模型的错误率和响应时间
4. **动态策略调整**：根据历史数据动态调整错误处理策略
5. **错误聚合分析**：定期分析错误模式，优化配置

## 对外错误响应（下游契约）

故障转移全部失败时，网关不会只回一句「所有模型都不可用」，而是把**关键错误**
收敛成一份 OpenAI 兼容错误体。下游按 OpenAI SDK 的既有方式解析即可，无需改代码。

### 1. 非流式

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 10
Content-Type: application/json
```

```json
{
  "error": {
    "message": "HTTP 429: rate limit exceeded",
    "type": "rate_limit_error",
    "param": null,
    "code": "rate_limit_exceeded",
    "request_id": "a1b2c3d4",
    "attempts": [
      {"status": 429, "category": "http_429_rate_limit"},
      {"status": 503, "category": "http_503_service_unavailable"}
    ]
  }
}
```

约定：

- `message` 优先使用**上游原始文案**（例如「图片下载失败 + 完整 URL」），最多 800 字符；
  拿不到上游文案时才用「HTTP 状态码 + 原始报文」兜底。
- `type` / `code` / `param` 优先沿用上游返回值，下游的判断逻辑与直连上游时一致。
- 只有 `error` 一个顶层键，同时保留 HTTP 状态码语义。
- `attempts` 只包含 `status` 和 `category`，不暴露平台名、模型名、base_url。
- 单次尝试失败时不输出 `attempts`。
- 错误文本会先脱敏（`sk-*`、`Bearer *`、`api_key=*` 等）再回传。

### 2. 状态码映射

| 主错误 | HTTP | type |
|---|---|---|
| 上游 400 / 工具调用错误 / 参数错 | 400 | `invalid_request_error` |
| 上游 429 | 429（带 `Retry-After`） | `rate_limit_error` |
| 上游 401 / 403 / 404 | 502 | `api_error` |
| 上游 5xx / 连接失败 | 502 | `api_error` |
| 上游 504 / 首块超时 | 504 | `api_error` |
| 所有模型当前不可用 | 503（带 `Retry-After`） | `api_error` |
| 未预期异常 | 500 | `api_error` |

> 上游 401/403 **不会**原样透传：那是网关侧的配置问题，透传 401 会让下游误以为自己的密钥失效。

### 3. 主错误选择

多次尝试全部失败时，按以下优先级挑一条作为 `message`：

1. 标记为「停止故障转移」的错误（请求自身构造有问题）；
2. 上游 400 类（换模型也没用）；
3. **最早出现的真实上游错误**（根因，不会被后面的错误覆盖）；
4. 「无可用模型」兜底。

如果所有模型都在上一个周期内被禁用，`message` 会带上最近一次失败原因
（例如「当前没有可用模型；最近一次失败原因: invalid api key」）。

### 4. 流式

流式请求增加一个「快速失败窗口」（`FAILOVER_FAST_FAIL_SECONDS`，默认 3 秒）：

- 窗口内失败：还能用真实 HTTP 状态码返回上面的标准 JSON；
- 超过窗口：先下发 SSE 心跳保活，之后的失败用标准事件通报。

Chat Completions：

```
data: {"error":{"message":"...","type":"api_error","code":"upstream_timeout",...}}

data: [DONE]
```

Responses：

```
event: error
data: {"type":"error","code":"upstream_timeout","message":"...","param":null}

data: [DONE]
```
