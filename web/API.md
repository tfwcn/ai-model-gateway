# 管理接口契约（前端 / 命令行共用的数据形状）

这份文档是 `web/js/*` 依赖的字段清单。**改动 `/admin/*` 接口的返回字段时必须同步这里**，
`tests/test_admin_ui_routes.py` 会检查前端用到的字段是否仍然存在、文档是否覆盖了全部端点。

约定：
- **非转发接口统一用 POST**：所有 `/admin/*` 接口（包括查询）都使用 POST；`/metrics` 同时保留 GET 供 Prometheus 抓取。
  转发协议接口 `/v1/*` 不在本文范围内，方法和错误形状保持原样。
- 管理接口一律要求 `Authorization: Bearer <GATEWAY_API_KEY>`（只有静态壳 `/admin/ui` 例外：它不含任何数据，可以无密钥加载）。
- 网关未配置 `GATEWAY_API_KEY` 时管理接口返回 `403 {"error": {"code": "admin_disabled"}}`；
  密钥缺失或错误返回 `401`。
- 非转发接口的错误统一为 `{"error": {"code": "...", "message": "...", "details": {...}?}}`；
  `/v1/*` 保持原有 `{"detail": ...}` / OpenAI 兼容错误形状。
- 时间参数统一支持三种写法：`2026-01-01T10:00:00+00:00`、`2026-01-01`、相对时间 `7d` / `24h` / `30m`。
- 密钥永不下发：配置视图只给出它引用的环境变量名与「是否已设置」。
- 用户级密钥在 SQLite 里加密保存，支持按用户名取回；明文不再只能看一次。
- 示例一律使用占位符：`example-platform` / `https://api.example.com/v1` / `${EXAMPLE_PLATFORM_API_KEY}` / `model-a`。

## 数据面用户标识

`/v1/chat/completions`、`/v1/responses` 与透传接口 `/proxy/*` 支持两种指定审计用户的方式：

```http
# 方式一：用户密钥
Authorization: Bearer <用户密钥>

# 方式二：管理密钥 + 用户名 header
Authorization: Bearer <GATEWAY_API_KEY>
X-Gateway-User: user-a
```

- 用户密钥：审计记该密钥绑定的用户名，`X-Gateway-User` 会被忽略；
- 管理密钥 + `X-Gateway-User`：用户存在则直接记账；不存在则自动创建用户和密钥；
- 自动创建失败时直接返回错误，不请求上游；
- `admin` / `anonymous` 是保留名，不能作为用户名；
- 未配置鉴权时忽略该 header，审计记 `admin`。

## 目录

| 端点 | 方法 | 用途 |
| --- | --- | --- |
| `/admin/usage/summary` | POST | 按维度聚合用量与性能 |
| `/admin/usage/daily` | POST | 全量日用量（日期 × 用户，永久保留） |
| `/admin/usage/timeseries` | POST | 按小时/天分桶的曲线 |
| `/admin/usage/models` | POST | 模型横向对比榜 |
| `/admin/usage/dimensions` | POST | 永久维度日汇总（按任意维度组合） |
| `/admin/usage/dimension-keys` | POST | 维度候选（配置 + 历史） |
| `/admin/audit` | POST | 审计明细下钻 |
| `/admin/audit/prune` | POST | 删除指定时间之前的审计记录（body: `{"before": "..."}`） |
| `/admin/audit/chain/{gateway_request_id}` | POST | 一次请求的完整故障转移链 |
| `/admin/audit/stats` | POST | 审计自身健康状态 |
| `/admin/users/list` | POST | 用户级密钥清单（只含前缀） |
| `/admin/users` | POST | 新增用户并生成一次性密钥 |
| `/admin/users/key` | POST | 按用户名取回密钥明文 |
| `/admin/users/{user_id}/delete` | POST | 删除用户，密钥立即失效 |
| `/admin/config/document` | POST | 脱敏的配置视图 |
| `/admin/config/raw` | POST | 原始 YAML 文本（只读预览） |
| `/admin/config/schema` | POST | 字段白名单、取值边界与字段字典 |
| `/admin/config/env-status` | POST | 配置引用的环境变量是否已设置 |
| `/admin/config/models` | POST | 进程内当前可用的模型清单（下拉候选） |
| `/admin/config/validate` | POST | 校验当前配置文件 |
| `/admin/config/backups` | POST | 可回滚的备份列表 |
| `/admin/config/plan` | POST | 预演补丁（不写盘） |
| `/admin/config/apply` | POST | 应用补丁并写盘 |
| `/admin/config/rollback` | POST | 回滚到指定备份 |
| `/admin/config/reload` | POST | 进程内重载模型配置 |
| `/metrics` | POST / GET | Prometheus 文本指标（GET 留给 Prometheus 抓取） |

## 统计口径

- 一次客户端请求 = 一个 `gateway_request_id`；每次上游尝试一行（`attempt_key = gateway_request_id-attempt_seq`）。
- `outcome` 取值：`stream_open`（流已交给上层、尚未收尾，`finalized=0`）、`success`、`aborted`（客户端中断）、`failure`。
- `done_sent`（0/1）：网关是否已把终止标记 `[DONE]` 转发给客户端。`aborted` 只出现在 `done_sent=0` 的行上；
  客户端读到 `[DONE]` 之后才关闭连接（工具调用的正常节奏）算内容已发完，记 `success`。
- **成功率只统计 `finalized=1` 的行**：`success_rate = success / finalized`，未收尾的流不进分母。
- `P95`/`P50` 用名次近似（`ROW_NUMBER`），只统计已收尾且 `duration_ms` 非空的尝试；缺数据的分组返回 `null`。
- `failover_count` = 同一请求里第 2 次及之后的尝试数，即实际发生的故障转移次数。
- `api_user` 是调用方标签：管理密钥、未启用鉴权与历史行都记 `admin`，用户级密钥记创建时填写的名称。
  用户级密钥只以 SHA-256 摘要存放在独立的 `data/api_keys.db`（`API_KEYS_DB_PATH`），审计库里没有密钥。
- **两套保留策略**：`audit_attempts` 明细默认保留 30 天（`AUDIT_RETENTION_DAYS`），用于下钻与排障；
  `usage_daily` 日汇总只累加、不清理，用于永久保留输入/输出/缓存 tokens 等用量。
- **维度日汇总**：`usage_dimension_daily` 按 `(day, endpoint, api_user, dimensions)` 累加，
  同样只增不删；`dimensions` 是完整维度组合的规范 JSON，查询时可按任意子集分组。

---

## POST /admin/usage/summary

查询参数：`group_by`（`model|platform|user|endpoint|selector|outcome|error|day|hour`，默认 `model`）、
`since`、`until`、`platform`、`model`（模型名，不是 selector）、`endpoint`、`user`（用户标签）、`limit`（1~1000）、
`sort`（`requests|p95|duration|ttft|throughput|failover|errors|tokens`）。

```json
{
  "group_by": "model",
  "range": { "since": "2026-01-01T00:00:00+00:00", "until": "2026-01-02T00:00:00+00:00" },
  "count": 1,
  "items": [
    {
      "bucket": "example-platform|model-a",
      "attempts": 3, "requests": 2, "finalized": 3,
      "success": 2, "failure": 1, "aborted": 0, "retries": 1, "disabled_models": 0,
      "avg_duration_ms": 1033.33, "avg_ttft_ms": 132.5, "avg_probe_ms": 33.3,
      "avg_throughput": 42.5,
      "input_tokens": 200, "cached_tokens": 0, "output_tokens": 400, "output_chars": 1600,
      "success_rate": 0.6667, "avg_attempts": 1.5, "failover_count": 1,
      "p50_duration_ms": 1000.0, "p95_duration_ms": 1100.0,
      "min_duration_ms": 1000.0, "max_duration_ms": 1100.0
    }
  ]
}
```

`group_by` 为 `day`/`hour` 时按时间先后返回，其余按 `sort` 排序；派生列（`success_rate`、`p95_duration_ms` 等）
不在 SQL 里，聚合后在 Python 端补齐，缺失值为 `null`。

## POST /admin/usage/daily

全量日用量：按 `(日期, 用户)` 返回，数据来自独立日汇总表 `usage_daily`，**永久保留**，
不随审计明细的 30 天保留期清理。查询参数：`since`、`until`、`user`、`limit`（1~5000）。

```json
{
  "range": { "since": null, "until": "2026-01-03T00:00:00+00:00" },
  "count": 2,
  "totals": {
    "requests": 42, "attempts": 45, "success": 43, "failure": 2, "aborted": 0, "retries": 3,
    "input_tokens": 12000, "cached_tokens": 3000, "output_tokens": 8000, "success_rate": 0.9556
  },
  "items": [
    {
      "day": "2026-01-01", "api_user": "user-a",
      "requests": 20, "attempts": 21, "success": 20, "failure": 1, "aborted": 0, "retries": 1,
      "input_tokens": 6000, "cached_tokens": 1500, "output_tokens": 4000,
      "updated_at": "2026-01-01T10:05:00.000+00:00"
    }
  ]
}
```

`items` 按 `day` 升序、`api_user` 升序；`totals` 是同一筛选范围内的合计，前端直接用于顶部卡片。

## POST /admin/usage/timeseries

查询参数：`bucket`（`hour|day`）、`since`、`until`、`platform`、`model`、`endpoint`、`user`。

```json
{
  "bucket": "hour",
  "range": { "since": "...", "until": "..." },
  "points": [ { "bucket": "2026-01-01T10", "...": "与 summary.items 的字段完全相同" } ]
}
```

`points` 一定按桶升序，前端直接拿来画折线。

## POST /admin/usage/models

查询参数：`sort`、`since`、`until`、`limit`（1~500）、`user`。返回 `{ "sort": "...", "range": {...}, "items": [...] }`，
`items` 的字段与 `summary.items` 相同。

## POST /admin/usage/dimensions

永久维度日汇总。查询参数：`group_by`（逗号分隔，支持 `day` / `month` / `user` / `endpoint` 与任意维度名，
例如 `model`、`model,size`、`month,user,model`）、`since`、`until`、`user`、`endpoint`、`limit`（1~1000）。

```json
{
  "group_by": ["month", "user", "model"],
  "range": { "since": "2026-01-01T00:00:00+00:00", "until": "2026-02-01T00:00:00+00:00" },
  "count": 2,
  "items": [
    {
      "bucket": "2026-01 | user-a | model-a",
      "values": { "month": "2026-01", "user": "user-a", "model": "model-a" },
      "requests": 12, "attempts": 12, "success": 12, "failure": 0, "aborted": 0, "retries": 0,
      "input_tokens": 12000, "cached_tokens": 3000, "output_tokens": 8000,
      "success_rate": 1.0
    }
  ]
}
```

数据来自 `usage_dimension_daily`，只累加、不清理；明细保留期清理后历史仍可查。
`input_tokens` / `cached_tokens` / `output_tokens` 来自审计行，上游没有返回 usage 时为空值按 0 累计。

## POST /admin/usage/dimensions/export

把同一组筛选条件下的维度日汇总导出为 CSV。查询参数与 `/admin/usage/dimensions` 相同
（`group_by`、`since`、`until`、`user`、`endpoint`），但没有 `limit`：导出全部匹配分组。

响应头为 `Content-Type: text/csv; charset=utf-8`，文件带 UTF-8 BOM，可直接用 Excel 打开。
列顺序是「所选维度列 + 请求 / 尝试 / 成功 / 失败 / 中断 / 输入tokens / 缓存tokens / 输出tokens / 成功率」。

## POST /admin/usage/dimension-keys

维度候选：已配置的透传路由 + 历史表里出现过的维度名。

```json
{
  "routes": [
    { "route": "example-service", "prefix": "/proxy/example-service",
      "endpoint": "passthrough:example-service", "enabled": true,
      "dimensions": ["model", "size"] }
  ],
  "stored": ["model", "size", "platform", "selector"],
  "keys": ["model", "platform", "selector", "size"]
}
```

## POST /admin/audit

查询参数：`since`、`until`、`platform`、`model`、`outcome`、`endpoint`、`user`、`gateway_request_id`、
`limit`（1~500）、`offset`。

```json
{
  "total": 128, "limit": 50, "offset": 0,
  "items": [
    {
      "attempt_key": "req-7-1", "gateway_request_id": "req-7", "request_id": "req-7",
      "api_user": "user-a",
      "endpoint": "chat_completions", "stream": 1, "selector": "all",
      "platform": "example-platform", "model_name": "example-platform|model-a", "model_id": "model-a",
      "attempt_seq": 1, "retry_seq": 1,
      "ts": "2026-01-01T10:00:00.000+00:00",
      "outcome": "failure", "finalized": 1, "http_status": 429,
      "error_category": "rate_limit", "error_code": "rate_limited", "disabled_model": 0,
      "duration_ms": 1000.0, "probe_ms": 30.0, "ttft_ms": 120.0,
      "has_usage": 0, "upstream_id": null, "throughput": null,
      "input_tokens": null, "cached_tokens": null, "output_tokens": null, "output_chars": null,
      "tool_call_converted": 0, "keepalive_count": 0, "msg_count": 1, "done_sent": 0,
      "dimensions": "{\"platform\":\"example-platform\",\"model\":\"example-platform|model-a\",\"model_id\":\"model-a\",\"selector\":\"all\"}",
      "platform": "example-platform", "model_name": "example-platform|model-a", "model_id": "model-a", "selector": "all"
    }
  ]
}
```

`dimensions` 是唯一的写入源；`platform` / `model_name` / `model_id` / `selector` 是 SQLite 生成列，
由 `dimensions` 自动派生，保持旧字段可读。

`items` 是数据库原始行（布尔列以 `0/1` 出现），列顺序与落库列一致。
老库缺 `done_sent` / `api_user` 时启动会自动补列：`done_sent` 会给历史上记成 `success` 的流式行回填成 `1`，
`api_user` 留空（启动迁移会统一回填为 `admin`）；旧结局一条都不重算。
前端按 `outcome` 上色、按 `gateway_request_id` 下钻到链路。

## POST /admin/audit/prune

请求体：`{ "before": "2026-01-01" }`（必填，支持 ISO 时间、日期或 `30d`）。
返回 `{ "deleted": 12, "before": "2026-01-01T00:00:00.000+00:00" }`。

## POST /admin/audit/chain/{gateway_request_id}

```json
{ "gateway_request_id": "req-7", "attempts": [ { "...": "同 /admin/audit 的行" } ] }
```

没有记录时 `attempts` 为 `[]`（不是 404）。

## POST /admin/audit/stats

```json
{
  "config": {
    "enabled": true, "db_path": "data/audit.db", "retention_days": 30,
    "queue_size": 10000, "batch_size": 200, "flush_interval": 5.0,
    "record_failed_only": false, "include_request_meta": true,
    "prometheus_enabled": true, "metrics_label_model": false
  },
  "collector": {
    "enabled": true, "pending_rows": 0, "submitted_total": 128, "finalized_total": 120,
    "skipped_total": 0, "dropped_total": 0, "write_errors_total": 0, "failover_total": 9,
    "last_flush_at": "2026-01-01T10:00:05.000+00:00",
    "batch_size": 200, "flush_interval": 5.0, "queue_size": 10000, "utc_now": "..."
  },
  "store": { "rows": 128, "dimension_rows": 24, "oldest": "...", "newest": "...", "db_path": "data/audit.db", "db_bytes": 65536 }
}
```

审计未启动时 `collector` 只有 `{"enabled": false}`、`store` 为 `{"enabled": false, "rows": 0}`（接口不会 500）。

---

## POST /admin/users/list

用户级密钥清单。只返回可读前缀，**永不返回明文或摘要**。

```json
{
  "count": 1,
  "items": [
    { "id": 1, "name": "user-a", "key_prefix": "gw_ab12cd", "created_at": "2026-01-01T10:00:00.000+00:00" }
  ]
}
```

## POST /admin/users（新增用户）

请求体：`{ "name": "user-a" }`（1~64 个字符，不能重名）。

```json
{
  "user": { "id": 1, "name": "user-a", "key_prefix": "gw_ab12cd", "created_at": "..." },
  "key": "gw_ab12cd..."
}
```

`key` 是明文；数据库保存 SHA-256 摘要和 Fernet 密文，之后仍可通过 `/admin/users/key` 取回。
用户名重复返回 `409 {"error": {"code": "user_exists"}}`；名称为空、超长或使用保留名返回 `400/422`。

## POST /admin/users/key

请求体：`{ "name": "user-a" }`。返回该用户的密钥明文：

```json
{
  "user": { "id": 1, "name": "user-a", "key_prefix": "gw_ab12cd", "created_at": "..." },
  "key": "gw_ab12cd..."
}
```

- 只接受管理密钥；
- 用户不存在返回 `404 user_not_found`；
- 旧版本创建、没有密文的用户返回 `409 key_unavailable`；
- 密文解密失败返回 `500 key_decrypt_failed`。

## POST /admin/users/{user_id}/delete

返回 `{ "deleted": true, "id": 1 }`。删除后该用户的密钥立即失效，历史审计记录保留。
用户不存在返回 `404`，`user_id` 非法返回 `400`。

---

## POST /admin/config/document

```json
{
  "path": "models.yaml",
  "version": "3f236f1ac5ed...",
  "bytes": 920,
  "modified_at": "2026-01-01T10:00:00+00:00",
  "audit": { "enabled": true, "retention_days": 30 },
  "passthrough": [
    {
      "name": "example-service", "enabled": true, "prefix": "/proxy/example-service",
      "upstream": "https://api.example.com/v1",
      "apiKey": { "kind": "env_placeholder", "env": "EXAMPLE_PLATFORM_API_KEY",
        "convention_env": "PASSTHROUGH_IMAGE_GENERATION_API_KEY", "env_set": true, "preview": "" },
      "auth_header": "Authorization", "auth_prefix": "Bearer ",
      "timeout": 300, "connect_timeout": 10, "body_limit": 16777216,
      "forward_headers": ["x-request-id"],
      "extra_headers": { "X-API-Version": "2026-01-01" },
      "dimensions": { "model": "body.model", "size": "body.size" }
    }
  ],
  "aliases": [ { "name": "alias-a", "selectors": ["example-platform|model-a"] } ],
  "platforms": [
    {
      "name": "example-platform",
      "baseUrl": "https://api.example.com/v1",
      "apiKey": {
        "kind": "env_placeholder", "env": "EXAMPLE_PLATFORM_API_KEY",
        "convention_env": "EXAMPLE-PLATFORM_API_KEY",
        "env_set": true, "convention_env_set": false, "preview": ""
      },
      "enabled": true, "weight": 10, "timeout": 120, "retry_count": 3,
      "quota_period": null, "connect_timeout": 5, "first_chunk_timeout": 45,
      "stream_probe_timeout": null, "enable_tool_call_conversion": true,
      "blacklist": ["model-x"],
      "models": [ { "name": "model-a", "selector": "example-platform|model-a", "overrides": { "first_chunk_timeout": 45 }, "writable": true } ],
      "plugin": { "code": "plugin.example-scraper", "scraper": true, "args": ["scrape_url"] }
    }
  ],
  "editable": {
    "platform_fields": ["apiKey", "baseUrl", "blacklist", "connect_timeout", "enable_tool_call_conversion", "enabled", "first_chunk_timeout", "quota_period", "retry_count", "stream_probe_timeout", "timeout", "weight"],
    "model_fields": ["connect_timeout", "enable_tool_call_conversion", "enabled", "first_chunk_timeout", "quota_period", "retry_count", "stream_probe_timeout", "timeout", "weight"],
    "audit_fields": ["batch_size", "db_path", "enabled", "flush_interval", "include_request_meta", "metrics_label_model", "prometheus_enabled", "queue_size", "record_failed_only", "retention_days"],
    "passthrough_fields": ["apiKey", "auth_header", "auth_prefix", "body_limit", "connect_timeout", "dimensions", "enabled", "extra_headers", "forward_headers", "prefix", "timeout", "upstream"],
    "ops": ["set_platform_field", "unset_platform_field", "add_platform", "remove_platform", "add_model", "remove_model", "set_model_field", "unset_model_field", "set_alias_selectors", "set_audit_field", "add_passthrough", "remove_passthrough", "set_passthrough_field", "unset_passthrough_field"]
  },
  "errors": [], "warnings": []
}
```

`models` 里字符串写法的条目 `writable=false`：给它加覆盖字段会先把条目改写成对象写法（注释可能随之移动，`warnings` 会提示）。

## POST /admin/config/raw

`{ "path": "...", "version": "...", "bytes": 920, "text": "原始 YAML" }`。只用于预览，写回必须走补丁接口。

## POST /admin/config/schema

`{ "ops": [...], "platform_fields": {字段: 类型}, "model_fields": {...}, "audit_fields": {...}, "passthrough_fields": {...}, "limits": {字段: {"min": 1, "max": 3600}}, "enums": {字段: {"values": [...], "allow_none": true}}, "docs": {...}, "sections": [...] }`。
前端据此决定控件形状（下拉 / 带边界的 number 输入框 / 标签勾选 / 手填输入框）、取值范围提示，以及每个字段的说明（这一页本身就是文档）。

`docs` 按作用域给出，覆盖对应白名单里的**每一个**字段：

```json
{
  "docs": {
    "platform": {
      "retry_count": {
        "label": "单模型重试次数",
        "summary": "只在请求的 model 写成「平台名|模型名」时生效……",
        "unit": "次数（整数）",
        "default": "3 次",
        "example": "retry_count: 1",
        "effect": "热重载",
        "note": "只试一次请写 1：写 0 会走到没有兜底的重试分支……"
      }
    },
    "model": { "...": "与 platform 共用语义（写在模型条目里覆盖平台级取值）" },
    "audit": { "enabled": { "label": "审计总开关", "effect": "重启" } },
    "passthrough": { "dimensions": { "label": "汇总维度", "effect": "热重载" } }
  }
}
```

- `effect` 只有两种：`热重载`（进程内立即生效）/ `重启`（进程内改不干净，网关永不自行重启）。
- 同一个字段名在不同作用域含义不同（`enabled` 在平台级是「是否参与转发」，在 audit 段是「审计总开关」），所以必须按作用域取。
- 说明的唯一来源是 `openai_proxy/config_store/field_docs.py`；接口不返回字典里没有的字段说明，前端也不会自己编文案。
- 每条说明还带 `values`（取值只有有限几种时列出，否则为空数组）与 `allow_none`（这个字段能不能「不配置」）。

`enums` 是把所有 `values` 非空的字段单独汇总一份，供前端渲染下拉：
`{ "quota_period": { "values": ["hourly", "daily", "weekly", "monthly"], "allow_none": true } }`。
`allow_none` 为真时前端给出「不配置」选项，走 `unset_*` 操作删掉这一行（回落到上层），而不是往文件里写 `null`。
选项与 `docs[*].unit` 里写的取值范围同源，测试会核对两边一致、且每个选项都能过后端校验。

`sections` 是配置文件本身的结构说明（`openai_proxy/config_store/config_guide.py`）：
`[ { "title": "改动什么时候真正生效", "body": "热重载做的事：……" } ]`，覆盖条目写法、`model` 的四种写法、注释保留规则、并发保护与回滚。

`limits` 里的下限就是写盘会被拒绝的下限，例如 `retry_count` 是 `1~20`（写 0 会让重试分支没有兜底赋值），
越界时报错信息里会附上原因。

## POST /admin/config/env-status

`[ { "env": "EXAMPLE_PLATFORM_API_KEY", "platform": "example-platform", "source": "yaml_placeholder", "set": true } ]`。
`source` 为 `yaml_placeholder`（配置里写的 `${...}`）或 `platform_name_convention`（按平台名约定的环境变量）。
**永不返回密钥内容**。

## POST /admin/config/models

`{ "loaded": true, "total": 46, "platforms": [ { "name": "example-platform", "models": [ { "name": "model-a", "selector": "example-platform|model-a" } ] } ], "note": "" }`。

- 存在理由：爬虫平台的模型是插件运行时灌进来的，`models.yaml` 里根本没有条目；「屏蔽模型」与「别名指向」要给出可勾的候选，只能问进程当前加载了哪些模型。
- `name` 是模型条目名（`blacklist` 的通配匹配用的就是它）；`selector` 是别名指向的写法 `平台名|条目名`。
- `loaded=false` 时 `platforms` 为空、`note` 给人话说明（服务刚启动或模型加载失败），界面据此解释下拉为什么是空的。
- 只给名字：**永不返回密钥、baseUrl 或插件参数**。

## POST /admin/config/validate

`{ "version": "...", "valid": true, "errors": [], "warnings": [] }`。
`errors` 会阻止任何写盘；`warnings` 只提示（例如环境变量当前未设置）。

## POST /admin/config/backups

`{ "items": [ { "name": "models.yaml.20260101T100000Z.bak", "created_at": "...", "bytes": 920, "version": "3f236f1a...", "note": "调权重", "path": "..." } ] }`。

## POST /admin/config/plan

请求体：

```json
{
  "ops": [ { "op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 5 } ],
  "version": "客户端看到的 version（也可用 If-Match 头）"
}
```

返回：

```json
{
  "changes": [ { "op": "set_platform_field", "path": "platforms.example-platform.weight", "before": 10, "after": 5 } ],
  "diff": ["--- 当前配置", "+++ 应用补丁后", "@@ -9,5 +9,5 @@", "-  weight: 10 # 优先级最高", "+  weight: 5  # 优先级最高"],
  "diff_rows": [
    { "kind": "modify", "before": "weight: 10 # 优先级最高", "after": "weight: 5  # 优先级最高", "context": "example-platform:" }
  ],
  "errors": [], "warnings": [], "valid": true,
  "needs_restart": false,
  "version_before": "3f236f1a...", "version_after": "66a0bd07..."
}
```

`diff` 是给人读的整段文本；`diff_rows` 是界面用的形状，每一行给出 `before`（原行）、`after`（新行）、
`context`（改动块上方最近的段名，例如 `example-platform:` / `models:`，用来说明这一行属于谁），
以及决定颜色的 `kind`：

| kind | 含义 | 界面颜色 |
| --- | --- | --- |
| `modify` | 同一块里旧行与新行成对出现（不会拆成一删一增两条颜色） | 黄 |
| `add` | 只有新行（`before` 为空） | 绿 |
| `remove` | 只有旧行（`after` 为空） | 红 |

`needs_restart` 为 `true` 表示改动涉及插件/爬虫结构（`add_platform` / `remove_platform` / `.plugin`），
必须你自己重启服务。

补丁操作一览：

| op | 必填参数 | 备注 |
| --- | --- | --- |
| `set_platform_field` | `platform`, `field`, `value` | 改平台字段（`blacklist` 用列表值） |
| `unset_platform_field` | `platform`, `field` | 删除平台字段，回落到全局默认 |
| `add_platform` | `platform`, `config` | 需要重启 |
| `remove_platform` | `platform` | 需要重启 |
| `add_model` | `platform`, `model`, `overrides` | 静态条目 |
| `remove_model` | `platform`, `model` | 爬虫平台会被拒绝：改用 `blacklist` |
| `set_model_field` | `platform`, `model`, `field`, `value` | 模型级覆盖 |
| `unset_model_field` | `platform`, `model`, `field` | 回落到平台取值 |
| `set_alias_selectors` | `alias`, `selectors` | 整列表覆盖；别名不存在时会把它建出来。指向不存在的平台只报 warning（爬虫平台的模型是运行时灌入的，所以不校验模型段） |
| `set_audit_field` | `field`, `value` | 写顶层 `audit:` 段 |
| `add_passthrough` | `name`, `config` | 新增透传路由 |
| `remove_passthrough` | `name` | 删除透传路由 |
| `set_passthrough_field` | `name`, `field`, `value` | 改透传字段（`dimensions` / `extra_headers` 用对象值） |
| `unset_passthrough_field` | `name`, `field` | 删除透传字段，回落到默认值 |

管理台覆盖全部 14 个操作：`set_platform_field` / `unset_platform_field`（「删除该字段（回落）」按钮）、
`add_model`（「新增模型条目」：只有模型名要打字，覆盖用字段下拉逐项「加一项覆盖」）、
`remove_model`、`set_model_field` / `unset_model_field`、`set_alias_selectors`、`set_audit_field`、
`add_platform`、`remove_platform`、`add_passthrough` / `remove_passthrough`、
`set_passthrough_field` / `unset_passthrough_field`。CLI 与界面走的是同一套补丁操作，效果等价。

注释保留代价（`warnings` 会逐条列出）：只改取值不动注释；条目写法变化（字符串 ↔ 对象）会摘掉该条目附带的注释；
删除条目会摘掉它的注释并修正其余条目的注释下标；整列表覆盖（别名）会丢掉附着在这些行上的注释。

## POST /admin/config/apply

请求体同 `plan`，另有 `note`（写进备份元数据，≤200 字）、`reload`（默认 `true`，写盘后尝试进程内热重载）。
返回 `plan` 的全部字段，再加：

```json
{
  "backup": "models.yaml.20260101T100000Z.bak",
  "version_after": "66a0bd07...",
  "reloaded": true,
  "reload": { "reloaded": true, "needs_restart": false, "message": "模型配置已重载" }
}
```

校验有 `errors` 时返回
`400 {"error": {"code": "validation_failed", "message": "...", "details": {"errors": [...]}}}`，**不写盘**；
版本不匹配返回
`409 {"error": {"code": "version_conflict", "message": "...", "details": {"expected": "...", "actual": "..."}}}`。

## POST /admin/config/rollback

请求体 `{ "name": "models.yaml.20260101T100000Z.bak", "reload": true }`（版本号只可通过 `If-Match` 传）。

```json
{
  "rolled_back_to": "models.yaml.20260101T100000Z.bak",
  "backup": "models.yaml.20260101T110000Z.bak",
  "version_before": "66a0bd07...", "version_after": "3f236f1a...",
  "reloaded": true
}
```

回滚前当前内容也会备份，所以回滚本身可以再回滚。

## POST /admin/config/reload

无请求体。返回 `{ "reloaded": true, "needs_restart": false, "message": "模型配置已重载" }`；
服务未提供热重载能力时返回 `409`。

## POST /metrics（GET 仍保留给 Prometheus）

Prometheus 文本格式。`audit.prometheus_enabled=false` 时返回 `404`。

| 指标 | 类型 | 标签 |
| --- | --- | --- |
| `proxy_requests_total` | Counter | `platform`,`status`,`error_type` |
| `proxy_errors_total` | Counter | `platform`,`error_type` |
| `proxy_failover_total` | Counter | `from_platform`,`to_platform` |
| `proxy_request_duration_seconds` | Histogram | `platform` |
| `proxy_stream_ttft_seconds` | Histogram | `platform` |
| `proxy_stream_duration_seconds` | Histogram | `platform` |
| `proxy_stream_throughput_tokens_per_second` | Histogram | `platform` |
| `proxy_stream_tokens_total` | Counter | `platform`,`token_type` |
| `proxy_usage_missing_total` | Counter | `platform` |
| `platform_availability` | Gauge | `platform`,`model` |
| `cache_hits_total` / `cache_misses_total` | Counter | 无 |
| `active_connections` / `pending_requests` | Gauge | 无 |

默认不带 `model` 标签，避免模型数量把标签基数撑爆；开关是环境变量 `METRICS_LABEL_MODEL`（启动时读，
改完要重启），打开后前 5 项与三个流式直方图会多出 `model` 标签。

---

## 错误形状

非转发接口统一返回：

```json
{
  "error": {
    "code": "user_exists",
    "message": "用户名已存在: user-a",
    "details": { "errors": [] }
  }
}
```

- `code` 用于程序判断；
- `message` 用于直接展示；
- `details` 只在有结构化信息时出现（校验错误列表、版本冲突的 `expected` / `actual` 等）；
- HTTP 状态码仍然保留，用来区分大类。

| 状态码 | 常见 code | 场景 |
| --- | --- | --- |
| 400 | `bad_request` / `invalid_parameter` / `invalid_name` / `invalid_user_id` / `version_mismatch` / `validation_failed` | 参数非法、用户名非法、校验不通过 |
| 401 | `unauthorized` | 密钥缺失或不正确 |
| 403 | `admin_disabled` / `forbidden` | 未配置管理密钥 / 用户级密钥访问管理接口 |
| 404 | `not_found` / `user_not_found` / `metrics_disabled` / `file_not_found` | 资源不存在 |
| 405 | `method_not_allowed` | 非转发接口用了 GET/DELETE，应改用 POST |
| 409 | `version_conflict` / `user_exists` / `reload_unavailable` / `key_unavailable` | 版本冲突、用户重名、热重载不可用、旧密钥无法取回 |
| 422 | `validation_error` | 请求体不符合 schema，`details.errors` 是字段级错误 |
| 500 | `internal_error` / `write_failed` / `key_generation_failed` / `key_decrypt_failed` | 服务内部、写盘或密钥解密失败 |
| 503 | `key_store_unavailable` / `key_encryption_unavailable` | 用户密钥库或加密密钥未就绪 |

`/v1/*` 转发协议接口**不使用**上面的形状，继续保持 OpenAI/上游兼容的错误体。
