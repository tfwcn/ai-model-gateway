# 🧾 审计与用量统计

网关的每一次上游调用都会写一行审计记录，用来回答三类问题：

- **用量**：哪个模型/平台被用了多少、消耗多少 tokens
- **性能**：首块耗时、总耗时、P95、tokens/s
- **可靠性**：失败率、故障转移了多少次、失败原因是什么

示例一律使用占位符：`example-platform` / `https://api.example.com/v1` / `${EXAMPLE_PLATFORM_API_KEY}` / `model-a`。

---

## 📋 目录

- [记录粒度](#记录粒度)
- [结局与统计口径](#结局与统计口径)
- [记录哪些字段](#记录哪些字段)
- [配置项](#配置项)
- [查询方式](#查询方式)
- [保留与清理](#保留与清理)
- [Prometheus 指标](#prometheus-指标)
- [对转发性能的影响](#对转发性能的影响)

---

## 记录粒度

**一次客户端请求 = 一个 `gateway_request_id`；每次上游尝试 = 一行。**

一次请求触发故障转移时会产生多行，`attempt_seq` 从 1 递增，因此可以完整还原「先试了谁、为什么换、最后成没成」：

```text
gateway_request_id = 1a2b3c4d
  attempt_key 1a2b3c4d-1  example-platform|model-a   failure      429 rate_limit
  attempt_key 1a2b3c4d-2  example-platform|model-b   success
```

流式调用会分两段写：
1. 首个有效内容拿到后立刻落一行 `outcome=stream_open`（`finalized=0`），保证「已经在转发的请求」即使进程被杀也留有痕迹；
2. 流结束时补齐 tokens/耗时，并按「[DONE] 是否已转发」改写结局：发完了记 `success`，没发完就被掐掉记 `aborted`。

## 结局与统计口径

| `outcome` | `finalized` | 含义 |
| --- | --- | --- |
| `stream_open` | 0 | 流已交给上层转发，还没收尾 |
| `success` | 1 | 非流式完成，或内容已发完（含「[DONE] 已转发后客户端才关闭连接」） |
| `aborted` | 1 | 客户端在 `[DONE]` 之前断开，内容确实没发完（`done_sent=0`） |
| `failure` | 1 | 这次上游尝试失败 |

`done_sent` 是判定 `aborted` 的依据，也是 `aborted` 与「正常收尾」的分水岭：

- 按 SSE 协议读流的客户端读到 `[DONE]` / `finish_reason` 就关连接去执行工具，此时上游可能还没结束 chunked body；
  这种情况内容其实已经发完，**不能算客户端中断**，否则带工具调用的会话会被系统性记成中断，成功率被压低。
- 转发循环看到 `[DONE]` 之后只再用 `SSE_DONE_DRAIN_TIMEOUT`（默认 0.5 秒）收可能跟在后面的 usage 尾片，
  然后正常收尾并释放上游连接，不再死等上游结束响应体。
- 老库升级按 `PRAGMA user_version` 幂等执行：`done_sent` 用 `ALTER TABLE` 追加，并把历史上记成 `success` 的流式行回填成「已发完」
  （旧实现只有把流读到底才记 success）；已经记成 `aborted` 的行保持原样，**旧结局一条都不重算**。

**成功率只统计 `finalized=1` 的行**：

```text
success_rate = success / finalized
failover_count = 同一请求里 attempt_seq > 1 的尝试数
```

`stream_open` 不进分母，否则「正在流式输出」会被当成失败，成功率会假得离谱。

`P95` / `P50` 用名次近似（`ROW_NUMBER`，SQLite 没有 percentile 函数），只统计已收尾且 `duration_ms` 非空的尝试；没有数据的分组返回 `null`，排序时缺失值一律排最后。

## 记录哪些字段

表名 `audit_attempts`（SQLite，WAL）：

| 分组 | 字段 |
| --- | --- |
| 身份 | `attempt_key`、`gateway_request_id`、`request_id`、`upstream_id` |
| 入口 | `endpoint`（`chat_completions` / `responses` / `config_write`）、`stream`、`selector` |
| 目标 | `platform`、`model_name`、`model_id`、`attempt_seq`、`retry_seq`、`disabled_model` |
| 时间 | `ts`（UTC 毫秒 ISO，分桶依据） |
| 结局 | `outcome`、`finalized`、`http_status`、`error_category`、`error_code`、`done_sent`（[DONE] 是否已转发） |
| 性能 | `duration_ms`、`probe_ms`、`ttft_ms`、`throughput`（tokens/s） |
| 用量 | `has_usage`、`input_tokens`、`cached_tokens`、`output_tokens`、`output_chars` |
| 其它 | `tool_call_converted`、`keepalive_count`（补发的心跳条数）、`msg_count` |

**只记录消息条数，绝不记录消息内容、密钥或请求体。**

通过管理接口/命令行改配置时也会写一行 `endpoint=config_write` 的审计行，`selector` 是补丁操作名，方便回答「这个权重是谁什么时候改的」。

## 配置项

优先级：**内置默认值 → `AUDIT_*` 环境变量 → `models.yaml` 顶层 `audit:` 段**（YAML 最高）。

```yaml
audit:
  enabled: true
  db_path: "data/audit.db"
  retention_days: 30
  batch_size: 200
  flush_interval: 2.0
  record_failed_only: false
  include_request_meta: false
  prometheus_enabled: true
  metrics_label_model: false
```

| 字段 | 环境变量 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enabled` | `AUDIT_ENABLED` | `true` | 关掉后热路径完全不走审计代码 |
| `db_path` | `AUDIT_DB_PATH` | `data/audit.db` | 审计库路径；查询工具按同一条规则解析它 |
| `retention_days` | `AUDIT_RETENTION_DAYS` | `30` | 启动时自动删除更早的记录；小于 1 的取值会被归一化回默认 30（写不出「永不清理」，要长期保留就写一个大天数） |
| `queue_size` | `AUDIT_QUEUE_SIZE` | `10000` | 内存队列容量，写库跟不上时丢弃最旧行并计数 |
| `batch_size` | `AUDIT_BATCH_SIZE` | `200` | 单次写库的最大行数 |
| `flush_interval` | `AUDIT_FLUSH_INTERVAL` | `2.0` | 后台刷盘间隔（秒） |
| `record_failed_only` | `AUDIT_RECORD_FAILED_ONLY` | `false` | 只记失败尝试，省磁盘但看不到用量 |
| `include_request_meta` | `AUDIT_INCLUDE_REQUEST_META` | `false` | 是否记录 `msg_count`（消息条数） |
| `prometheus_enabled` | `AUDIT_PROMETHEUS` | `true` | 审计落库时同步喂 Prometheus |
| `metrics_label_model` | `METRICS_LABEL_MODEL` | `false` | ⚠️ 真正生效的是**环境变量**：指标注册时就读环境变量，写在 `models.yaml` 里目前没有运行时效果（只会出现在 `/admin/audit/stats` 的回显里）。爬虫模式下基数很大，默认关 |

- 这一段的**每一个**字段都只在启动时应用一次：改完 `audit:` 段必须自行重启才生效（管理台会逐字段标注「重启」）。
- 环境变量的取值都在启动时读，改 `.env` 同样要重启。
- 非法取值会被归一化到安全范围：审计配置写错绝不会影响转发。

结局判定还受一个流式参数影响（它不属于 `audit:` 段，只走环境变量）：

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `SSE_DONE_DRAIN_TIMEOUT` | `0.5` | 转发完 `[DONE]` 之后，还为「可能跟在后面的 usage 尾片」多等这么多秒；到期即收尾并释放上游连接。取值只影响能否补到 usage 尾片，不影响「[DONE] 已发完」这一判定 |

## 查询方式

### 命令行

```bash
# 按模型看用量与性能
python manage.py usage summary --group-by model --since 7d

# 谁最慢 / 谁最不稳定 / 谁是用量大户
python manage.py usage models --sort p95
python manage.py usage models --sort errors
python manage.py usage models --sort tokens

# 按天看趋势
python manage.py usage timeseries --bucket day --since 30d

# 明细下钻，只看失败
python manage.py usage attempts --outcome failure --limit 20

# 还原一次请求的完整故障转移链
python manage.py usage chain 1a2b3c4d

# 审计自身健康状态（行数、缓冲、丢弃、写库失败）
python manage.py usage stats
```

详细参数见 [CONFIG_CLI.md](CONFIG_CLI.md)。

### HTTP 接口

全部需要 `Authorization: Bearer <GATEWAY_API_KEY>`，字段契约见 [../web/API.md](../web/API.md)。

```bash
curl -H "Authorization: Bearer $GATEWAY_API_KEY" \
  "http://localhost:8000/admin/usage/summary?group_by=model&since=24h&sort=p95"

curl -H "Authorization: Bearer $GATEWAY_API_KEY" \
  "http://localhost:8000/admin/audit?outcome=failure&limit=20"

curl -H "Authorization: Bearer $GATEWAY_API_KEY" \
  "http://localhost:8000/admin/audit/chain/1a2b3c4d"

curl -H "Authorization: Bearer $GATEWAY_API_KEY" \
  "http://localhost:8000/admin/audit/stats"
```

### 管理台

打开 `http://localhost:8000/admin/ui/`，「用量与性能」和「审计明细」两个标签页就是上面这些接口的图形化视图。
详见 [ADMIN_UI.md](ADMIN_UI.md)。

## 保留与清理

- 启动时按 `retention_days` 自动清理；
- 手动清理：`python manage.py usage prune --before 30d`，或 `DELETE /admin/audit?before=2026-01-01`；
- 库体积、最旧/最新记录时间可以从 `usage stats` 或 `/admin/audit/stats` 看到。

审计库就是一个普通 SQLite 文件，可以直接备份、拷贝到别处再查：

```bash
python manage.py usage stats            # 先看 db_path 指向哪里
cp data/audit.db /tmp/audit-copy.db
AUDIT_DB_PATH=/tmp/audit-copy.db python manage.py usage summary --group-by day --since 90d
```

## Prometheus 指标

`/metrics` 需要在 `GATEWAY_API_KEY` 保护下拉取（未配置密钥时返回 403）。指标清单见 [MONITORING.md](MONITORING.md)。

`proxy_usage_missing_total` 专门统计「上游没回 usage」的调用次数：这类调用的 tokens 统计不完整，只能看 `output_chars`。

## 对转发性能的影响

- 审计走内存队列 + 后台批量写库，热路径只构造一行对象并 `put`，不等磁盘；
- 队列满时丢弃最旧行并在 `dropped_total` 里计数，绝不阻塞请求；
- 任何审计异常都被吞掉并降级为关闭审计，服务照常转发；
- `enabled=false` 时上层全部走空实现，请求热路径零开销。

## 相关文档

- [管理台前端](ADMIN_UI.md)
- [命令行管理工具](CONFIG_CLI.md)
- [监控与运维](MONITORING.md)
- [配置指南](CONFIGURATION_GUIDE.md)
- [接口契约](../web/API.md)
