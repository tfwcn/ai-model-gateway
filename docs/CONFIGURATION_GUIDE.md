# 🔧 配置指南

详细的 AI Model Gateway 配置说明。

> 💡 **重要提示：** 本项目支持**任何提供 OpenAI 兼容 API** 的平台。对于有专用插件的平台（ModelScope、NVIDIA、OpenRouter），可以自动获取免费模型列表；对于其他平台，需要手动配置模型信息。

> 📖 **单个字段到底控制什么**（默认值、取值单位、写法示例、改完是热重载还是需要重启、容易踩的坑）
> 只有一份来源：`openai_proxy/config_store/field_docs.py`，管理台「模型配置」页与
> `POST /admin/config/schema` 直接展示它（见 [管理台前端](ADMIN_UI.md) 的「字段字典」一节）。
> 本文讲的是结构、写法与环境变量，不重复逐字段语义。

## 📋 目录

- [环境变量配置](#环境变量配置)
- [平台配置](#平台配置)
- [插件系统详解](#插件系统详解)
- [OpenClaw 集成配置](#openclaw-集成配置)
- [高级配置选项](#高级配置选项)
  - [审计与用量统计（顶层 `audit:` 段）](#审计与用量统计顶层-audit-段)
  - [透传接口（顶层 `passthrough:` 段）](#透传接口顶层-passthrough-段)
  - [管理接口与管理台](#管理接口与管理台)

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

# Gateway 认证密钥（可选，留空则跳过认证）
GATEWAY_API_KEY=sk-your-secret-key
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

### 审计与用量统计（顶层 `audit:` 段）

每次上游调用都会记一行审计（用量、耗时、首块、tokens/s、故障转移、错误分类），落在 SQLite。
优先级：**内置默认值 → `AUDIT_*` 环境变量 → `models.yaml` 顶层 `audit:` 段**（YAML 最高）。

```yaml
audit:
  enabled: true
  db_path: "data/audit.db"
  retention_days: 30          # 审计明细保留天数，启动时与运行中定期清理；全量日用量永久保留
  queue_size: 10000           # 内存队列容量，满了丢弃最旧行并计数
  batch_size: 200             # 单次写库的最大行数
  flush_interval: 2.0         # 后台刷盘间隔（秒）
  record_failed_only: false   # 只记失败尝试，省磁盘但看不到用量
  include_request_meta: false # 是否记录消息条数；任何情况下都不记录消息内容
  prometheus_enabled: true    # 审计落库时同步喂 Prometheus
  metrics_label_model: false  # ⚠️ 目前真正生效的是环境变量 METRICS_LABEL_MODEL（启动时读）
```

⚠️ **这一段的每个字段都只在启动时应用一次**：改完 `audit:` 段必须自行重启，进程内热重载不会应用它。

对应环境变量：`AUDIT_ENABLED`、`AUDIT_DB_PATH`、`AUDIT_RETENTION_DAYS`、`AUDIT_QUEUE_SIZE`、
`AUDIT_BATCH_SIZE`、`AUDIT_FLUSH_INTERVAL`、`AUDIT_RECORD_FAILED_ONLY`、`AUDIT_INCLUDE_REQUEST_META`、
`AUDIT_PROMETHEUS`、`METRICS_LABEL_MODEL`。

完整说明（统计口径、字段清单、查询方式）见 [审计与用量统计](./AUDIT.md)。

### 透传接口（顶层 `passthrough:` 段）

把任意第三方接口按前缀原样转发，同时复用网关的用户鉴权与审计：

```yaml
passthrough:
  example-service:
    prefix: "/proxy/example-service"      # 必须以 /proxy/ 开头
    upstream: "https://api.example.com/v1" # 剩余路径与查询串拼在它后面
    apiKey: "${EXAMPLE_PLATFORM_API_KEY}"  # 客户端密钥不会转发到上游
    timeout: 300
    dimensions:                            # 键名就是汇总时的分组名
      model: "body.model"
      size: "body.size"
```

客户端请求 `/proxy/example-service/assets/render?size=1024`
会转发到 `https://api.example.com/v1/assets/render?size=1024`。

维度支持 `body.` / `query.` / `header.` / `path.` 四种来源，会进入审计的 `dimensions` 与永久日汇总表。
`platform` / `model` / `model_id` / `selector` 是标准维度键，配置同名维度即可覆盖默认值。

完整说明见 [透传接口与维度用量](./PASSTHROUGH.md)。

### 管理接口与管理台

`/admin/*` 与 `/metrics` 一律要求 `GATEWAY_API_KEY`；未配置该密钥时管理接口返回 403（默认不对外裸露）。

```env
GATEWAY_API_KEY=sk-your-admin-key

# 用户级密钥：在管理台「用户密钥」页新增/删除，只访问 /v1/*，用于按用户统计流量
API_KEYS_DB_PATH=data/api_keys.db

ADMIN_UI_ENABLED=true        # 管理台前端 /admin/ui（false 则不提供）
CONFIG_API_ENABLED=true      # 配置读写接口 /admin/config/*（false 则不提供）
CONFIG_BACKUP_DIR=data/config_backups   # 每次写盘前的自动备份目录
MODEL_CONFIG_FILE=models.yaml           # 命令行默认读取的配置文件
```

用户密钥只保存 SHA-256 摘要，明文只在创建时返回一次；删除后立即失效。管理密钥仍可访问数据面，
并按 `admin` 身份记账；未启用鉴权时的流量同样记 `admin`。

改配置有三种等价做法：管理台页面、`manage.py` 命令行、直接编辑 `models.yaml`。
无论哪种都不会自动重启服务：需要重启时只会提示，由你自己动手。
详见 [管理台前端](./ADMIN_UI.md) 与 [命令行管理工具](./CONFIG_CLI.md)。

---

## 相关文档

- [审计与用量统计](./AUDIT.md)
- [管理台前端](./ADMIN_UI.md)
- [命令行管理工具](./CONFIG_CLI.md)
- [监控与运维指南](./MONITORING.md)
- [负载均衡策略](./LOAD_BALANCING.md)
- [部署指南](./DEPLOYMENT.md)
- [安全注意事项](./SECURITY.md)
