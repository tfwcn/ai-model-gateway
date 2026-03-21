# AI 免费模型代理服务

一个智能的多平台免费模型代理服务，支持自动切换不同平台的免费 AI 模型并实现负载均衡，提供统一的 OpenAI 兼容接口。
解决Openclaw模型调用失败或额度不足问题，增加模型调用稳定性。

## 📌 项目概述

AI 免费模型代理服务是一个智能代理层，专门用于管理和调度多个平台的免费 AI 模型资源。主要解决以下问题：

- **多平台整合**：统一接入不同平台（如 ModelScope、OpenRouter、OpenAI、Azure 等）的免费模型
- **自动切换**：当某个平台的模型调用失败时，自动切换到其他可用平台
- **负载均衡**：基于配置的权重在多个可用平台之间分配请求，优先使用高权重平台
- **标准化接口**：提供统一的 OpenAI 兼容 API，客户端无需关心底层实现细节

## ✨ 功能特性

- **多平台支持**：支持配置多个 AI 平台的模型服务
- **智能切换**：自动检测模型调用失败，实现无缝故障转移
- **权重调度**：通过 `weight` 参数配置平台优先级，实现负载均衡
- **OpenAI 兼容**：完全兼容 OpenAI API 接口，客户端零改造
- **动态配置**：通过 `models.yaml` 文件灵活配置支持的模型和平台
- **高性能异步**：基于 FastAPI 和 aiohttp 实现高并发处理
- **额度周期管理**：支持配置额度刷新周期（daily/weekly/monthly/hourly）
- **安全密钥管理**：支持通过 `.env` 文件或环境变量管理API密钥，避免敏感信息泄露
- **插件扩展**：支持插件系统动态获取模型列表，无需手动维护模型配置

## 🛠 技术栈

- **FastAPI** >=0.104.0：构建异步 API 服务
- **Uvicorn** >=0.24.0：ASGI 服务器
- **aiohttp** >=3.8.0：异步 HTTP 客户端用于多平台请求
- **pydantic** >=2.0.0：数据校验与设置管理
- **PyYAML** >=6.0：解析 models.yaml 配置文件
- **python-dotenv** >=1.0.0：加载 .env 环境变量文件

**Python 版本要求**：Python 3.9 或更高版本

## 🚀 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/tfwcn/openclaw-free-openai-proxy.git
cd openclaw-free-openai-proxy
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置环境变量和模型平台

**步骤1：创建环境变量文件**

```bash
cp .env.example .env
```

**步骤2：编辑 `.env` 文件**

```bash
nano .env
```

填入你的真实API密钥：

```env
MODELSCOPE_API_KEY=your-real-modelscope-api-key
OPENROUTER_API_KEY=your-real-openrouter-api-key
OPENAI_API_KEY=your-real-openai-api-key
AZURE_API_KEY=your-real-azure-api-key
```

**步骤3：配置模型平台**
复制示例配置文件：

```bash
cp models.example.yaml models.yaml
```

编辑 `models.yaml` 文件来配置你的平台和模型（**注意：apiKey字段使用环境变量占位符**）：

```yaml
modelscope:
  baseUrl: "https://api-inference.modelscope.cn/v1"
  apiKey: "${MODELSCOPE_API_KEY}" # 自动从环境变量读取
  models:
    - "Qwen/Qwen3.5-397B-A17B"
    - "ZhipuAI/GLM-5"
  timeout: 300
  weight: 10 # 权重越高，优先级越高
  enabled: true
  quota_period: "daily" # 额度刷新周期

openrouter:
  baseUrl: "https://openrouter.ai/api/v1"
  apiKey: "${OPENROUTER_API_KEY}" # 自动从环境变量读取
  models:
    - "healer-alpha"
    - "nvidia/nemotron-3-super-120b-a12b:free"
    - "qwen/qwen3-next-80b-a3b-instruct:free"
  timeout: 300
  weight: 5 # 权重较低，作为备选
  enabled: true
  quota_period: "daily"
```

### 4. 使用插件系统动态获取模型（推荐）

项目提供了强大的插件系统，可以动态从各平台API获取最新的免费模型列表，无需手动维护模型配置。

#### OpenRouter 插件使用

OpenRouter 插件可以从 OpenRouter API 动态获取免费模型列表，支持参数过滤和智能缓存。

**基本配置示例：**

```yaml
openrouter:
  baseUrl: "https://openrouter.ai/api/v1"
  apiKey: "${OPENROUTER_API_KEY}"
  plugin:
    code: "plugin.openrouter"
    args:
      category: "free"           # 模型类别：free(默认), programming, coding, coder
      input_modalities: ["text"] # 输入模态：text, image, 或 ["text", "image"]
      cache_timeout: 300         # 缓存过期时间（秒），默认300秒（5分钟）
  timeout: 300
  weight: 5
  enabled: true
  quota_period: "daily"
```

**插件参数说明：**

- **category 参数**：
  - `"free"`: 获取所有免费模型（默认行为）
  - `"programming"`, `"coding"`, `"coder"`: 获取编程相关的免费模型
  - 其他类别: 直接传递给 OpenRouter API 的 categories 参数

- **input_modalities 参数**：
  - `["text"]`: 仅文本输入模型
  - `["image"]`: 仅图像输入模型  
  - `["text", "image"]`: 支持文本和图像输入的模型
  - 省略此参数: 获取所有输入模态的模型

- **cache_timeout 参数**：
  - 正整数: 缓存过期时间（秒），例如 300 = 5分钟
  - 0: 禁用缓存，每次都会调用 OpenRouter API 获取最新数据
  - 默认值: 300秒（5分钟）

**混合配置模式：**

插件可以与静态模型配置共存，插件返回的模型会优先于静态配置的模型：

```yaml
openrouter:
  plugin:
    code: "plugin.openrouter"
    args:
      category: "programming"
      cache_timeout: 300
  models:  # 这些静态模型会放在插件返回的模型后面
    - my-special-model
    - backup-model
  timeout: 300
  weight: 5
  enabled: true
```

### 5. 启动服务

使用 Python 直接启动：

```bash
python run.py
```

或者使用启动脚本（包含虚拟环境管理）：

```bash
./start.sh
```

服务默认运行在 `http://localhost:8000`

### 6. OpenClaw 配置

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

## 🔧 配置说明

### models.yaml 配置格式

配置文件采用分层结构，每个平台作为一个顶级键：

```yaml
platform_name:
  baseUrl: "API基础URL"
  apiKey: "${PLATFORM_NAME_API_KEY}" # 环境变量占位符，自动替换
  models:
    - "model_name_1"
    - "model_name_2"
  timeout: 30 # 请求超时时间（秒）
  weight: 1 # 权重（数值越大优先级越高）
  enabled: true # 是否启用该平台配置
  quota_period: "daily" # 额度刷新周期（可选）
```

#### 支持的配置字段：

- `baseUrl`: 平台 API 的基础 URL
- `apiKey`: **必须使用 `${PLATFORM_NAME_API_KEY}` 格式的环境变量占位符**
- `models`: 该平台支持的模型列表
- `timeout`: 请求超时时间（秒）
- `weight`: 权重值，用于负载均衡（数值越大优先级越高）
- `enabled`: 是否启用该平台配置
- `quota_period`: 额度刷新周期，支持 `daily`、`weekly`、`monthly`、`hourly`
- `plugin`: 插件配置（可选），用于动态获取模型列表

> **重要安全说明**：
>
> - API 密钥**绝不应该**直接写在 `models.yaml` 文件中
> - 所有敏感信息都应该通过 `.env` 文件或环境变量注入

### 环境变量加载机制

服务启动时会自动：

1. 加载 `.env` 文件中的环境变量（如果存在）
2. 读取系统环境变量
3. 在解析 `models.yaml` 时，自动将 `${PLATFORM_NAME_API_KEY}` 替换为对应的环境变量值

### 额度周期管理

`quota_period` 字段用于标记模型在特定周期内的可用性：

- 当模型调用失败时，会被标记为当前周期内不可用
- 周期结束后（如每日午夜），模型会自动恢复可用状态
- 如果未配置 `quota_period`，模型不会被标记为不可用

## 📡 API 使用

服务完全兼容 OpenAI API，你可以像直接调用 OpenAI 一样使用它：

```bash
# 获取可用的模型组列表
curl http://localhost:8000/models

# 聊天完成（自动选择最佳可用模型）
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "modelscope",  # 指定平台名称
    "messages": [{"role": "user", "content": "Hello!"}]
  }'

# 或者让服务自动选择所有平台中的最佳模型
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "all",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

> **模型选择机制**：
>
> - 指定具体平台名称（如 `"modelscope"`）：只在该平台的模型中选择
> - 使用 `"all"`：在所有配置的平台中选择最佳模型
> - 不指定 `model` 参数：等同于 `"all"`

## ⚡ 负载均衡策略

服务采用以下策略实现智能负载均衡：

1. **权重优先**：根据配置的 `weight` 值排序，权重高的平台优先使用
2. **故障检测**：当模型调用失败时，自动标记为当前周期内不可用
3. **自动切换**：在剩余可用的平台中按权重顺序尝试
4. **周期恢复**：根据 `quota_period` 配置，在周期结束后恢复模型可用性

## 🚨 安全注意事项

- **API 密钥安全**：所有API密钥必须通过环境变量管理，**绝不能**硬编码在配置文件中
- **.env文件保护**：确保 `.env` 文件权限设置为仅应用可读
- **配置验证**：确保 `baseUrl` 配置正确，API密钥通过环境变量正确注入
- **权重设置**：合理设置 `weight` 值以实现期望的负载均衡效果
- **超时配置**：根据平台响应时间调整 `timeout` 值，避免不必要的超时

## 📦 部署建议

### 生产环境部署

- 确保 `.env` 文件权限安全（chmod 600 .env）
- 配置适当的日志级别以便监控模型调用状态
- 定期检查各平台的免费政策变化，及时更新配置
- 考虑使用更安全的密钥管理服务（如HashiCorp Vault、AWS Secrets Manager等）

### 多实例部署

- 可以部署多个实例实现高可用
- 使用负载均衡器分发请求到不同实例
- 共享相同的配置文件，但每个实例使用独立的环境变量

### Docker 部署

项目提供了完整的 Docker 支持，使用 Python 3.12 镜像进行容器化部署。

#### 1. 构建并运行 Docker 容器

```bash
# 构建镜像
docker build -t openai-proxy .

# 运行容器（通过环境变量注入密钥）
docker run -d \
  --name openai-proxy \
  -p 8000:8000 \
  --env-file .env \
  -v $(pwd)/models.yaml:/app/models.yaml:ro \
  openai-proxy
```

#### 2. 使用 Docker Compose（推荐）

启动服务：

```bash
# 启动服务
docker-compose up -d

# 查看日志
docker-compose logs -f

# 停止服务
docker-compose down
```

#### Docker 部署注意事项

- **环境变量注入**：通过 `--env-file .env` 或 docker-compose 的 environment 配置注入密钥
- **配置文件挂载**：`models.yaml` 文件以只读方式挂载到容器中，确保配置安全
- **端口映射**：默认映射 8000 端口，可根据需要修改
- **安全性**：容器以非 root 用户运行，提高安全性
- **自动重启**：配置了 `unless-stopped` 重启策略，确保服务高可用

## 🤝 贡献指南

欢迎提交 Issue 和 Pull Request！特别欢迎：

1. 新平台的集成支持
2. 更完善的错误处理和恢复机制
3. 性能优化和稳定性改进
4. 文档完善和使用示例

## 📄 许可证

[MIT License](LICENSE)

---

**免责声明**：本服务仅用于合法合规的个人学习和研究目的。请遵守各 AI 平台的使用条款和免费额度限制。