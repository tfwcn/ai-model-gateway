# 🗺️ 项目功能

AI Model Gateway 当前已实现的功能列表。

## 📋 目录

- [核心功能](#核心功能)
- [高级功能](#高级功能)
- [监控与运维](#监控与运维)
- [插件系统](#插件系统)
- [API 兼容性](#api-兼容性)

---

## 核心功能

### 故障转移机制

- ✅ 自动检测平台失败
- ✅ 智能切换到备用平台
- ✅ 可配置的超时和重试策略
- ✅ 7 种错误类型精细化处理

### 负载均衡

- ✅ 基于权重的优先级分配
- ✅ 支持多平台并行配置
- ✅ 动态调整权重
- ✅ 健康检查与自动恢复

### OpenAI API 兼容

- ✅ `/v1/chat/completions` 端点
- ✅ 完全兼容现有客户端
- ✅ 零改造成本
- ✅ 支持流式和非流式响应

---

## 高级功能

### 缓存系统

- ✅ 内存缓存（默认）
- ✅ Redis 缓存支持
- ✅ 可配置的 TTL
- ✅ 缓存命中率统计

### 插件系统

- ✅ 可扩展的插件架构
- ✅ ModelScope 爬虫
- ✅ NVIDIA 爬虫
- ✅ OpenRouter 爬虫
- ✅ 动态模型发现
- ✅ 缓存模型列表

### 错误分类

- ✅ RateLimitError - 短期禁用
- ✅ QuotaExceededError - 按周期禁用
- ✅ AuthenticationError - 长期禁用
- ✅ NotFoundError - 长期禁用
- ✅ TimeoutError - 立即重试
- ✅ ConnectionError - 立即重试
- ✅ ServerError - 立即重试

详细文档：[📖 错误分类系统](./error-classification.md)

---

## 监控与运维

### Prometheus 监控

- ✅ 内置指标收集
- ✅ `/metrics` 端点
- ✅ Grafana 仪表板支持
- ✅ 关键指标：请求量、延迟、错误率、故障转移次数

### 健康检查

- ✅ `/health` 基本检查
- ✅ `/health/detailed` 详细检查
- ✅ 组件状态监控
- ✅ 平台可用性报告

### 日志管理

- ✅ 结构化日志输出
- ✅ 可配置的日志级别
- ✅ 请求追踪 ID
- ✅ 错误详情记录

---

## 插件系统

### 支持的爬虫插件

| 插件 | 功能 | 缓存策略 |
|------|------|----------|
| **ModelScope** | 抓取免费文本生成模型 | 1小时 |
| **NVIDIA** | 抓取 NIM 免费预览模型 | 1小时 |
| **OpenRouter** | 按价格过滤免费模型 | 5分钟 |

### 插件特性

- ✅ 动态加载，无需重启
- ✅ 独立的缓存配置
- ✅ 自定义过滤参数
- ✅ 错误隔离机制

详细文档：
- [🔌 插件配置 FAQ](./PLUGIN_FAQ.md)
- [📖 NVIDIA 爬虫文档](./NVIDIA_SCRAPER_README.md)
- [📖 ModelScope 爬虫文档](./MODELSCOPE_SCRAPER_README.md)
- [📖 OpenRouter 爬虫文档](./OPENROUTER_SCRAPER_README.md)

---

## API 兼容性

### 支持的 OpenAI 兼容平台

本项目支持**任何提供 OpenAI 兼容 API** 的平台，分为两类：

#### 🎯 有插件支持的平台（自动获取模型）

| 平台 | 状态 | 特点 |
|------|------|------|
| **ModelScope** | ✅ 已支持 | 国内访问快，模型丰富，自动抓取免费模型 |
| **OpenRouter** | ✅ 已支持 | 免费模型多，更新快，按价格过滤 |
| **NVIDIA NIM** | ✅ 已支持 | 高质量模型，企业级，自动抓取预览模型 |

#### 🔧 无插件支持的平台（需手动配置）

| 平台 | 状态 | 配置方式 |
|------|------|----------|
| **OpenAI** | ✅ 已支持 | 手动配置模型列表 |
| **Azure OpenAI** | ✅ 已支持 | 手动配置模型列表 |
| **Google Vertex AI** | ✅ 可用 | 手动配置模型列表（OpenAI 兼容模式） |
| **阿里云百炼** | ✅ 可用 | 手动配置模型列表 |
| **其他平台** | ✅ 可用 | 手动配置模型列表 |

> 💡 **提示：** 即使没有插件，只要平台提供 OpenAI 兼容的 `/v1/chat/completions` 端点，都可以正常使用。插件只是帮助自动获取和更新模型列表，不是必需的。

### 手动配置示例

对于没有插件的平台，在 `models.yaml` 中手动配置：

```yaml
my_custom_platform:
  baseUrl: "https://api.example.com/v1"
  apiKey: "${MY_API_KEY}"
  weight: 5
  timeout: 300
  enabled: true
  models:
    - id: "gpt-4"
      name: "GPT-4"
      contextWindow: 8192
      maxTokens: 4096
    - id: "gpt-3.5-turbo"
      name: "GPT-3.5 Turbo"
      contextWindow: 4096
      maxTokens: 2048
```

### 客户端兼容性

所有支持 OpenAI API 的客户端均可直接使用：

- ✅ OpenClaw
- ✅ LangChain
- ✅ AutoGen
- ✅ LiteLLM
- ✅ 任何 OpenAI SDK

---

## 部署选项

- ✅ Docker 容器化部署
- ✅ Docker Compose 编排
- ✅ Kubernetes YAML 配置
- ✅ ServiceMonitor for Prometheus
- ✅ 环境变量配置管理

详细文档：[🐳 部署指南](./DEPLOYMENT.md)

---

## 安全特性

- ✅ API 密钥环境变量管理
- ✅ 敏感信息日志脱敏
- ✅ HTTPS/TLS 支持
- ✅ 速率限制配置
- ✅ 访问日志记录

详细文档：[🚨 安全注意事项](./SECURITY.md)

---

## 相关文档

- [🔧 完整配置指南](./CONFIGURATION_GUIDE.md)
- [📊 监控与运维](./MONITORING.md)
- [⚡ 负载均衡策略](./LOAD_BALANCING.md)
- [🐳 部署指南](./DEPLOYMENT.md)
- [🚨 安全注意事项](./SECURITY.md)
