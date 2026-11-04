# 🔀 透传接口与维度用量

透传用于把任意第三方接口按前缀原样转发，同时复用网关的用户鉴权与审计能力。
所有示例使用占位符：`example-platform` / `https://api.example.com/v1` / `${EXAMPLE_PLATFORM_API_KEY}` / `model-a`。

---

## 1. 配置

在 `models.yaml` 顶层增加 `passthrough:` 段：

```yaml
passthrough:
  example-service:
    enabled: true
    prefix: "/proxy/example-service"
    upstream: "https://api.example.com/v1"
    apiKey: "${EXAMPLE_PLATFORM_API_KEY}"
    timeout: 300
    connect_timeout: 10
    body_limit: 16777216
    forward_headers:
      - "x-request-id"
    extra_headers:
      X-API-Version: "2026-01-01"
    dimensions:
      model: "body.model"
      size: "body.size"
      quality: "body.quality"
```

路径映射：

```text
客户端  /proxy/example-service/assets/render?size=1024
上游    https://api.example.com/v1/assets/render?size=1024
```

常用字段：

| 字段 | 说明 |
| --- | --- |
| `prefix` | 对外前缀，必须以 `/proxy/` 开头；不写默认 `/proxy/<路由名>` |
| `upstream` | 上游基地址，剩余路径与查询串原样拼在它后面 |
| `apiKey` | 上游密钥；支持 `${ENV_VAR}`，也支持 `PASSTHROUGH_<路由名>_API_KEY` |
| `auth_header` / `auth_prefix` | 上游鉴权头名称与前缀，默认 `Authorization` / `Bearer ` |
| `timeout` / `connect_timeout` | 总超时与连接超时（秒） |
| `body_limit` | 允许缓存的请求体上限，超过返回 413 |
| `forward_headers` | 额外放行的客户端请求头；`Authorization` 永不转发 |
| `extra_headers` | 固定附加到上游请求的头 |
| `dimensions` | 从请求里提取的汇总维度 |

---

## 2. 维度

维度是唯一可扩展的分组概念：

```yaml
dimensions:
  model: "body.model"
  size: "body.size"
  tenant: "header.x-tenant"
  stage: "path.0"
```

支持四种来源：

| 写法 | 来源 |
| --- | --- |
| `body.data.items[0].name` | JSON 请求体，支持嵌套路径与数组下标 |
| `query.size` | 查询参数 |
| `header.x-tenant` | 请求头，大小写不敏感 |
| `path.0` | 前缀之后的第 1 段路径 |

`model`、`platform`、`model_id`、`selector` 是标准维度键，转发器会给默认值：

```text
platform = passthrough
model    = 路由名
model_id = 路由名
selector = 路由名
```

如果配置里声明了同名键，就覆盖默认值。例如：

```yaml
dimensions:
  model: "body.model"
```

此时 `model` 就是请求体里的业务模型；查询、审计明细与管理台的“按模型”视图都会用它。
`model_id` 是独立的标准键，需要它也跟着变时再写一行 `model_id: "body.model"`。

没有“模型维度”这种第二概念：**键名就是分组名**。如果上游参数叫 `engine`，但你想按模型汇总，就把它命名为 `model`：

```yaml
dimensions:
  model: "body.engine"
```

---

## 3. 审计口径

每次透传请求记一条审计行：

```text
endpoint   = passthrough:example-service
platform   = passthrough（可由 dimensions.platform 覆盖）
model_name = dimensions.model
dimensions = {"platform":"passthrough","model":"model-a","size":"1024x1024",...}
```

结局：

- 响应完整转发且上游状态码 2xx/3xx：`success`
- 上游 4xx/5xx：`failure`，记录 `http_status` 与 `upstream_4xx` / `upstream_5xx`
- 客户端中途断开：`aborted`
- 连接失败 / 超时：`failure`，返回 502 / 504

透传不重试、不做故障转移，一个请求只有一条 attempt。
这类接口通常没有 token usage，tokens 列为空；调用量、耗时、状态码照常统计。

---

## 4. 永久维度日汇总

后台刷盘时会把已收尾的审计行累加进 `usage_dimension_daily`：

```text
(day, endpoint, api_user, dimensions) → requests / attempts / success / failure / aborted / retries
```

- 按完整维度组合存一行，查询时可按任意子集分组；
- 只累加、不删除，审计明细保留期清理后历史仍在；
- 每个透传请求只有一条 attempt，所以按用户/维度求和不会重复计数请求。

查询命令：

```bash
python manage.py usage dimensions --group-by model,size --since 30d
python manage.py usage dimensions --group-by day,user,model --since 30d \
  --endpoint passthrough:example-service
```

HTTP：

```bash
curl -X POST -H "Authorization: Bearer $GATEWAY_API_KEY" \
  "http://localhost:8000/admin/usage/dimensions?group_by=model,size&since=30d"

curl -X POST -H "Authorization: Bearer $GATEWAY_API_KEY" \
  "http://localhost:8000/admin/usage/dimensions?group_by=day,user,model,size&user=user-a"
```

审计明细保留期内也可以直接按维度聚合：

```bash
python manage.py usage summary --group-by dim:model,size --since 7d
```

---

## 5. 管理台

- 「模型配置」页的“透传接口”面板可以新增/删除路由、编辑字段、维护维度键值对；
- 「用量与性能」页的“透传维度汇总”面板可以选择路由、维度组合、用户和时间范围；
- 所有改动走现有补丁、预演、备份、回滚与热重载机制。

---

## 6. 边界与安全

- 只代理 HTTP/HTTPS；不跟随上游重定向，3xx 原样返回给客户端。
- 客户端 `Authorization`、`Cookie`、`Host` 等头不会转发到上游；上游鉴权统一使用配置密钥。
- 请求体会被缓存以支持维度提取，默认上限 16MB，超过返回 413。
- 只提取配置声明的维度字段，单值截断 200 字符；不记录请求体内容。
- `audit.record_failed_only=true` 会导致成功调用不进入维度汇总，需要完整维度统计时保持默认 `false`。
- 依赖 SQLite 3.31+ 的 JSON1 与生成列；当前运行环境为 3.37.2，已满足。
