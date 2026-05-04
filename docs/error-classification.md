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
