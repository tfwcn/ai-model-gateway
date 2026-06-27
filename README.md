# 🤖 AI Model Gateway

![AI Model Gateway](docs/img.png)

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104+-green.svg)](https://fastapi.tiangolo.com/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

> **智能多平台免费 AI 模型网关服务** — 自动故障转移、负载均衡、协议转换、OpenAI 兼容接口
>
> **Smart Multi-Platform Free AI Model Gateway** — Auto failover, load balancing, protocol conversion, OpenAI-compatible API

💡 **定位说明：** 支持**任何提供 OpenAI 兼容 API** 的平台。内置插件可自动爬取 ModelScope、NVIDIA、OpenRouter 的免费模型；其他平台需手动配置。

🌐 Languages: [中文](README.md) | [English](README_EN.md)

**[快速开始](#-快速开始)** · **[API 参考](#-api-参考)** · **[配置指南](docs/CONFIGURATION_GUIDE.md)** · **[监控运维](docs/MONITORING.md)** · **[贡献指南](#-贡献指南)**

---

## ✨ 核心价值

### 解决什么问题？

- ❌ 免费模型经常调用失败或额度用尽
- ❌ 需要手动维护多个平台的 API 密钥和配置
- ❌ 模型列表过时，无法及时获取新发布的免费模型
- ❌ 非标准工具调用格式（NVIDIA JSON、Minimax XML）无法兼容
- ❌ 缺乏监控，不知道哪个平台出了问题

**AI Model Gateway** 为你提供：

- 🔄 **智能故障转移**：当某个平台失败时，自动切换到备用平台
- ⚖️ **权重负载均衡**：基于配置的优先级分配请求
- 🔌 **插件调度系统**：定时爬取最新免费模型列表（支持 Playwright 浏览器自动化）
- 🛡️ **智能错误分类**：7 种错误类型精细化处理，区分可重试/不可重试
- 🔧 **工具调用格式转换**：自动将非标准 tool call 格式（NVIDIA JSON、Minimax XML）转换为标准 OpenAI 格式
- 📡 **OpenAI Responses API**：支持 `/v1/responses` 协议转换（含流式）
- 🔑 **Bearer Token 认证**：可选 `GATEWAY_API_KEY` 保护所有接口
- 📊 **Prometheus 监控**：实时监控请求量、延迟、错误率
- 🚀 **零客户端改造**：完全兼容 OpenAI API

## 🆕 最新特性

| 特性 | 说明 |
|------|------|
| **Responses API** | 完整支持 `/v1/responses` 端点，Chat API ↔ Responses API 双向协议转换 |
| **工具调用转换器** | 自动识别 NVIDIA JSON / Minimax XML 等非标准格式并转换为标准 tool_calls |
| **流式工具调用缓冲** | 智能缓冲检测流式响应中的非标准工具调用，零延迟切换 |
| **工具能力测试** | 新增模型时自动测试 tool call 能力，只保留真正支持的模型 |
| **模型重试机制** | 单模型支持多次重试（可配置 `retry_count`），基于错误类型智能重试 |
| **模型别名 & 黑名单** | `all_aliases` 支持多别名，`blacklist` 支持 `*` 通配符过滤 |
| **GATEWAY_API_KEY 认证** | 可选 Bearer Token 认证保护所有 API 接口 |
| **Redis 缓存 & 会话** | 响应缓存 + Responses API 会话历史，Redis 不可用时自动降级为文件 |
| **SSE 事件标准化** | 统一 SSE 格式，消除多端点重复代码 |
| **错误分类系统** | 7 种错误类型（超时/连接/认证/限流/服务器/格式/模型），精细控制重试和禁用策略 |

### 对比传统方案

| 特性 | AI Model Gateway | 直接调用平台 API | 其他代理方案 |
|------|------------------|------------------|--------------|
| 自动故障转移 | ✅ 智能切换 | ❌ 需手动处理 | ⚠️ 部分支持 |
| 多平台整合 | ✅ 5+ 平台 | ❌ 单平台 | ⚠️ 2-3 平台 |
| 动态模型发现 | ✅ 插件调度 + 定时爬虫 | ❌ 手动维护 | ❌ 静态配置 |
| OpenAI Responses API | ✅ 完整支持 | ❌ 仅 Chat | ❌ 不支持 |
| 工具调用格式转换 | ✅ NVIDIA/Minimax 等 | ❌ 需手动处理 | ❌ 不支持 |
| 监控告警 | ✅ Prometheus | ❌ 无 | ⚠️ 基础日志 |
| 错误分类 | ✅ 7 种类型 | ❌ 统一处理 | ⚠️ 简单分类 |
| 缓存支持 | ✅ Memory/Redis | ❌ 无 | ⚠️ 基础缓存 |

---

## 🚀 快速开始

### 1️⃣ 安装

```bash
git clone <repo-url>
cd openai-proxy
pip install -r requirements.txt
# 安装 Playwright 浏览器（用于爬虫插件）
playwright install --with-deps chromium
```

### 2️⃣ 配置

```bash
cp .env.example .env
cp models.example.yaml models.yaml
nano .env  # 填入你的 API 密钥（可选设置 GATEWAY_API_KEY 开启认证）
```

详细配置请参考：[📖 配置指南](docs/CONFIGURATION_GUIDE.md)

### 3️⃣ 启动

```bash
python run.py
```

或使用启动脚本（自动管理 Redis 和虚拟环境）：

```bash
bash start.sh
```

服务运行在 `http://localhost:8000`

### 4️⃣ 测试

```bash
# 获取可用模型列表
curl http://localhost:8000/v1/models

# 发送聊天请求（按权重选择平台）
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "all",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'

# 发送 Responses API 请求
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{
    "model": "all",
    "input": "Hello!"
  }'
```

---

## 📡 API 参考

### 主要端点

| 端点 | 方法 | 描述 |
|------|------|------|
| `/v1/chat/completions` | POST | OpenAI Chat Completions（需认证） |
| `/v1/responses` | POST | OpenAI Responses API（需认证） |
| `/v1/models` | GET | 获取可用模型列表（支持筛选/分页，需认证） |
| `/health` | GET | 基本健康检查（免认证） |
| `/health/detailed` | GET | 详细健康检查 |
| `/metrics` | GET | Prometheus 监控指标 |

### 模型选择策略

三种模型选择器格式：

- **`"all"`** — 遍历所有平台所有模型，按权重排序（默认）
- **`"modelscope"`** — 指定平台所有模型
- **`"modelscope|Qwen/Qwen2.5-7B-Instruct"`** — 指定平台的指定模型

`/v1/models` 端点支持查询参数：

- `provider` — 按平台筛选
- `capability` — 按能力筛选（chat/completion/embedding）
- `q` — 关键字搜索
- `limit` / `offset` — 分页

### 模型列表响应格式

```json
{
  "data": [
    {
      "id": "all",
      "name": "所有平台所有模型",
      "provider": "all",
      "capabilities": ["chat", "completion"],
      "metadata": { "type": "group", "description": "..." }
    },
    {
      "id": "modelscope|Qwen-Qwen2.5-7B-Instruct",
      "name": "Qwen/Qwen2.5-7B-Instruct",
      "provider": "modelscope",
      "capabilities": ["chat", "completion"],
      "metadata": {
        "base_url": "https://api-inference.modelscope.cn/v1",
        "timeout": 10,
        "weight": 1,
        "enabled": true,
        "retry_count": 3
      }
    }
  ],
  "meta": { "total": 42, "limit": 50, "offset": 0 }
}
```

---

## 🏗️ 架构概览

```
┌─────────────┐
│   Client    │ (OpenClaw / 任何 OpenAI 兼容客户端)
└──────┬──────┘
       │
       ▼
┌──────────────────────────────────────────┐
│          AI Model Gateway                │
│                                          │
│  ┌────────────────────────────────────┐  │
│  │         FastAPI 路由层             │  │
│  │  /v1/chat/completions              │  │
│  │  /v1/responses (协议转换)          │  │
│  │  /v1/models (带筛选/分页)          │  │
│  │  /health /metrics                  │  │
│  └──────────┬─────────────────────────┘  │
│             │                             │
│  ┌──────────▼─────────────────────────┐  │
│  │       Failover Manager             │  │
│  │  ┌──────────┐  ┌────────────────┐  │  │
│  │  │ 权重排序  │  │ 故障转移/重试  │  │  │
│  │  └──────────┘  └────────────────┘  │  │
│  │  ┌──────────┐  ┌────────────────┐  │  │
│  │  │错误分类器 │  │ 模型状态管理   │  │  │
│  │  │(7种类型)  │  │(周期禁用/恢复) │  │  │
│  │  └──────────┘  └────────────────┘  │  │
│  └────────────────────────────────────┘  │
│                                          │
│  ┌──────────┐  ┌──────────┐             │
│  │ Plugins  │  │  Cache   │             │
│  │(Scraper) │  │ (Memory/ │             │
│  │(Scheduler)│  │  Redis)  │             │
│  └──────────┘  └──────────┘             │
│  ┌────────────────────────────────────┐  │
│  │  Responses Adapter                 │  │
│  │  Chat ↔ Responses 协议转换         │  │
│  │  流式事件转换 + 会话管理            │  │
│  └────────────────────────────────────┘  │
└──────┬──────────┬───────────────────────┘
       │          │
       ▼          ▼
┌──────────┐ ┌──────────┐
│Platform A│ │Platform B│ ... (多平台)
└──────────┘ └──────────┘
```

### 核心模块

- **Core**：配置加载、插件管理、缓存抽象层（MemoryCache / RedisCache）
- **Model**：故障转移管理器、错误分类器、模型状态管理、工具能力测试
- **Scraper**：Playwright 网页爬虫基类 + 定时任务调度器（APScheduler）
- **Adapter**：Responses API ↔ Chat API 协议转换器
- **Utils**：SSE 事件解析器、工具调用格式转换器、流式工具调用缓冲器、Prometheus 指标、会话存储（Redis/File 双模）

### 工作流程

1. **接收请求** → 客户端发送 OpenAI 兼容请求
2. **模型选择** → 解析 `model` 选择器（all / 平台 / 平台|模型）
3. **故障转移** → 按权重排序平台，逐个尝试；单模型失败自动重试
4. **错误分类** → 识别 7 种错误类型，区分可重试/不可重试，周期禁用模型
5. **协议转换**（可选）→ Responses API 请求自动转换为 Chat API
6. **返回结果** → 返回标准 OpenAI 格式响应
7. **监控记录** → 记录 Prometheus 指标和错误日志

---

## 🔧 配置详解

### 环境变量

创建 `.env` 文件：

```bash
cp .env.example .env
nano .env
```

```env
# API 密钥（按需配置）
MODELSCOPE_API_KEY=your-modelscope-api-key
OPENROUTER_API_KEY=your-openrouter-api-key
NVIDIA_API_KEY=your-nvidia-api-key
OPENAI_API_KEY=your-openai-api-key

# Gateway 认证（可选，为空则跳过）
GATEWAY_API_KEY=your-gateway-api-key

# Redis 配置（可选）
REDIS_URL=redis://localhost:6379
RESPONSES_SESSION_TTL=86400
```

### 平台配置示例

```yaml
modelscope:
  baseUrl: "https://api-inference.modelscope.cn/v1"
  apiKey: "${MODELSCOPE_API_KEY}"
  weight: 10
  timeout: 30
  enabled: true
  quota_period: "daily"       # 额度刷新周期
  retry_count: 3              # 单模型重试次数
  plugin:                     # 插件配置（可选）
    code: "plugin.modelscope"
    cache_timeout: 3600
    args:
      scrape_url: "https://www.modelscope.cn/models?filter=inference_type&..."
      max_models: 10
      scraper_timeout: 60
      enable_scheduled_task: true
      schedule_cron: "0 2 * * *"
  blacklist:                  # 黑名单（支持通配符）
    - "iic/*"
  models: []                  # 静态模型列表（可选）
```

### 工具调用能力测试

插件可配置自动测试模型的工具调用能力，只保留支持的模型：

```yaml
modelscope:
  plugin:
    args:
      enable_tool_capability_test: true
      max_concurrent_tests: 1
      test_timeout_seconds: 60
```

### 完整配置

详细配置请参考：[📖 配置指南](docs/CONFIGURATION_GUIDE.md)

---

## 📖 详细文档

### 配置与部署

- [🔧 完整配置指南](docs/CONFIGURATION_GUIDE.md)
- [🐳 Docker 部署指南](docs/DEPLOYMENT.md)
- [🚨 安全注意事项](docs/SECURITY.md)

### 高级功能

- [📊 监控与运维](docs/MONITORING.md)
- [⚡ 负载均衡策略](docs/LOAD_BALANCING.md)
- [🛡️ 错误分类系统](docs/error-classification.md)
- [🔧 工具调用格式转换器](docs/TOOL_CALL_CONVERTER.md)
- [📡 Responses API 协议](docs/protocol_summary.md)

### 插件系统

- [🔌 插件配置 FAQ](docs/PLUGIN_FAQ.md)
- [📖 NVIDIA 爬虫文档](docs/NVIDIA_SCRAPER_README.md)
- [📖 ModelScope 爬虫文档](docs/MODELSCOPE_SCRAPER_README.md)
- [📖 OpenRouter 爬虫文档](docs/OPENROUTER_SCRAPER_README.md)

---

## 🤝 贡献指南

### 开发环境设置

```bash
git clone <repo-url>
cd openai-proxy
pip install -r requirements.txt
pip install -r requirements-dev.txt

# 运行测试
pytest tests/ -v

# 代码格式化
black openai_proxy/
```

### 贡献流程

1. Fork 本仓库
2. 创建特性分支 (`git checkout -b feature/amazing-feature`)
3. 提交更改 (`git commit -m 'Add amazing feature'`)
4. 推送到分支 (`git push origin feature/amazing-feature`)
5. 创建 Pull Request

### 代码规范

- 遵循 PEP 8 Python 代码风格
- 添加单元测试覆盖新功能
- 更新相关文档

---

## 📄 许可证

本项目采用 MIT 许可证 — 详见 [LICENSE](LICENSE) 文件

---

## 🙏 致谢

- [FastAPI](https://fastapi.tiangolo.com/)
- [aiohttp](https://docs.aiohttp.org/)
- [Playwright](https://playwright.dev/)
- [Prometheus](https://prometheus.io/)
- [APScheduler](https://apscheduler.readthedocs.io/)

---

## 📞 联系方式

- 💬 GitHub Issues: [提交问题](https://github.com/tfwcn/ai-model-gateway/issues)
- 📖 文档: [完整文档](docs/)

---

<div align="center">

**⭐ 如果这个项目对你有帮助，请给个 Star！**

</div>
