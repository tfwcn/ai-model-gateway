# 📊 监控与运维指南

AI Model Gateway 的监控、日志和运维说明。

## 📋 目录

- [Prometheus 监控](#prometheus-监控)
- [健康检查端点](#健康检查端点)
- [日志管理](#日志管理)
- [性能优化](#性能优化)
- [故障排查](#故障排查)

---

## Prometheus 监控

### 启用监控

项目内置 Prometheus 指标收集，默认在 `/metrics` 端点暴露。

```bash
curl http://localhost:8000/metrics
```

### 关键指标

| 指标名称 | 类型 | 描述 |
|---------|------|------|
| `http_requests_total` | Counter | 总请求数 |
| `http_request_duration_seconds` | Histogram | 请求延迟分布 |
| `model_failover_total` | Counter | 故障转移次数 |
| `cache_hits_total` | Counter | 缓存命中数 |
| `cache_misses_total` | Counter | 缓存未命中数 |
| `error_classification_total` | Counter | 按类型的错误计数 |

### Grafana 仪表板示例

```json
{
  "dashboard": {
    "title": "AI Model Gateway Monitoring",
    "panels": [
      {
        "title": "Request Rate",
        "type": "graph",
        "targets": [
          {
            "expr": "rate(http_requests_total[5m])"
          }
        ]
      },
      {
        "title": "Error Rate by Type",
        "type": "piechart",
        "targets": [
          {
            "expr": "error_classification_total"
          }
        ]
      }
    ]
  }
}
```

### Kubernetes ServiceMonitor

项目提供 K8s ServiceMonitor 配置（`k8s/servicemonitor.yaml`）：

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: ai-model-gateway-monitor
spec:
  selector:
    matchLabels:
      app: ai-model-gateway
  endpoints:
    - port: http
      path: /metrics
      interval: 30s
```

---

## 健康检查端点

### 基本健康检查

```bash
curl http://localhost:8000/health
```

响应示例：
```json
{
  "status": "healthy",
  "timestamp": "2026-05-04T10:30:00Z"
}
```

### 详细健康检查

```bash
curl http://localhost:8000/health/detailed
```

响应示例：
```json
{
  "status": "healthy",
  "components": {
    "database": "connected",
    "cache": "active",
    "plugins": {
      "modelscope": "loaded",
      "openrouter": "loaded",
      "nvidia": "loaded"
    },
    "models_available": 127
  },
  "uptime_seconds": 86400
}
```

---

## 日志管理

### 日志级别

通过环境变量配置日志级别：

```env
LOG_LEVEL=INFO  # DEBUG, INFO, WARNING, ERROR, CRITICAL
```

### 日志格式

```
2026-05-04 10:30:00 [INFO] AI Model Gateway 启动中...
2026-05-04 10:30:01 [INFO] 正在加载配置文件: models.yaml
2026-05-04 10:30:02 [INFO] 插件加载完成: modelscope, openrouter, nvidia
2026-05-04 10:30:02 [INFO] AI Model Gateway 启动完成
```

### 日志轮转

生产环境建议使用日志轮转工具：

```bash
# 使用 logrotate
/var/log/ai-model-gateway/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
}
```

---

## 性能优化

### 缓存策略

#### 内存缓存（默认）

```yaml
cache:
  type: "memory"
  ttl: 3600
  max_size: 1000
```

**适用场景：**
- 单机部署
- 低并发场景
- 快速原型开发

#### Redis 缓存（推荐生产环境）

```yaml
cache:
  type: "redis"
  redis_url: "redis://localhost:6379"
  ttl: 3600
```

**优势：**
- 支持分布式部署
- 持久化存储
- 更高的缓存命中率

### 并发控制

```yaml
concurrency:
  max_connections: 100
  timeout: 30
  retry_count: 3
```

### 负载均衡调优

调整平台权重以优化性能：

```yaml
modelscope:
  weight: 10  # 高优先级
  timeout: 300

openrouter:
  weight: 5   # 中等优先级
  timeout: 300
```

---

## 故障排查

### 常见问题

#### 1. 所有平台都失败

**症状：** 所有请求返回 503 错误

**排查步骤：**
```bash
# 检查网络连接
curl https://api-inference.modelscope.cn/v1/models

# 检查 API 密钥
echo $MODELSCOPE_API_KEY

# 查看详细日志
tail -f logs/app.log | grep ERROR
```

**解决方案：**
- 验证 API 密钥是否正确
- 检查网络连接
- 查看平台状态页面

#### 2. 缓存不生效

**症状：** 相同请求重复调用平台 API

**排查步骤：**
```bash
# 检查缓存统计
curl http://localhost:8000/metrics | grep cache

# 清除缓存重试
curl -X POST http://localhost:8000/cache/clear
```

**解决方案：**
- 确认缓存已启用
- 检查 TTL 配置
- 验证 Redis 连接（如果使用）

#### 3. 故障转移不工作

**症状：** 平台失败后没有切换到备用平台

**排查步骤：**
```bash
# 检查故障转移日志
grep "failover" logs/app.log

# 查看模型状态
curl http://localhost:8000/health/detailed | jq '.components.models'
```

**解决方案：**
- 确认多个平台已启用
- 检查权重配置
- 验证错误分类器是否正常工作

### 调试模式

启用调试模式获取更详细的日志：

```env
LOG_LEVEL=DEBUG
DEBUG_MODE=true
```

⚠️ **注意：** 生产环境不要启用调试模式，会影响性能并泄露敏感信息。

---

## 相关文档

- [配置指南](./CONFIGURATION_GUIDE.md)
- [负载均衡策略](./LOAD_BALANCING.md)
- [错误分类系统](./error-classification.md)
- [部署指南](./DEPLOYMENT.md)
