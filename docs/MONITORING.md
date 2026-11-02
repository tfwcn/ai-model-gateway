# 📊 监控与运维指南

AI Model Gateway 的监控、日志和运维说明。

## 📋 目录

- [Prometheus 监控](#prometheus-监控)
- [用量与性能审计](#用量与性能审计)
- [健康检查端点](#健康检查端点)
- [日志管理](#日志管理)
- [性能优化](#性能优化)
- [故障排查](#故障排查)

---

## Prometheus 监控

### 启用监控

项目内置 Prometheus 指标收集，默认在 `/metrics` 端点暴露。

> ⚠️ `/metrics` 与所有 `/admin/*` 一样受 `GATEWAY_API_KEY` 保护：
> 未配置该密钥时返回 `403 {"error":{"code":"admin_disabled"}}`；配置了却没带密钥返回 `401`。

```bash
curl -H "Authorization: Bearer $GATEWAY_API_KEY" http://localhost:8000/metrics
```

### 关键指标

| 指标名称 | 类型 | 描述 |
|---------|------|------|
| `proxy_requests_total` | Counter | 总请求数，标签 `platform` / `status` / `error_type` |
| `proxy_errors_total` | Counter | 按分类统计的错误数 |
| `proxy_request_duration_seconds` | Histogram | 请求延迟分布 |
| `proxy_failover_total` | Counter | 故障转移次数，标签 `from_platform` / `to_platform` |
| `proxy_stream_ttft_seconds` | Histogram | 流式首块耗时分布 |
| `proxy_stream_duration_seconds` | Histogram | 上游调用总耗时分布 |
| `proxy_stream_throughput_tokens_per_second` | Histogram | 输出速度分布（端到端 tokens/s） |
| `proxy_stream_tokens_total` | Counter | 上游回报的 token 用量，标签 `token_type` |
| `proxy_usage_missing_total` | Counter | 上游没有回报 usage 的调用次数（这类调用的用量统计不完整） |
| `platform_availability` | Gauge | 平台/模型可用性（1=可用，0=不可用） |
| `cache_hits_total` / `cache_misses_total` | Counter | 缓存命中/未命中 |
| `active_connections` / `pending_requests` | Gauge | 活跃连接数 / 待处理请求数 |

标签默认不含 `model`：爬虫模式下模型数量很大，`model` 标签会把指标基数撑爆。要按模型看 Prometheus 数据，
用环境变量 `METRICS_LABEL_MODEL=true` **并在启动前**设好（指标注册时就读它，`models.yaml` 里的
`audit.metrics_label_model` 目前不改变标签）；打开后前四个计数与三个流式直方图会多出 `model` 标签。

关掉 Prometheus 打点：`AUDIT_PROMETHEUS=false`（`/metrics` 返回 404）。

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
            "expr": "rate(proxy_requests_total[5m])"
          }
        ]
      },
      {
        "title": "Error Rate by Type",
        "type": "piechart",
        "targets": [
          {
            "expr": "rate(proxy_errors_total[5m])"
          }
        ]
      },
      {
        "title": "P95 Time To First Chunk",
        "type": "graph",
        "targets": [
          {
            "expr": "histogram_quantile(0.95, rate(proxy_stream_ttft_seconds_bucket[5m]))"
          }
        ]
      },
      {
        "title": "Failovers",
        "type": "graph",
        "targets": [
          {
            "expr": "rate(proxy_failover_total[5m])"
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

---

## 用量与性能审计

Prometheus 适合看趋势与告警，**要下钻到「哪一次请求、哪一次切换、为什么失败」要看审计记录**：
一次上游尝试一行，落在 SQLite（默认 `data/audit.db`）。

### 管理接口

都需要 `Authorization: Bearer <GATEWAY_API_KEY>`：

```bash
# 按模型聚合用量与性能（成功率、P95、首块、tokens/s）
curl -H "Authorization: Bearer $GATEWAY_API_KEY" \
  "http://localhost:8000/admin/usage/summary?group_by=model&since=24h&sort=p95"

# 按天看趋势
curl -H "Authorization: Bearer $GATEWAY_API_KEY" \
  "http://localhost:8000/admin/usage/timeseries?bucket=day&since=30d"

# 只看失败的尝试
curl -H "Authorization: Bearer $GATEWAY_API_KEY" \
  "http://localhost:8000/admin/audit?outcome=failure&limit=20"

# 还原一次请求的完整故障转移链
curl -H "Authorization: Bearer $GATEWAY_API_KEY" \
  "http://localhost:8000/admin/audit/chain/<gateway_request_id>"

# 审计自身健康状态：行数、缓冲、丢弃、写库失败、最近刷盘
curl -H "Authorization: Bearer $GATEWAY_API_KEY" \
  "http://localhost:8000/admin/audit/stats"
```

### 命令行（不需要网关在跑）

```bash
python manage.py usage summary --group-by model --since 7d
python manage.py usage attempts --outcome failure --limit 20
python manage.py usage stats
```

### 管理台

`http://localhost:8000/admin/ui/` → 「用量与性能」「审计明细」两个标签页，
点任意一行审计记录就能展开它所属请求的整条故障转移链。

口径要点：**成功率只统计已收尾的尝试**（`finalized=1`），正在转发的流式请求记为 `stream_open`，不进分母。
完整说明见 [AUDIT.md](AUDIT.md)，接口字段见 [../web/API.md](../web/API.md)。

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

### 输出速度埋点

每次模型调用（流式与非流式）结束后追加一条 `输出速度统计` 日志，字段为 `key=value`，便于检索与聚合：

```
2026-05-04 10:30:31 [INFO] openai_proxy.utils.stream_metrics: 输出速度统计: 模型=openrouter|qwen3-235b 请求=e2e00001 上游id=chatcmpl-9f2c 首块=2.31s 总耗时=12.40s 输入tokens=120 输入缓存命中=64 输出tokens=350 输出字符数=820 端到端速度=28.23tokens/s
```

| 字段 | 来源 | 说明 |
|------|------|------|
| `请求` | 网关生成 | 本次模型尝试生成的 8 位 id（失败重试会更换），非流式为 `-` |
| `上游id` | 上游响应 | 上游响应/chunk 里的 `id`（如 `chatcmpl-xxx`）；上游未返回时为 `-`。它和 `/v1/responses` 回给客户端的 `resp_xxx` 不是同一个 |
| `首块` | 网关实测 | 上游首块数据耗时（TTFB），非流式为 `-` |
| `总耗时` | 网关实测 | 发起调用到流结束的耗时（含客户端消费产生的背压时间） |
| `输入tokens` | 上游 usage | `prompt_tokens`；上游未返回 usage 时为 `-` |
| `输入缓存命中` | 上游 usage | `prompt_tokens_details.cached_tokens`（DeepSeek 为 `prompt_cache_hit_tokens`）；上游未上报该字段时记 0 |
| `输出tokens` | 上游 usage | `completion_tokens`，**含推理 token**；上游未返回 usage 时为 `-` |
| `输出字符数` | 网关统计 | 增量文本字符数（含 `reasoning_content`、工具调用参数），**不是 token 数**，仅在 usage 缺失时用于估算 |
| `端到端速度` | 派生 | 输出 tokens ÷ 总耗时，客户端感知的平均输出速度 |

注意：token 字段来自上游 usage，不同供应商的 tokenizer 不同，跨供应商的 `端到端速度` 不能直接横向比较；`-` 表示上游没返回 usage（例如流被中途打断），不是 0。

按小时聚合平均速度（日志时间为 UTC）：

```bash
grep "输出速度统计" logs/proxy.log \
  | sed -nE 's/^([0-9]{4}-[0-9]{2}-[0-9]{2}) ([0-9]{2}):[0-9:]+ .*端到端速度=([0-9.]+)tokens\/s.*/\1T\2 \3/p' \
  | awk '{s[$1]+=$2; n[$1]++} END {for (k in s) printf "%s  平均 %.2f tokens/s（%d 次）\n", k, s[k]/n[k], n[k]}' \
  | sort
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
# 检查缓存统计（/metrics 需要管理密钥）
curl -H "Authorization: Bearer $GATEWAY_API_KEY" http://localhost:8000/metrics | grep cache

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

# 直接看审计里的故障转移链：先试了谁、为什么换、最后成没成
python manage.py usage attempts --outcome failure --limit 5
python manage.py usage chain <gateway_request_id>
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

- [审计与用量统计](./AUDIT.md)
- [管理台前端](./ADMIN_UI.md)
- [命令行管理工具](./CONFIG_CLI.md)
- [配置指南](./CONFIGURATION_GUIDE.md)
- [负载均衡策略](./LOAD_BALANCING.md)
- [错误分类系统](./error-classification.md)
- [部署指南](./DEPLOYMENT.md)
