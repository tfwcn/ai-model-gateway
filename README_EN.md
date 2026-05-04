# 🤖 AI Free Model Proxy Service

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104+-green.svg)](https://fastapi.tiangolo.com/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Downloads](https://pepy.tech/badge/openai-proxy)](https://pepy.tech/project/openai-proxy)

> **Smart Multi-Platform Free AI Model Proxy** - Auto failover, load balancing, OpenAI-compatible API
> 
> **智能多平台免费 AI 模型代理服务** - 自动切换、负载均衡、OpenAI 兼容接口

🌐 Languages: [中文](README.md) | [English](README_EN.md)

**[Quick Start](#-quick-start-30-seconds)** · **[Configuration](#-configuration-guide)** · **[API Reference](#-api-reference)** · **[FAQ](docs/PLUGIN_FAQ.md)** · **[Contributing](#-contributing)**

---

## ✨ Why Choose This Project?

### 🎯 Core Value

Are you facing these issues?
- ❌ Free models often fail or run out of quota
- ❌ Need to manually maintain API keys and configs for multiple platforms
- ❌ Outdated model lists, can't get newly released free models in time
- ❌ Lack of monitoring, don't know which platform has problems

**AI Free Model Proxy Service** solves these problems for you:

- 🔄 **Smart Failover**: Automatically switch to backup platforms when one fails, no manual intervention needed
- ⚖️ **Weighted Load Balancing**: Distribute requests based on configured priorities, prefer high-quality platforms
- 🔌 **Plugin System**: Dynamically fetch latest free model lists from platform APIs, no manual maintenance
- 📊 **Prometheus Monitoring**: Built-in metrics collection, monitor request volume, latency, error rates in real-time
- 🚀 **Zero Client Changes**: Fully compatible with OpenAI API, existing clients work without modification
- 🛡️ **Smart Error Classification**: Automatically identify 7 error types, handle different failure scenarios precisely

### 📊 Comparison with Traditional Solutions

| Feature | This Project | Direct Platform API | Other Proxy Solutions |
|------|--------|------------------|--------------|
| Auto Failover | ✅ Smart switching | ❌ Manual handling required | ⚠️ Partial support |
| Multi-Platform Integration | ✅ 5+ platforms | ❌ Single platform | ⚠️ 2-3 platforms |
| Dynamic Model Discovery | ✅ Plugin system | ❌ Manual maintenance | ❌ Static configuration |
| Monitoring & Alerts | ✅ Prometheus | ❌ None | ⚠️ Basic logging |
| Error Classification | ✅ 7 types | ❌ Unified handling | ⚠️ Simple classification |
| Cache Support | ✅ Memory/Redis | ❌ None | ⚠️ Basic caching |

---

## 🚀 Quick Start (30 Seconds)

### 1️⃣ Installation

```bash
git clone https://github.com/tfwcn/ai-model-gateway.git
cd ai-model-gateway
pip install -r requirements.txt
```

### 2️⃣ Configuration

```bash
# Copy configuration files
cp .env.example .env
cp models.example.yaml models.yaml

# Edit .env and fill in your API keys
nano .env
```

### 3️⃣ Start Service

```bash
python run.py
```

Service runs on `http://localhost:8000` by default.

### 4️⃣ Test

```bash
# Get available model list
curl http://localhost:8000/models

# Send chat request (auto-select best model)
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "all",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

---

## 📊 Project Stats

- 🌐 **Supported Platforms**: ModelScope, OpenRouter, NVIDIA, OpenAI, Azure, and 5+ more
- 🤖 **Available Models**: 100+ free models (dynamically updated)
- 📈 **High Concurrency**: Based on FastAPI + aiohttp async architecture
- ⏱️ **Average Latency**: < 500ms (including failover)
- 🛡️ **Availability**: 99.9%+ (multi-platform redundancy)
- 🔧 **Error Classification**: 7 error types intelligently identified

---

## 🏗️ Architecture Design

```
┌─────────────┐
│   Client    │ (OpenClaw / Any OpenAI-compatible client)
└──────┬──────┘
       │
       ▼
┌─────────────────────────────────┐
│   AI Free Model Proxy Service   │
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
│Platform A│ │Platform B│ ... (Multiple platforms)
└──────────┘ └──────────┘
```

### Core Modules

- **Core**: Plugin management, configuration loading, cache abstraction layer
- **Model**: Model state management, failover, capability testing
- **Scraper**: Crawler system (ModelScope, NVIDIA, OpenRouter)
- **Adapter**: API adapter (Responses API compatible)
- **Utils**: Error classifier, Prometheus metrics, session storage

### Workflow

1. **Receive Request**: Client sends OpenAI-compatible request
2. **Model Selection**: Select best platform based on weight and availability
3. **Failover**: If failed, automatically switch to next available platform
4. **Return Result**: Return response to client
5. **Monitor & Log**: Record metrics and error information

---

## 🔧 Configuration Guide

### Environment Variables

Create `.env` file and fill in your API keys:

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

> ⚠️ **Security Note**: API keys should NEVER be written directly in `models.yaml`, must be managed through environment variables.

### Platform Configuration Example

Edit `models.yaml` configuration file:

```yaml
modelscope:
  baseUrl: "https://api-inference.modelscope.cn/v1"
  apiKey: "${MODELSCOPE_API_KEY}"  # Auto-read from environment variable
  weight: 10  # Higher weight = higher priority
  timeout: 300
  enabled: true
  quota_period: "daily"  # Quota refresh period
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
        max_price: 0  # Only fetch free models

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

### Plugin System Details

The project provides a powerful plugin system to dynamically fetch latest free model lists from platform APIs:

#### 🎯 NVIDIA Plugin

Automatically crawl free preview models from NVIDIA NIM API:

```yaml
nvidia:
  plugin:
    code: "plugin.nvidia"
    cache_timeout: 3600
    args:
      free_model_count: 10  # Get top 10 free models
```

**Supported Free Model Patterns:**
- `nvidia/` - NVIDIA official models
- `microsoft/phi` - Microsoft Phi series
- `google/gemma` - Google Gemma series
- `meta/llama-3.2` - Meta Llama 3.2 series

📖 [View NVIDIA Scraper Documentation](docs/NVIDIA_SCRAPER_README.md)

#### 🎯 ModelScope Plugin

Filter free models based on `SupportInference` field:

```yaml
modelscope:
  plugin:
    code: "plugin.modelscope"
    cache_timeout: 3600
    args:
      request_params:
        SupportInference: "txt2txt"  # Text generation models
```

📖 [View ModelScope Scraper Documentation](docs/MODELSCOPE_SCRAPER_README.md)

#### 🎯 OpenRouter Plugin

Filter free models by category and price:

```yaml
openrouter:
  plugin:
    code: "plugin.openrouter"
    cache_timeout: 300
    args:
      request_params:
        max_price: 0  # Only fetch free models
        categories: "programming"  # Programming models (optional)
```

📖 [View OpenRouter Scraper Documentation](docs/OPENROUTER_SCRAPER_README.md)

📖 [View Plugin Configuration FAQ](docs/PLUGIN_FAQ.md) - Frequently Asked Questions  
📖 [View Migration Guide](docs/MIGRATION_GUIDE.md) - Migrate from old configuration

---

## 📡 API Reference

### Endpoint List

| Endpoint | Method | Description |
|------|------|------|
| `/models` | GET | Get available model list |
| `/v1/chat/completions` | POST | Chat completion (OpenAI compatible) |
| `/health` | GET | Basic health check |
| `/health/detailed` | GET | Detailed health check (with component status) |
| `/metrics` | GET | Prometheus monitoring metrics |
| `/cache/clear` | POST | Clear all cache |
| `/cache` | DELETE | Delete specific request cache |

### Model Selection Strategy

- **`"all"`** - Select best model from all configured platforms (default)
- **`"modelscope"`** - Specify ModelScope platform
- **`"openrouter"`** - Specify OpenRouter platform
- **Auto Weight + Failover** - Intelligently select based on weight priority and availability

### Usage Examples

```bash
# Get available model list
curl http://localhost:8000/models

# Send chat request (auto-select best model)
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "all",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'

# Specify platform
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "modelscope",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### OpenClaw Configuration

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

## 📊 Monitoring & Operations

### Health Check

```bash
# Basic health check
curl http://localhost:8000/health

# Detailed health check (with component status)
curl http://localhost:8000/health/detailed
```

Response example:
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

### Prometheus Monitoring Metrics

```bash
# Get Prometheus format metrics
curl http://localhost:8000/metrics
```

**Main Metrics:**
- `proxy_requests_total` - Total requests (grouped by platform, model, status, error type)
- `proxy_request_duration_seconds` - Request latency histogram
- `proxy_errors_total` - Total errors (grouped by platform, error type)
- `platform_availability` - Platform availability status (1=available, 0=unavailable)
- `proxy_failover_total` - Failover count
- `cache_hits_total` / `cache_misses_total` - Cache hit/miss count
- `active_connections` - Active connections

### Cache Management

```bash
# Clear all cache
curl -X POST http://localhost:8000/cache/clear

# Delete specific request cache
curl -X DELETE http://localhost:8000/cache \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4", "messages": [{"role": "user", "content": "Hello"}]}'
```

---

## ⚡ Load Balancing Strategy

The service uses the following strategies for intelligent load balancing:

1. **Weight Priority**: Sort by configured `weight` value, higher weight platforms used first
2. **Failure Detection**: When model call fails, mark as unavailable for current period
3. **Auto Switching**: Try remaining available platforms in weight order
4. **Period Recovery**: Restore model availability after period ends based on `quota_period`
5. **Smart Error Classification**: 7 error types handled precisely, avoid unnecessary model disabling

📖 [View Error Classification Details](docs/error-classification.md)

---

## 🚨 Security Notes

- 🔑 **API Key Security**: All API keys must be managed through environment variables, **NEVER** hardcode in configuration files
- 🛡️ **.env File Protection**: Ensure `.env` file permissions are read-only for application (recommend `chmod 600 .env`)
- ✅ **Configuration Validation**: Ensure `baseUrl` is correct, API keys properly injected through environment variables
- ⚖️ **Weight Settings**: Set `weight` values reasonably to achieve desired load balancing效果
- ⏱️ **Timeout Configuration**: Adjust `timeout` based on platform response times, avoid unnecessary timeouts
- 📝 **Log Security**: Production environment should keep `DEBUG_LOGS=0` (default), avoid sensitive information leakage

---

## 🐳 Docker Deployment

The project provides complete Docker support, using Python 3.12 image for containerized deployment.

### Quick Start

```bash
# Build and run
docker build -t openai-proxy .
docker run -d \
  --name openai-proxy \
  -p 8000:8000 \
  --env-file .env \
  -v $(pwd)/models.yaml:/app/models.yaml:ro \
  openai-proxy
```

### Docker Compose (Recommended)

```bash
# Start service
docker-compose up -d

# View logs
docker-compose logs -f

# Stop service
docker-compose down
```

**Docker Deployment Notes:**
- 🔑 **Environment Variable Injection**: Inject keys via `--env-file .env` or docker-compose environment config
- 📄 **Configuration File Mounting**: `models.yaml` mounted as read-only to container for security
- 🌐 **Port Mapping**: Default map port 8000, modify as needed
- 🛡️ **Security**: Container runs as non-root user for improved security
- 🔄 **Auto Restart**: Configured with `unless-stopped` restart policy for high availability

---

## 🤝 Contributing

Welcome to submit Issues and Pull Requests! We especially welcome the following contributions:

### 🎯 Contribution Areas

1. **New Platform Integration**: Add more AI platform support (e.g., Google Vertex AI, Anthropic, etc.)
2. **Error Handling Optimization**: More robust failure recovery mechanisms and retry strategies
3. **Performance Optimization**: Improve concurrency handling and reduce latency
4. **Documentation Improvement**: Add usage examples, tutorials, and best practices
5. **Test Coverage**: Increase unit tests and integration tests
6. **UI Interface**: Web management interface development

### 🛠️ Development Environment Setup

```bash
# Clone project
git clone https://github.com/tfwcn/ai-model-gateway.git
cd ai-model-gateway

# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt  # Development dependencies

# Run tests
pytest tests/ -v

# Code formatting
black openai_proxy/
flake8 openai_proxy/
```

### 📝 Submission Process

1. **Fork** this project
2. Create feature branch (`git checkout -b feature/amazing-feature`)
3. Commit changes (`git commit -m 'Add some amazing feature'`)
4. Push to branch (`git push origin feature/amazing-feature`)
5. Create **Pull Request**

### 💻 Code Standards

- Follow [PEP 8](https://peps.python.org/pep-0008/) Python code style
- Use [Black](https://black.readthedocs.io/) for code formatting
- Use [Flake8](https://flake8.pycqa.org/) for code checking
- All public functions and classes must have docstrings
- Add type annotations to improve code readability

---

## 🗺️ Roadmap

### ✅ v1.0 (Completed)

- ✅ Multi-platform support (ModelScope, OpenRouter, NVIDIA, etc.)
- ✅ Smart failover and load balancing
- ✅ Plugin system for dynamic model fetching
- ✅ Prometheus monitoring metrics
- ✅ Smart error classification system
- ✅ Request caching (Memory/Redis)

### 🚧 v2.0 (Planned)

- 🔄 Web UI management interface
- 🔄 More platform integration (Google Vertex AI, Anthropic, etc.)
- 🔄 AI-driven model recommendation
- 🔄 Finer-grained rate limit control
- 🔄 Distributed deployment support

### 💡 Future Vision

- 🌟 Model performance analysis and auto-optimization
- 🌟 Multi-language SDK support
- 🌟 Enterprise features (SSO, audit logs, etc.)

---

## 📄 License

This project uses [MIT License](LICENSE) open source protocol.

---

## 🙏 Acknowledgments

Thanks to the following excellent open source projects:

- **[FastAPI](https://fastapi.tiangolo.com/)** - High-performance async web framework
- **[OpenClaw](https://github.com/openclaw/openclaw)** - AI agent framework
- **[ModelScope](https://modelscope.cn/)** - Alibaba ModelScope Community
- **[OpenRouter](https://openrouter.ai/)** - Unified AI model API
- **[NVIDIA NIM](https://www.nvidia.com/en-us/ai-data-science/nim/)** - NVIDIA AI inference microservices

---

## 📞 Contact

- 📧 Email: [your-email@example.com](mailto:your-email@example.com)
- 💬 GitHub Issues: [Submit Issue](https://github.com/tfwcn/ai-model-gateway/issues)
- 📖 Documentation: [Full Documentation](docs/)

---

<div align="center">

**⭐ If this project helps you, please give it a Star!**

[![Star History Chart](https://api.star-history.com/svg?repos=tfwcn/ai-model-gateway&type=Date)](https://star-history.com/#tfwcn/ai-model-gateway&Date)

</div>

---

**Disclaimer**: This service is only for legal and compliant personal learning and research purposes. Please comply with the terms of use and free quota limits of each AI platform.
