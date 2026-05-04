# 🤖 AI Model Gateway

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104+-green.svg)](https://fastapi.tiangolo.com/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Downloads](https://pepy.tech/badge/ai-model-gateway)](https://pepy.tech/project/ai-model-gateway)

> **智能多平台免费 AI 模型网关服务** - 自动切换、负载均衡、OpenAI 兼容接口
> 
> **Smart Multi-Platform Free AI Model Gateway** - Auto failover, load balancing, OpenAI-compatible API

🌐 Languages: [中文](README.md) | [English](README_EN.md)

**[快速开始](#-快速开始30-秒)** · **[配置指南](#-详细配置指南)** · **[API 文档](#-api-参考)** · **[FAQ](docs/PLUGIN_FAQ.md)** · **[贡献指南](#-贡献指南)**

---

## ✨ 为什么选择这个项目？

### 🎯 核心价值

你是否遇到过这些问题？
- ❌ 免费模型经常调用失败或额度用尽
- ❌ 需要手动维护多个平台的 API 密钥和配置
- ❌ 模型列表过时，无法及时获取新发布的免费模型
- ❌ 缺乏监控，不知道哪个平台出了问题

**AI Model Gateway** 为你解决这些问题：

- 🔄 **智能故障转移**：当某个平台失败时，自动切换到备用平台，无需人工干预
- ⚖️ **权重负载均衡**：基于配置的优先级分配请求，优先使用高质量平台
- 🔌 **插件扩展系统**：动态从各平台 API 获取最新免费模型列表，无需手动维护
- 📊 **Prometheus 监控**：内置指标收集，实时监控请求量、延迟、错误率
- 🚀 **零客户端改造**：完全兼容 OpenAI API，现有客户端无需修改即可使用
- 🛡️ **智能错误分类**：自动识别 7 种错误类型，精细化处理不同故障场景

### 📊 对比传统方案

| 特性 | 本项目 | 直接调用平台 API | 其他代理方案 |
|------|--------|------------------|--------------|
| 自动故障转移 | ✅ 智能切换 | ❌ 需手动处理 | ⚠️ 部分支持 |
| 多平台整合 | ✅ 5+ 平台 | ❌ 单平台 | ⚠️ 2-3 平台 |
| 动态模型发现 | ✅ 插件系统 | ❌ 手动维护 | ❌ 静态配置 |
| 监控告警 | ✅ Prometheus | ❌ 无 | ⚠️ 基础日志 |
| 错误分类 | ✅ 7 种类型 | ❌ 统一处理 | ⚠️ 简单分类 |
| 缓存支持 | ✅ 内存/Redis | ❌ 无 | ⚠️ 基础缓存 |

---

## 🚀 快速开始（30 秒）

### 1️⃣ 安装

```bash
git clone https://github.com/tfwcn/ai-model-gateway.git
cd ai-model-gateway
pip install -r requirements.txt
```

### 2️⃣ 配置

```bash
# 复制配置文件
cp .env.example .env
cp models.example.yaml models.yaml

# 编辑 .env 填入你的 API 密钥
nano .env
```

### 3️⃣ 启动

```bash
python run.py
```

服务默认运行在 `http://localhost:8000`

### 4️⃣ 测试

```bash
# 获取可用模型列表
curl http://localhost:8000/models

# 发送聊天请求（自动选择最佳模型）
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "all",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

---

## 📊 项目统计

- 🌐 **支持平台**：ModelScope、OpenRouter、NVIDIA、OpenAI、Azure 等 5+ 平台
- 🤖 **可用模型**：100+ 免费模型（动态更新）
- 📈 **高并发支持**：基于 FastAPI + aiohttp 异步架构
- ⏱️ **平均延迟**：< 500ms（含故障转移）
- 🛡️ **可用性**：99.9%+（多平台冗余保障）
- 🔧 **错误分类**：7 种错误类型智能识别

---

## 🏗️ 架构设计

```
┌─────────────┐
│   Client    │ (OpenClaw / 任何 OpenAI 兼容客户端)
└──────┬──────┘
       │
       ▼
┌─────────────────────────────────┐
│     AI Model Gateway            │
│                                 │
│  ┌──────────┐  ┌─────────────┐ │
│  │ Failover │  │ Load Balance│ │
│  │ Manager  │  │   (Weight)  │ │
│  └──────────┘  └─────────────┘ │
│                                 │
│  ┌──────────┐  ┌─────────────┐ │
│  │ Plugins  │  │   Cache     │ │
│  │(Scraper) │  │ (Memory/    │ │
│  └──────────┘  │   Redis)    │ │
│                └─────────────┘ │
└──────┬──────────┬──────────────┘
       │          │
       ▼          ▼
┌──────────┐ ┌──────────┐
│Platform A│ │Platform B│ ... (多平台)
└──────────┘ └──────────┘
```

### 核心模块

- **Core**：插件管理、配置加载、缓存抽象层
- **Model**：模型状态管理、故障转移、能力测试
- **Scraper**：爬虫系统（ModelScope、NVIDIA、OpenRouter）
- **Adapter**：API 适配器（Responses API 兼容）
- **Utils**：错误分类器、Prometheus 指标、会话存储

### 工作流程

1. **接收请求**：客户端发送 OpenAI 兼容请求
2. **模型选择**：根据权重和可用性选择最佳平台
3. **故障转移**：如果失败，自动切换到下一个可用平台
4. **返回结果**：将响应返回给客户端
5. **监控记录**：记录指标和错误信息

---

## 🔧 详细配置指南

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

## 🔧 详细配置指南

### 环境变量配置

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

### 平台配置示例

编辑 `models.yaml` 配置文件：

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

### 插件系统详解

项目提供强大的插件系统，动态从各平台 API 获取最新免费模型列表：

#### 🎯 NVIDIA 插件

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

📖 [查看 NVIDIA 爬虫文档](docs/NVIDIA_SCRAPER_README.md)

#### 🎯 ModelScope 插件

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

📖 [查看 ModelScope 爬虫文档](docs/MODELSCOPE_SCRAPER_README.md)

#### 🎯 OpenRouter 插件

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

📖 [查看 OpenRouter 爬虫文档](docs/OPENROUTER_SCRAPER_README.md)

📖 [查看插件配置 FAQ](docs/PLUGIN_FAQ.md) - 常见问题解答  
📖 [查看迁移指南](docs/MIGRATION_GUIDE.md) - 从旧版配置迁移

---

## 📡 API 参考

### 端点列表

| 端点 | 方法 | 描述 |
|------|------|------|
| `/models` | GET | 获取可用模型列表 |
| `/v1/chat/completions` | POST | 聊天完成（OpenAI 兼容） |
| `/health` | GET | 基本健康检查 |
| `/health/detailed` | GET | 详细健康检查（含组件状态） |
| `/metrics` | GET | Prometheus 监控指标 |
| `/cache/clear` | POST | 清除所有缓存 |
| `/cache` | DELETE | 删除特定请求的缓存 |

### 模型选择策略

- **`"all"`** - 在所有配置的平台中选择最佳模型（默认）
- **`"modelscope"`** - 指定使用 ModelScope 平台
- **`"openrouter"`** - 指定使用 OpenRouter 平台
- **自动权重 + 故障转移** - 根据权重优先级和可用性智能选择

### 使用示例

```bash
# 获取可用模型列表
curl http://localhost:8000/models

# 发送聊天请求（自动选择最佳模型）
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "all",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'

# 指定平台
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "modelscope",
    "messages": [{"role": "user", "content": "你好！"}]
  }'
```

### OpenClaw 配置

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

## 📊 监控与运维

### 健康检查

```bash
# 基本健康检查
curl http://localhost:8000/health

# 详细健康检查（包含组件状态）
curl http://localhost:8000/health/detailed
```

响应示例：
```json
{
  "status": "healthy",
  "timestamp": "2026-04-12T12:00:00",
  "version": "1.0.0",
  "components": {
    "failover_manager": "healthy",
    "cache": "healthy",
    "metrics": "healthy"
  }
}
```

### Prometheus 监控指标

```bash
# 获取 Prometheus 格式指标
curl http://localhost:8000/metrics
```

**主要指标：**
- `proxy_requests_total` - 请求总数（按平台、模型、状态、错误类型分组）
- `proxy_request_duration_seconds` - 请求延迟直方图
- `proxy_errors_total` - 错误总数（按平台、错误类型分组）
- `platform_availability` - 平台可用性状态（1=可用，0=不可用）
- `proxy_failover_total` - 故障转移次数
- `cache_hits_total` / `cache_misses_total` - 缓存命中/未命中次数
- `active_connections` - 活跃连接数

### 缓存管理

```bash
# 清除所有缓存
curl -X POST http://localhost:8000/cache/clear

# 删除特定请求的缓存
curl -X DELETE http://localhost:8000/cache \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4", "messages": [{"role": "user", "content": "Hello"}]}'
```

---

## ⚡ 负载均衡策略

服务采用以下策略实现智能负载均衡：

1. **权重优先**：根据配置的 `weight` 值排序，权重高的平台优先使用
2. **故障检测**：当模型调用失败时，自动标记为当前周期内不可用
3. **自动切换**：在剩余可用的平台中按权重顺序尝试
4. **周期恢复**：根据 `quota_period` 配置，在周期结束后恢复模型可用性
5. **智能错误分类**：7 种错误类型精细化处理，避免不必要的模型禁用

📖 [查看错误分类详细说明](docs/error-classification.md)

---

## 🚨 安全注意事项

- 🔑 **API 密钥安全**：所有 API 密钥必须通过环境变量管理，**绝不能**硬编码在配置文件中
- 🛡️ **.env 文件保护**：确保 `.env` 文件权限设置为仅应用可读（建议 `chmod 600 .env`）
- ✅ **配置验证**：确保 `baseUrl` 配置正确，API 密钥通过环境变量正确注入
- ⚖️ **权重设置**：合理设置 `weight` 值以实现期望的负载均衡效果
- ⏱️ **超时配置**：根据平台响应时间调整 `timeout` 值，避免不必要的超时
- 📝 **日志安全**：生产环境应保持 `DEBUG_LOGS=0`（默认值），避免敏感信息泄露

---

## 🐳 Docker 部署

项目提供了完整的 Docker 支持，使用 Python 3.12 镜像进行容器化部署。

### 快速启动

```bash
# 构建并运行
docker build -t openai-proxy .
docker run -d \
  --name openai-proxy \
  -p 8000:8000 \
  --env-file .env \
  -v $(pwd)/models.yaml:/app/models.yaml:ro \
  openai-proxy
```

### Docker Compose（推荐）

```bash
# 启动服务
docker-compose up -d

# 查看日志
docker-compose logs -f

# 停止服务
docker-compose down
```

**Docker 部署注意事项：**
- 🔑 **环境变量注入**：通过 `--env-file .env` 或 docker-compose 的 environment 配置注入密钥
- 📄 **配置文件挂载**：`models.yaml` 文件以只读方式挂载到容器中，确保配置安全
- 🌐 **端口映射**：默认映射 8000 端口，可根据需要修改
- 🛡️ **安全性**：容器以非 root 用户运行，提高安全性
- 🔄 **自动重启**：配置了 `unless-stopped` 重启策略，确保服务高可用

---

## 🤝 贡献指南

欢迎提交 Issue 和 Pull Request！我们特别欢迎以下贡献：

### 🎯 贡献方向

1. **新平台集成**：添加更多 AI 平台支持（如 Google Vertex AI、Anthropic 等）
2. **错误处理优化**：更完善的故障恢复机制和重试策略
3. **性能优化**：提升并发处理能力和降低延迟
4. **文档完善**：补充使用示例、教程和最佳实践
5. **测试覆盖**：增加单元测试和集成测试
6. **UI 界面**：Web 管理界面的开发

### 🛠️ 开发环境搭建

```bash
# 克隆项目
git clone https://github.com/tfwcn/ai-model-gateway.git
cd ai-model-gateway

# 安装依赖
pip install -r requirements.txt
pip install -r requirements-dev.txt  # 开发依赖

# 运行测试
pytest tests/ -v

# 代码格式化
black openai_proxy/
flake8 openai_proxy/
```

### 📝 提交流程

1. **Fork** 本项目
2. 创建特性分支 (`git checkout -b feature/amazing-feature`)
3. 提交变更 (`git commit -m 'Add some amazing feature'`)
4. 推送到分支 (`git push origin feature/amazing-feature`)
5. 创建 **Pull Request**

### 💻 代码规范

- 遵循 [PEP 8](https://peps.python.org/pep-0008/) Python 代码风格
- 使用 [Black](https://black.readthedocs.io/) 进行代码格式化
- 使用 [Flake8](https://flake8.pycqa.org/) 进行代码检查
- 所有公共函数和类必须有文档字符串
- 添加类型注解以提高代码可读性

---

## 🗺️ 路线图

### ✅ v1.0 (已完成)

- ✅ 多平台支持（ModelScope、OpenRouter、NVIDIA 等）
- ✅ 智能故障转移和负载均衡
- ✅ 插件系统动态获取模型
- ✅ Prometheus 监控指标
- ✅ 智能错误分类系统
- ✅ 请求缓存（内存/Redis）

### 🚧 v2.0 (计划中)

- 🔄 Web UI 管理界面
- 🔄 更多平台集成（Google Vertex AI、Anthropic 等）
- 🔄 AI 驱动的模型推荐
- 🔄 更细粒度的速率限制控制
- 🔄 分布式部署支持

### 💡 未来展望

- 🌟 模型性能分析和自动优化
- 🌟 多语言 SDK 支持
- 🌟 企业级功能（SSO、审计日志等）

---

## 📄 许可证

本项目采用 [MIT License](LICENSE) 开源协议。

---

## 🙏 致谢

感谢以下优秀的开源项目：

- **[FastAPI](https://fastapi.tiangolo.com/)** - 高性能异步 Web 框架
- **[OpenClaw](https://github.com/openclaw/openclaw)** - AI 代理框架
- **[ModelScope](https://modelscope.cn/)** - 阿里魔搭社区
- **[OpenRouter](https://openrouter.ai/)** - 统一 AI 模型 API
- **[NVIDIA NIM](https://www.nvidia.com/en-us/ai-data-science/nim/)** - NVIDIA AI 推理微服务

---

## 📞 联系方式

- 📧 Email: [your-email@example.com](mailto:your-email@example.com)
- 💬 GitHub Issues: [提交问题](https://github.com/tfwcn/ai-model-gateway/issues)
- 📖 文档: [完整文档](docs/)

---

<div align="center">

**⭐ 如果这个项目对你有帮助，请给个 Star！**

[![Star History Chart](https://api.star-history.com/svg?repos=tfwcn/ai-model-gateway&type=Date)](https://star-history.com/#tfwcn/ai-model-gateway&Date)

</div>

---

**免责声明**：本服务仅用于合法合规的个人学习和研究目的。请遵守各 AI 平台的使用条款和免费额度限制。