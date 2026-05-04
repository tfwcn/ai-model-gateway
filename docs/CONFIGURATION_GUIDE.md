# 🔧 配置指南

详细的 AI Model Gateway 配置说明。

## 📋 目录

- [环境变量配置](#环境变量配置)
- [平台配置](#平台配置)
- [插件系统详解](#插件系统详解)
- [OpenClaw 集成配置](#openclaw-集成配置)
- [高级配置选项](#高级配置选项)

---

## 环境变量配置

创建 `.env` 文件并填入你的 API 密钥：

```bash
cp .env.example .env
nano .env
```

```env
MODELSCOPE_API_KEY=your-modelscope-api-key
OPENROUTER_API_KEY=your-openrouter-api-key
NVIDIA_API_KEY=your-nvidia-api-key
OPENAI_API_KEY=your-openai-api-key
AZURE_API_KEY=your-azure-api-key
```

> ⚠️ **安全提示**：API 密钥绝不应该直接写在 `models.yaml` 文件中，必须通过环境变量管理。

---

## 平台配置

编辑 `models.yaml` 配置文件：

### ModelScope 配置

```yaml
modelscope:
  baseUrl: "https://api-inference.modelscope.cn/v1"
  apiKey: "${MODELSCOPE_API_KEY}"  # 自动从环境变量读取
  weight: 10  # 权重越高，优先级越高
  timeout: 300
  enabled: true
  quota_period: "daily"  # 额度刷新周期
  plugin:
    code: "plugin.modelscope"
    cache_timeout: 3600
```

### OpenRouter 配置

```yaml
openrouter:
  baseUrl: "https://openrouter.ai/api/v1"
  apiKey: "${OPENROUTER_API_KEY}"
  weight: 5
  timeout: 300
  enabled: true
  quota_period: "daily"
  plugin:
    code: "plugin.openrouter"
    cache_timeout: 300
    args:
      request_params:
        max_price: 0  # 只获取免费模型
```

### NVIDIA 配置

```yaml
nvidia:
  baseUrl: "https://integrate.api.nvidia.com/v1"
  apiKey: "${NVIDIA_API_KEY}"
  weight: 8
  timeout: 300
  enabled: true
  quota_period: "daily"
  plugin:
    code: "plugin.nvidia"
    cache_timeout: 3600
```

---

## 插件系统详解

项目提供强大的插件系统，动态从各平台 API 获取最新免费模型列表。

### 🎯 NVIDIA 插件

自动抓取 NVIDIA NIM API 的免费预览模型：

```yaml
nvidia:
  plugin:
    code: "plugin.nvidia"
    cache_timeout: 3600
    args:
      free_model_count: 10  # 获取前10个免费模型
```

**支持的免费模型模式：**
- `nvidia/` - NVIDIA 官方模型
- `microsoft/phi` - Microsoft Phi 系列
- `google/gemma` - Google Gemma 系列
- `meta/llama-3.2` - Meta Llama 3.2 系列

📖 [查看 NVIDIA 爬虫详细文档](./NVIDIA_SCRAPER_README.md)

### 🎯 ModelScope 插件

基于 `SupportInference` 字段过滤免费模型：

```yaml
modelscope:
  plugin:
    code: "plugin.modelscope"
    cache_timeout: 3600
    args:
      request_params:
        SupportInference: "txt2txt"  # 文本生成模型
```

📖 [查看 ModelScope 爬虫详细文档](./MODELSCOPE_SCRAPER_README.md)

### 🎯 OpenRouter 插件

按类别和价格过滤免费模型：

```yaml
openrouter:
  plugin:
    code: "plugin.openrouter"
    cache_timeout: 300
    args:
      request_params:
        max_price: 0  # 只获取免费模型
        categories: "programming"  # 编程类模型（可选）
```

📖 [查看 OpenRouter 爬虫详细文档](./OPENROUTER_SCRAPER_README.md)

📖 [查看插件配置 FAQ](./PLUGIN_FAQ.md) - 常见问题解答  
📖 [查看迁移指南](./MIGRATION_GUIDE.md) - 从旧版配置迁移

---

## OpenClaw 集成配置

在 OpenClaw 的 `clawdbot.json` 中配置：

```json
{
  "models": {
    "mode": "merge",
    "providers": {
      "auto": {
        "baseUrl": "http://localhost:8000/v1",
        "apiKey": "auto",
        "api": "openai-completions",
        "models": [
          {
            "id": "all",
            "name": "all",
            "api": "openai-completions",
            "reasoning": true,
            "input": ["text", "image"],
            "cost": {
              "input": 0,
              "output": 0,
              "cacheRead": 0,
              "cacheWrite": 0
            },
            "contextWindow": 256000,
            "maxTokens": 256000
          }
        ]
      }
    }
  },
  "agents": {
    "defaults": {
      "model": {
        "primary": "auto/all"
      },
      "models": {
        "auto/all": {}
      }
    }
  }
}
```

---

## 高级配置选项

### 缓存配置

```yaml
cache:
  type: "memory"  # 或 "redis"
  ttl: 3600  # 缓存过期时间（秒）
  max_size: 1000  # 最大缓存条目数
```

### Redis 配置（用于会话状态管理）

```env
REDIS_URL=redis://localhost:6379
RESPONSES_SESSION_TTL=86400
```

### Prometheus 监控配置

```yaml
monitoring:
  enabled: true
  metrics_path: "/metrics"
  scrape_interval: 30s
```

---

## 相关文档

- [监控与运维指南](./MONITORING.md)
- [负载均衡策略](./LOAD_BALANCING.md)
- [部署指南](./DEPLOYMENT.md)
- [安全注意事项](./SECURITY.md)
