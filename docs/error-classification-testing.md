# 错误分类系统测试指南

## 概述

本文档说明如何测试新实现的错误分类系统。

## 测试文件

### 1. 单元测试 (`tests/test_error_classification.py`)

测试错误分类器的基本功能：

```bash
cd /mnt/local_data/ubuntu_x86/kubernetes/website/moyume-project/openai-proxy
python3 -m pytest tests/test_error_classification.py -v
# 或者直接运行
python3 tests/test_error_classification.py
```

**测试内容**：
- ✅ HTTP错误分类（400, 401, 403, 404, 429, 500, 502, 503, 504）
- ✅ 超时错误分类
- ✅ 连接错误分类（DNS、连接拒绝）
- ✅ 无效响应分类
- ✅ 未知错误分类
- ✅ 错误处理策略验证

### 2. 集成测试 (`tests/test_error_classification_integration.py`)

测试错误分类在实际故障转移场景中的应用：

```bash
cd /mnt/local_data/ubuntu_x86/kubernetes/website/moyume-project/openai-proxy
python3 -m pytest tests/test_error_classification_integration.py -v
# 或者直接运行
python3 tests/test_error_classification_integration.py
```

**测试内容**：
- 🔄 模拟故障转移决策场景
- 📊 演示完整错误处理流程
- 📈 错误分类统计概览

## 测试结果示例

### 单元测试输出

```
🧪 开始测试错误分类系统

============================================================
测试1: HTTP错误分类
============================================================

✓ HTTP 400: [请求格式错误] 不可重试, 不禁用模型
  - 可重试: 否
  - 禁用模型: 否
  - 重试延迟: 0.0秒

✓ HTTP 401: [认证失败] 不可重试, 将禁用模型
  - 可重试: 否
  - 禁用模型: 是
  - 重试延迟: 0.0秒

...

✅ HTTP错误分类测试通过
...
🎉 所有测试通过！
```

### 集成测试输出

```
🔄 模拟故障转移决策场景
======================================================================

场景1: 网络超时
----------------------------------------------------------------------
错误类型: network_timeout
处理策略: [网络超时] 可重试, 不禁用模型
预期行为: 重试其他模型，不禁用当前模型
系统决策: ✅ 正确: 不禁用，允许重试（临时错误）

场景2: API密钥错误(401)
----------------------------------------------------------------------
错误类型: http_401_unauthorized
处理策略: [认证失败] 不可重试, 将禁用模型
预期行为: 禁用模型，不再尝试
系统决策: ✅ 正确: 永久禁用（配置/资源错误）

...

🎉 集成测试全部通过！
```

## 验证要点

### 1. 错误分类准确性

确保每种错误都被正确分类：
- HTTP状态码 → 对应的错误类型
- 超时异常 → `network_timeout`
- 连接异常 → `network_connection_error` 或 `network_dns_error`

### 2. 处理策略正确性

验证每种错误的处理策略：

| 错误类型 | 可重试 | 禁用模型 | 原因 |
|---------|--------|---------|------|
| 网络超时 | ✅ | ❌ | 临时性问题 |
| HTTP 401 | ❌ | ✅ | API密钥错误 |
| HTTP 429 | ✅ | ✅ | 速率限制，短暂禁用 |
| HTTP 500 | ✅ | ❌ | 服务器临时错误 |
| 缺少content | ❌ | ✅ | 模型问题 |

### 3. 日志输出完整性

检查日志是否包含：
- 错误分类结果摘要
- 详细的错误消息
- 处理决策（是否禁用、是否重试）

## 实际场景测试

要测试实际的代理服务，可以：

1. **启动代理服务**：
   ```bash
   cd /mnt/local_data/ubuntu_x86/kubernetes/website/moyume-project/openai-proxy
   python3 run.py
   ```

2. **发送测试请求**：
   ```bash
   curl -X POST http://localhost:8000/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{
       "model": "all",
       "messages": [{"role": "user", "content": "Hello"}],
       "stream": false
     }'
   ```

3. **观察日志**：
   查看服务日志中的错误分类信息，例如：
   ```
   INFO: 错误分类结果: [速率限制] 可重试, 将禁用模型
   WARNING: 模型 modelscope-Qwen-Max 被标记为周期内用完（错误类型: http_429_rate_limit）
   ```

## 常见问题

### Q: 测试失败怎么办？

A: 检查以下几点：
1. 确保已安装所有依赖：`pip install -r requirements.txt`
2. 确保在正确的目录运行测试
3. 查看错误堆栈跟踪，定位具体问题

### Q: 如何添加新的错误类型？

A: 
1. 在 `ErrorCategory` 枚举中添加新类型
2. 在 `ERROR_STRATEGIES` 字典中定义处理策略
3. 添加相应的分类方法到 `ErrorClassifier` 类
4. 更新测试用例

### Q: 如何调整错误处理策略？

A: 修改 `ErrorClassifier.ERROR_STRATEGIES` 字典中的配置：
```python
ErrorCategory.NETWORK_TIMEOUT: {
    "is_retryable": True,        # 是否可重试
    "should_disable_model": False,  # 是否禁用模型
    "retry_delay_seconds": 1.0,  # 重试延迟
},
```

## 下一步

测试通过后，可以：
1. 在生产环境中监控错误分类的效果
2. 根据实际数据调整错误处理策略
3. 添加更多细粒度的错误分类
4. 实现指数退避重试机制
