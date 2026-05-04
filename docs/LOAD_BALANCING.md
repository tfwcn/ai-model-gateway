# ⚡ 负载均衡策略

AI Model Gateway 的负载均衡和故障转移机制详解。

## 📋 目录

- [权重负载均衡](#权重负载均衡)
- [故障转移机制](#故障转移机制)
- [错误分类与处理](#错误分类与处理)
- [配置示例](#配置示例)
- [性能优化建议](#性能优化建议)

---

## 权重负载均衡

### 工作原理

系统根据配置的权重值分配请求优先级：

```yaml
modelscope:
  weight: 10  # 最高优先级
  enabled: true

nvidia:
  weight: 8   # 中等优先级
  enabled: true

openrouter:
  weight: 5   # 较低优先级
  enabled: true
```

**请求分配流程：**
1. 按权重从高到低排序平台
2. 尝试权重最高的平台
3. 如果失败，自动切换到下一个平台
4. 记录失败信息用于后续决策

### 动态权重调整（未来功能）

v3.0 计划引入 AI 驱动的动态权重调整：
- 基于历史成功率自动调整权重
- 考虑响应时间和成本因素
- 实时优化路由策略

---

## 故障转移机制

### 故障检测

系统监控以下指标判断平台是否可用：

- HTTP 状态码（4xx, 5xx）
- 响应超时
- 配额用尽错误
- 网络连接错误

### 故障转移策略

#### 1. 立即重试

对于临时性错误（网络抖动、短暂超时）：
- 立即切换到下一个平台
- 不标记原平台为不可用

#### 2. 短期禁用

对于配额用尽错误：
- 标记平台在指定周期内不可用
- 例如：`quota_period: "daily"` → 禁用至次日零点

#### 3. 长期禁用

对于持续性错误（API 密钥无效、服务下线）：
- 标记平台为不可用
- 需要手动干预恢复

### 故障恢复

系统定期检查被禁用的平台：
- 发送测试请求验证可用性
- 如果成功，恢复平台到可用状态
- 更新平台健康状态

---

## 错误分类与处理

系统实现 7 种错误类型的精细化处理：

| 错误类型 | 处理方式 | 示例 |
|---------|---------|------|
| **RateLimitError** | 短期禁用 | 429 Too Many Requests |
| **QuotaExceededError** | 按周期禁用 | 每日额度用尽 |
| **AuthenticationError** | 长期禁用 | API 密钥无效 |
| **NotFoundError** | 长期禁用 | 模型不存在 |
| **TimeoutError** | 立即重试 | 响应超时 |
| **ConnectionError** | 立即重试 | 网络连接失败 |
| **ServerError** | 立即重试 | 500 Internal Error |

详细文档：[📖 错误分类系统](./error-classification.md)

---

## 配置示例

### 基础配置

```yaml
modelscope:
  baseUrl: "https://api-inference.modelscope.cn/v1"
  apiKey: "${MODELSCOPE_API_KEY}"
  weight: 10
  timeout: 300
  enabled: true
  quota_period: "daily"  # 额度刷新周期
  
nvidia:
  baseUrl: "https://integrate.api.nvidia.com/v1"
  apiKey: "${NVIDIA_API_KEY}"
  weight: 8
  timeout: 300
  enabled: true
  quota_period: "daily"
  
openrouter:
  baseUrl: "https://openrouter.ai/api/v1"
  apiKey: "${OPENROUTER_API_KEY}"
  weight: 5
  timeout: 300
  enabled: true
  quota_period: "daily"
```

### 高级配置

```yaml
# 自定义重试策略
retry:
  max_retries: 3
  backoff_factor: 1.5  # 指数退避
  retry_on:
    - TimeoutError
    - ConnectionError
    - ServerError

# 健康检查配置
health_check:
  interval: 300  # 每 5 分钟检查一次
  timeout: 10
  test_model: "all"
```

---

## 性能优化建议

### 1. 合理设置权重

根据平台特点分配权重：

```yaml
# 高质量但有限额的平台 → 高权重
modelscope:
  weight: 10

# 稳定但较慢的平台 → 中等权重
nvidia:
  weight: 8

# 免费但可能不稳定的平台 → 低权重
openrouter:
  weight: 5
```

### 2. 调整超时时间

```yaml
# 快速响应的平台 → 短超时
fast_platform:
  timeout: 10

# 慢速但稳定的平台 → 长超时
slow_platform:
  timeout: 60
```

### 3. 启用缓存

减少重复请求：

```yaml
cache:
  type: "redis"
  ttl: 3600
  max_size: 1000
```

### 4. 监控和优化

定期检查 Prometheus 指标：

```bash
# 查看各平台的请求分布
curl http://localhost:8000/metrics | grep http_requests_total

# 查看故障转移次数
curl http://localhost:8000/metrics | grep model_failover_total

# 查看错误分类统计
curl http://localhost:8000/metrics | grep error_classification_total
```

根据数据调整配置：
- 某个平台频繁故障 → 降低权重或暂时禁用
- 某个平台表现优秀 → 提高权重
- 缓存命中率低 → 增加 TTL 或 max_size

---

## 相关文档

- [配置指南](./CONFIGURATION_GUIDE.md)
- [监控与运维](./MONITORING.md)
- [错误分类系统](./error-classification.md)
- [部署指南](./DEPLOYMENT.md)
