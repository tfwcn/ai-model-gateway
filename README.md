# 🤖 AI Model Gateway

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104+-green.svg)](https://fastapi.tiangolo.com/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

> **智能多平台免费 AI 模型网关服务** - 自动切换、负载均衡、OpenAI 兼容接口
> 
> **Smart Multi-Platform Free AI Model Gateway** - Auto failover, load balancing, OpenAI-compatible API

💡 **定位说明：** 本项目专注于聚合多个提供 **OpenAI 兼容 API** 的免费模型平台，不提供非 OpenAI 格式的 API 支持。

🌐 Languages: [中文](README.md) | [English](README_EN.md)

**[快速开始](#-快速开始)** · **[配置指南](docs/CONFIGURATION_GUIDE.md)** · **[API 文档](#-api-参考)** · **[监控运维](docs/MONITORING.md)** · **[贡献指南](#-贡献指南)**

---

## ✨ 核心价值

### 解决什么问题？

- ❌ 免费模型经常调用失败或额度用尽
- ❌ 需要手动维护多个平台的 API 密钥和配置
- ❌ 模型列表过时，无法及时获取新发布的免费模型
- ❌ 缺乏监控，不知道哪个平台出了问题

**AI Model Gateway** 为你提供：

- 🔄 **智能故障转移**：当某个平台失败时，自动切换到备用平台
- ⚖️ **权重负载均衡**：基于配置的优先级分配请求
- 🔌 **插件扩展系统**：动态获取最新免费模型列表
- 📊 **Prometheus 监控**：实时监控请求量、延迟、错误率
- 🚀 **零客户端改造**：完全兼容 OpenAI API
- 🛡️ **智能错误分类**：7 种错误类型精细化处理

### 对比传统方案

| 特性 | AI Model Gateway | 直接调用平台 API | 其他代理方案 |
|------|------------------|------------------|--------------|
| 自动故障转移 | ✅ 智能切换 | ❌ 需手动处理 | ⚠️ 部分支持 |
| 多平台整合 | ✅ 5+ 平台 | ❌ 单平台 | ⚠️ 2-3 平台 |
| 动态模型发现 | ✅ 插件系统 | ❌ 手动维护 | ❌ 静态配置 |
| 监控告警 | ✅ Prometheus | ❌ 无 | ⚠️ 基础日志 |
| 错误分类 | ✅ 7 种类型 | ❌ 统一处理 | ⚠️ 简单分类 |

---

## 🚀 快速开始

### 1️⃣ 安装

```bash
git clone https://github.com/tfwcn/ai-model-gateway.git
cd ai-model-gateway
pip install -r requirements.txt
```

### 2️⃣ 配置

```bash
cp .env.example .env
cp models.example.yaml models.yaml
nano .env  # 填入你的 API 密钥
```

详细配置请参考：[📖 配置指南](docs/CONFIGURATION_GUIDE.md)

### 3️⃣ 启动

```bash
python run.py
```

服务运行在 `http://localhost:8000`

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

## 📊 项目亮点

- 🌐 **支持平台**：ModelScope、OpenRouter、NVIDIA、OpenAI、Azure 等 5+ 平台
- 🤖 **可用模型**：100+ 免费模型（动态更新）
- 📈 **高并发支持**：基于 FastAPI + aiohttp 异步架构
- ⏱️ **平均延迟**：< 500ms（含故障转移）
- 🛡️ **可用性**：99.9%+（多平台冗余保障）

---

## 🏗️ 架构概览

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

---

## 📡 API 参考

### 主要端点

| 端点 | 方法 | 描述 |
|------|------|------|
| `/models` | GET | 获取可用模型列表 |
| `/v1/chat/completions` | POST | 聊天完成（OpenAI 兼容） |
| `/health` | GET | 基本健康检查 |
| `/health/detailed` | GET | 详细健康检查 |
| `/metrics` | GET | Prometheus 监控指标 |

### 模型选择策略

- **`"all"`** - 在所有配置的平台中选择最佳模型（默认）
- **`"modelscope"`** - 指定使用 ModelScope 平台
- **`"openrouter"`** - 指定使用 OpenRouter 平台
- **自动权重 + 故障转移** - 根据权重优先级和可用性智能选择

### OpenClaw 集成

详细配置请参考：[📖 OpenClaw 配置示例](docs/CONFIGURATION_GUIDE.md#openclaw-集成配置)

---

## 📚 详细文档

### 配置与部署

- [🔧 完整配置指南](docs/CONFIGURATION_GUIDE.md) - 环境变量、平台配置、插件系统
- [🐳 Docker 部署指南](docs/DEPLOYMENT.md) - Docker、Docker Compose、Kubernetes
- [🚨 安全注意事项](docs/SECURITY.md) - API 密钥管理、访问控制

### 高级功能

- [📊 监控与运维](docs/MONITORING.md) - Prometheus、Grafana、日志管理
- [⚡ 负载均衡策略](docs/LOAD_BALANCING.md) - 权重配置、故障转移机制
- [🛡️ 错误分类系统](docs/error-classification.md) - 7 种错误类型详解

### 插件系统

- [🔌 插件配置 FAQ](docs/PLUGIN_FAQ.md) - 常见问题解答
- [📖 NVIDIA 爬虫文档](docs/NVIDIA_SCRAPER_README.md)
- [📖 ModelScope 爬虫文档](docs/MODELSCOPE_SCRAPER_README.md)
- [📖 OpenRouter 爬虫文档](docs/OPENROUTER_SCRAPER_README.md)

### 其他

- [🔄 迁移指南](docs/MIGRATION_GUIDE.md) - 从旧版配置迁移
- [🗺️ 路线图](docs/ROADMAP.md) - 未来规划

---

## 🤝 贡献指南

我们欢迎所有形式的贡献！

### 开发环境设置

```bash
# 克隆项目
git clone https://github.com/tfwcn/ai-model-gateway.git
cd ai-model-gateway

# 安装依赖
pip install -r requirements.txt

# 运行测试
pytest tests/
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

## 🗺️ 路线图

查看完整路线图：[📖 ROADMAP.md](docs/ROADMAP.md)

### v1.0 (已完成) ✅
- [x] 基础故障转移机制
- [x] 权重负载均衡
- [x] 插件系统框架
- [x] Prometheus 监控

### v2.0 (进行中) 🚧
- [ ] 更多平台支持（Anthropic、Cohere）
- [ ] 高级缓存策略（分布式 Redis）
- [ ] Web UI 管理界面
- [ ] 更细粒度的错误分类

### v3.0 (计划中) 📋
- [ ] AI 驱动的动态权重调整
- [ ] 成本优化建议
- [ ] 企业级 RBAC 权限系统

---

## 📄 许可证

本项目采用 MIT 许可证 - 详见 [LICENSE](LICENSE) 文件

---

## 🙏 致谢

感谢以下开源项目：

- [FastAPI](https://fastapi.tiangolo.com/) - 高性能 Web 框架
- [aiohttp](https://docs.aiohttp.org/) - 异步 HTTP 客户端/服务器
- [Playwright](https://playwright.dev/) - 浏览器自动化
- [Prometheus](https://prometheus.io/) - 监控系统

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
