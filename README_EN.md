# 🤖 AI Model Gateway

![AI Model Gateway](docs/img.png)

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104+-green.svg)](https://fastapi.tiangolo.com/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

> **Smart Multi-Platform Free AI Model Gateway** — Auto failover, load balancing, protocol conversion, OpenAI-compatible API
>
> **智能多平台免费 AI 模型网关服务** — 自动切换、负载均衡、协议转换、OpenAI 兼容接口

💡 **Positioning:** Supports **any platform providing OpenAI-compatible APIs**. Built-in plugins can automatically scrape free models from ModelScope, NVIDIA, and OpenRouter; other platforms require manual configuration.

🌐 Languages: [中文](README.md) | [English](README_EN.md)

**[Quick Start](#-quick-start)** · **[Admin UI](#-admin-ui)** · **[API Reference](#-api-reference)** · **[Configuration Guide](docs/CONFIGURATION_GUIDE.md)** · **[Monitoring](docs/MONITORING.md)** · **[Observability & Administration](#-observability--administration)** · **[Contributing](#-contributing)**

---

## 🖥️ Admin UI

Once the service is running, open the admin UI first: platform weights, model timeouts, the blacklist and user keys can all be edited in the browser — **no hand-written `models.yaml` required**.

```text
http://localhost:8000/admin/ui/     # open it, then enter GATEWAY_API_KEY to connect
```

Five tabs: usage & performance, audit details, user keys, model config, backups & rollback. Pure static pages (no build step,
no external dependencies). The key lives only in browser `sessionStorage`, and **key values are never sent to the browser**:
the config view only shows which environment variable a key references and whether it is set.

| What you want to do | Where to click in the admin UI |
| --- | --- |
| Change platform weight, model timeout, blacklist | Edit directly on the *model config* tab; every save is backed up first and can be rolled back |
| Apply the change | Click reload after saving — it applies in-process. Adding/removing a platform still needs your own restart |
| Issue a key per user | The *user keys* tab generates one from a label; traffic is accounted per user |
| See who used how much, and where it is slow | *Usage & performance* aggregates by user / model / time |

> Prefer the terminal? `manage.py` offers the same capabilities — see [🖥️ Admin UI](docs/ADMIN_UI.md) and [⌨️ CLI Tool](docs/CONFIG_CLI.md).

---

## ✨ Why Choose This Project?

### 🎯 Core Value

Are you facing these issues?
- ❌ Free models often fail or run out of quota
- ❌ Need to manually maintain API keys and configs for multiple platforms
- ❌ Outdated model lists, can't get newly released free models in time
- ❌ Non-standard tool call formats (NVIDIA JSON, Minimax XML) break compatibility
- ❌ Lack of monitoring, don't know which platform has problems

**AI Model Gateway** solves these problems for you:

- 🔄 **Smart Failover**: Automatically switch to backup platforms when one fails
- ⚖️ **Weighted Load Balancing**: Distribute requests based on configured priorities
- 🔌 **Plugin Scheduler System**: Scheduled web scraping (Playwright) for latest free models
- 🛡️ **Smart Error Classification**: 7 error types with precise retry/disable strategies
- 🔧 **Tool Call Converter**: Auto-convert non-standard formats (NVIDIA JSON, Minimax XML) to OpenAI standard
- 📡 **OpenAI Responses API**: Full `/v1/responses` endpoint with protocol conversion
- 🔑 **Admin + user-level keys**: `GATEWAY_API_KEY` for the admin console; per-user keys for traffic accounting
- 📊 **Prometheus Monitoring**: Real-time metrics for requests, latency, errors
- 🚀 **Zero Client Changes**: Fully compatible with OpenAI API

## 🆕 Core Features

| Feature                        | Description                                                                      |
| ------------------------------ | -------------------------------------------------------------------------------- |
| **Responses API**              | Full `/v1/responses` support, bidirectional Chat ↔ Responses protocol conversion |
| **Tool Call Converter**        | Auto-detect NVIDIA JSON / Minimax XML and convert to standard `tool_calls`       |
| **Streaming Tool Call Buffer** | Intelligent buffer for streaming non-standard tool calls                         |
| **Tool Capability Testing**    | Auto-test tool call support on model discovery, keep only supported models       |
| **Model Retry Mechanism**      | Configurable `retry_count`, intelligent retry based on error type                |
| **Model Alias & Blacklist**    | `all_aliases` for multiple aliases, `blacklist` with glob pattern matching       |
| **GATEWAY_API_KEY Auth**       | Admin key protects `/admin/*` and `/metrics`; user keys are managed in the UI |
| **Redis Cache & Sessions**     | Response caching + Responses API session history, auto-fallback to file storage  |
| **SSE Normalization**          | Unified SSE event format, eliminating duplicate code across endpoints            |
| **Error Classification**       | 7 error types (timeout/connection/auth/rate-limit/server/format/model)           |
| **SKIP_PLUGIN_SCRAPER**        | Skip plugin scraper and use cached models, ideal for offline environments        |
| **Usage & Performance Audit**  | One row per upstream attempt: success rate, P95, TTFT, tokens/s, failover chain  |
| **Generic Passthrough**        | `/proxy/<route>/**` forwards any HTTP API as-is; user keys and audit are reused   |
| **Admin UI & CLI**             | `/admin/ui/` web page and `manage.py` both read audits and edit model config     |
| **Docker One-click Deploy**    | Built-in Redis, Playwright, ready-to-run containerized deployment                |

### 📊 Comparison with Traditional Solutions

| Feature              | This Project                 | Direct Platform API  | Other Proxy Solutions |
| -------------------- | ---------------------------- | -------------------- | --------------------- |
| Auto Failover        | ✅ Smart switching            | ❌ Manual handling    | ⚠️ Partial support     |
| Multi-Platform       | ✅ 5+ platforms               | ❌ Single platform    | ⚠️ 2-3 platforms       |
| Dynamic Models       | ✅ Plugin scheduler + scraper | ❌ Manual maintenance | ❌ Static config       |
| OpenAI Responses API | ✅ Full support               | ❌ Chat only          | ❌ Not supported       |
| Tool Call Conversion | ✅ NVIDIA/Minimax/etc         | ❌ Manual handling    | ❌ Not supported       |
| Monitoring           | ✅ Prometheus                 | ❌ None               | ⚠️ Basic logging       |
| Error Classification | ✅ 7 types                    | ❌ Unified handling   | ⚠️ Simple              |
| Cache                | ✅ Memory/Redis               | ❌ None               | ⚠️ Basic               |

---

## 🚀 Quick Start

### 1️⃣ Installation

```bash
git clone https://github.com/tfwcn/ai-model-gateway.git
cd ai-model-gateway
pip install -r requirements.txt
playwright install --with-deps chromium
```

### 2️⃣ Configuration

```bash
cp .env.example .env
cp models.example.yaml models.yaml
nano .env  # Fill in your API keys (optionally set GATEWAY_API_KEY)
```

> Don't want to write config by hand? Start with the default `models.yaml`, then add platforms and tune weights
> and timeouts in the [🖥️ Admin UI](#-admin-ui).

### 3️⃣ Start

Run directly (default port 8000):

```bash
python run.py
```

Or use the startup script (loads `.env`, auto-manages Redis and virtual env):

```bash
bash start.sh               # loads .env; default port 8000 when PORT is unset
bash start.sh --port 8000   # CLI args override .env
ENV_FILE=/etc/gateway.env bash start.sh  # use a custom env file
```

You can also set the port via environment variable:

```bash
PORT=8000 python run.py
PORT=8000 bash start.sh
```

### 4️⃣ Test

```bash
# List available models
curl http://localhost:8000/v1/models

# Chat completion
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "all",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'

# Responses API
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{
    "model": "all",
    "input": "Hello!"
  }'
```

---

## 📡 API Reference

### Endpoints

| Endpoint               | Method | Description                                           |
| ---------------------- | ------ | ----------------------------------------------------- |
| `/v1/chat/completions` | POST   | OpenAI Chat Completions (auth required)               |
| `/v1/responses`        | POST   | OpenAI Responses API (auth required)                  |
| `/v1/models`           | GET    | List models with filtering/pagination (auth required) |
| `/health`              | GET/POST | Basic health check (no auth)                        |
| `/health/detailed`     | GET    | Detailed health check                                 |
| `/metrics`             | POST/GET | Prometheus metrics (admin key; GET for scraping)    |
| `/admin/usage/*`       | POST   | Usage & performance aggregates (admin key required)   |
| `/admin/audit*`        | POST   | Audit details and failover chains (admin key required)|
| `/proxy/<route>/**`    | ANY    | Generic HTTP passthrough (gateway key required)       |
| `/admin/config/*`      | POST   | Config read, dry-run plan, apply, rollback            |
| `/admin/users*`        | POST   | Create/list/delete user keys (admin key required)     |
| `/admin/ui/`           | GET    | Admin UI shell (no key needed; its data calls do)     |

### Model Selection

Three selector formats:
- **`"all"`** — All platforms, sorted by weight (default)
- **`"modelscope"`** — Specific platform
- **`"modelscope|Qwen/Qwen2.5-7B-Instruct"`** — Specific model on specific platform

`/v1/models` supports query params: `provider`, `capability`, `q`, `limit`, `offset`.

---

## 🏗️ Architecture

```
┌─────────────┐
│   Client    │ (Any OpenAI-compatible client)
└──────┬──────┘
       │
       ▼
┌──────────────────────────────────────────┐
│          AI Model Gateway                │
│                                          │
│  ┌────────────────────────────────────┐  │
│  │         FastAPI Routes             │  │
│  │  /v1/chat/completions              │  │
│  │  /v1/responses (protocol convert)  │  │
│  │  /v1/models (filtered/paginated)   │  │
│  │  /health /metrics                  │  │
│  └──────────┬─────────────────────────┘  │
│             │                             │
│  ┌──────────▼─────────────────────────┐  │
│  │       Failover Manager             │  │
│  │  ┌──────────┐  ┌────────────────┐  │  │
│  │  │ Weight   │  │ Failover/Retry │  │  │
│  │  │ Sort     │  │                │  │  │
│  │  └──────────┘  └────────────────┘  │  │
│  │  ┌──────────┐  ┌────────────────┐  │  │
│  │  │ Error    │  │ Model State    │  │  │
│  │  │ Classifier│  │ Manager       │  │  │
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
│  │  Chat ↔ Responses protocol convert │  │
│  │  Streaming event + session mgmt    │  │
│  └────────────────────────────────────┘  │
└──────┬──────────┬───────────────────────┘
       │          │
       ▼          ▼
┌──────────┐ ┌──────────┐
│Platform A│ │Platform B│ ... (Multiple platforms)
└──────────┘ └──────────┘
```

### Core Modules

- **Core**: Config loader, plugin manager, cache abstraction (MemoryCache / RedisCache)
- **Model**: Failover manager, error classifier (7 types), model state manager, tool capability tester
- **Scraper**: Playwright web scraper base + APScheduler scheduled tasks
- **Adapter**: Responses API ↔ Chat API protocol converter
- **Utils**: SSE event parser, tool call converter, streaming tool call buffer, Prometheus metrics, session store (Redis/File dual-mode)

---

## 🔧 Configuration

### Environment Variables

```env
MODELSCOPE_API_KEY=your-modelscope-api-key
OPENROUTER_API_KEY=your-openrouter-api-key
NVIDIA_API_KEY=your-nvidia-api-key
OPENAI_API_KEY=your-openai-api-key
GATEWAY_API_KEY=your-gateway-api-key   # admin console key; empty disables admin endpoints
# API_KEYS_DB_PATH=data/api_keys.db    # user-level keys (add/remove in the UI)
# API_KEYS_ENCRYPTION_KEY=your-random-encryption-secret  # enables key retrieval
REDIS_URL=redis://localhost:6379       # Optional Redis

# Skip plugin scraper, use cached models directly (optional)
# SKIP_PLUGIN_SCRAPER=true

# Logging configuration (optional)
# DEBUG=true                           # Enable DEBUG level logs (default INFO)
# ENABLE_CONSOLE_LOGS=false            # Disable console output (default true)
# LOG_DIR=logs                         # Log directory (default logs/)

# Usage & performance audit (one row per upstream attempt, stored in SQLite)
# Priority: built-in defaults < AUDIT_* here < top-level `audit:` section in models.yaml
# AUDIT_ENABLED=true                   # Master switch; when off the forwarding hot path costs nothing
# AUDIT_DB_PATH=data/audit.db          # Audit database path
# AUDIT_RETENTION_DAYS=30              # Older records are pruned at startup, 0 disables pruning
# AUDIT_RECORD_FAILED_ONLY=false       # Log failures only (saves disk, loses usage stats)
# AUDIT_PROMETHEUS=true                # Feed Prometheus while writing audit rows

# Admin endpoints and admin UI (/admin/* and /metrics both require GATEWAY_API_KEY)
# ADMIN_UI_ENABLED=true                # Serve /admin/ui, false returns 404
# CONFIG_API_ENABLED=true              # Expose /admin/config/*, false does not mount them
# CONFIG_BACKUP_DIR=data/config_backups   # Automatic backup directory used before every write
# MODEL_CONFIG_FILE=models.yaml        # Config file the CLI reads by default
```

### Platform Config Example

```yaml
modelscope:
  baseUrl: "https://api-inference.modelscope.cn/v1"
  apiKey: "${MODELSCOPE_API_KEY}"
  weight: 10
  timeout: 30
  quota_period: "daily"
  retry_count: 3
  plugin:
    code: "plugin.modelscope"
    cache_timeout: 3600       # Model list cache TTL (seconds)
    args:
      scrape_url: "https://www.modelscope.cn/models?filter=inference_type&page=1&sort=default&tabKey=task"
      max_models: 10
      scraper_timeout: 60
      headless: true          # Run browser in headless mode
      enable_tool_capability_test: true
      enable_scheduled_task: true
      schedule_cron: "0 2 * * *"   # Run daily at 2 AM
  blacklist:                  # Blacklist (supports * glob pattern)
    - "iic/*"
  models: []                  # Static model list (optional)
```

See [📖 Configuration Guide](docs/CONFIGURATION_GUIDE.md) for details.

---

## 📊 Observability & Administration

Every upstream attempt is recorded as one audit row (default database: `data/audit.db`), which answers
"who was used how much, where is it slow, why did it fail".

Accounting rules: **one client request = one `gateway_request_id`, one upstream attempt = one row**, so a full
failover chain can be reconstructed. Success rate only counts finalized attempts (`finalized=1`); a stream that is
still being forwarded is recorded as `stream_open` and stays out of the denominator.

Retention is split: **audit details are kept for 30 days** by default (for drill-down and troubleshooting), while
**all-time daily usage is kept forever** (`usage_daily`, aggregated by day + user with input / cached / output tokens).
Clearing details therefore never deletes historical usage. The daily endpoint is `POST /admin/usage/daily`.
The admin UI entry point, its five tabs and key handling are described above in [🖥️ Admin UI](#-admin-ui);
`/admin/*` and `/metrics` both require `GATEWAY_API_KEY`.

### Command line

```bash
# Usage and performance
python manage.py usage summary --group-by model --since 7d
python manage.py usage daily --since 90d              # all-time daily tokens, by user
python manage.py usage models --sort p95              # slowest models
python manage.py usage attempts --outcome failure     # failed attempts only
python manage.py usage chain <gateway_request_id>     # reconstruct the failover chain

# Read and edit config (always backed up before writing; --dry-run shows the diff first)
python manage.py config document
python manage.py platform set example-platform --field weight --value 5 --dry-run
python manage.py model set example-platform model-a --field first_chunk_timeout --value 45
python manage.py config backups
python manage.py config rollback models.yaml.20260101T000000Z.bak
```

There are three equivalent ways to change config: the admin UI, `manage.py`, or editing `models.yaml` directly.
After the last two, call `POST /admin/config/reload` (with `GATEWAY_API_KEY`) to apply changes in-process.
Changes that add/remove a platform or alter `plugin` structure **require you to restart the service yourself** —
the gateway never restarts itself.

See [🧾 Audit & Usage Stats](docs/AUDIT.md), [🖥️ Admin UI](docs/ADMIN_UI.md), [⌨️ CLI Tool](docs/CONFIG_CLI.md).

---

## 📖 Documentation

- [🔧 Configuration Guide](docs/CONFIGURATION_GUIDE.md)
- [🐳 Docker Deployment](docs/DEPLOYMENT.md)
- [🚨 Security](docs/SECURITY.md)
- [📋 Roadmap](docs/ROADMAP.md)
- [🧾 Audit & Usage Stats](docs/AUDIT.md)
- [🔀 Passthrough & Dimension Usage](docs/PASSTHROUGH.md)
- [🖥️ Admin UI](docs/ADMIN_UI.md)
- [⌨️ CLI Tool](docs/CONFIG_CLI.md)
- [📡 Admin API Contract](web/API.md)
- [📊 Monitoring](docs/MONITORING.md)
- [⚡ Load Balancing](docs/LOAD_BALANCING.md)
- [🛡️ Error Classification](docs/error-classification.md)
- [🧪 Error Classification Testing](docs/error-classification-testing.md)
- [🔧 Tool Call Converter](docs/TOOL_CALL_CONVERTER.md)
- [📡 Responses API Protocol](docs/protocol_summary.md)
- [🔌 Plugin FAQ](docs/PLUGIN_FAQ.md)

### Migration Guide

- [🔄 Version Migration Guide](docs/MIGRATION_GUIDE.md) — Step-by-step upgrade instructions

---

## 🤝 Contributing

```bash
git clone https://github.com/tfwcn/ai-model-gateway.git
cd ai-model-gateway
pip install -r requirements.txt
pip install -r requirements-dev.txt
pytest tests/ -v --tb=short
black openai_proxy/
mypy openai_proxy/
```

1. Fork the repo
2. Create feature branch (`git checkout -b feature/amazing-feature`)
3. Commit changes (`git commit -m 'Add amazing feature'`)
4. Push (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## 📄 License

MIT License — see [LICENSE](LICENSE)

---

## 🙏 Acknowledgments

- [FastAPI](https://fastapi.tiangolo.com/)
- [aiohttp](https://docs.aiohttp.org/)
- [Playwright](https://playwright.dev/)
- [Prometheus](https://prometheus.io/)
- [APScheduler](https://apscheduler.readthedocs.io/)
- [pydantic](https://docs.pydantic.dev/)
- [Redis](https://redis.io/)

---

<div align="center">

**⭐ If this project helps you, please give it a Star!**

</div>