# ⌨️ 命令行管理工具（manage.py）

`manage.py` 用来**查看配置、改配置、看用量**，不需要正在运行的网关进程。

统一语法：

```bash
python manage.py <组> <动作> [参数]
```

示例一律使用占位符：`example-platform` / `https://api.example.com/v1` / `${EXAMPLE_PLATFORM_API_KEY}` / `model-a`。

---

## 📋 目录

- [公共参数](#公共参数)
- [退出码](#退出码)
- [配置组](#配置组)
- [平台与模型](#平台与模型)
- [别名与审计段](#别名与审计段)
- [用量统计](#用量统计)
- [与正在运行的网关是什么关系](#与正在运行的网关是什么关系)
- [脚本里怎么用](#脚本里怎么用)

---

## 公共参数

```bash
--config <路径>        配置文件路径（默认 models.yaml，也读 MODEL_CONFIG_FILE）
--backup-dir <路径>    备份目录（默认 data/config_backups，也读 CONFIG_BACKUP_DIR）
--json                 输出 JSON，便于脚本处理
--verbose              打印服务日志（默认只输出命令结果，日志走文件）
```

这些参数放在组前面或动作后面都能识别：

```bash
python manage.py --config /etc/gateway/models.yaml usage summary
python manage.py usage summary --config /etc/gateway/models.yaml
```

会写盘的动作额外支持：

```bash
--dry-run      只预演：打印变更与行级差异，绝不写盘
--note "文本"   写进备份元数据的备注（回滚时靠它认出是哪次改动）
--no-reload    写盘后不尝试进程内重载
```

## 退出码

| 码 | 含义 | 脚本里该怎么办 |
| --- | --- | --- |
| `0` | 成功 | 继续 |
| `1` | 失败：校验不通过、写盘被拒绝、审计库不可用 | 看 stderr 里的错误清单，改参数重试 |
| `2` | 版本冲突：配置在你操作期间被别人改过 | 直接重新执行一次命令即可（会读到最新配置） |
| `3` | 用法错误：参数不对 | 修命令 |

错误与警告一律走 stderr，标准输出只有结果，方便管道。

## 配置组

```bash
python manage.py config document                    # 看全部平台与别名（密钥只显示引用的环境变量）
python manage.py config document --platform example-platform
python manage.py config show example-platform       # 看某个平台的完整取值
python manage.py config validate                    # 校验当前配置文件
python manage.py config fields                      # 字段字典：每个能改的字段是干什么的、默认值、写法示例
python manage.py config fields --scope audit        # 只看 audit 段那一组
python manage.py config backups                     # 列出可回滚的备份
python manage.py config rollback models.yaml.20260101T000000Z.bak
```

`config fields` 就是管理台「模型配置」页里的那份字段字典（同一个来源 `openai_proxy/config_store/field_docs.py`
与 `config_guide.py`）：每个能改的字段给出它控制什么、默认值、取值单位与边界、`models.yaml` 里的写法示例、
改完是热重载还是需要重启，以及容易踩的坑。加 `--json` 时输出与 `POST /admin/config/schema` 的 `docs`/`sections` 完全一致。

`config validate` 的输出区分两类：

- **错误**：会阻止启动或阻止写盘（例如缺 `baseUrl`、`timeout` 越界）；
- **警告**：只提示（例如 `环境变量 EXAMPLE_PLATFORM_API_KEY 当前未设置，启动时该平台可能被判为无效`）。

回滚前当前内容也会先备份，所以回滚本身可以再回滚：

```text
已回滚到备份: models.yaml.20260101T100000Z.bak
回滚后的版本: d9f354084437
回滚前的内容也备份在: models.yaml.20260101T110000Z.bak（想撤销这次回滚就回滚到它）
```

## 平台与模型

```bash
# 改平台字段（列表型字段用 --values）
python manage.py platform set example-platform --field weight --value 5
python manage.py platform set example-platform --field blacklist --values model-a model-x
python manage.py platform unset example-platform --field timeout     # 删掉，回落到全局默认

# 新增/删除平台：改完需要你自行重启服务
python manage.py platform add new-platform \
  --base-url https://api.example.com/v1 \
  --api-key '${EXAMPLE_PLATFORM_API_KEY}' \
  --model model-a --weight 3 --first-chunk-timeout 30
python manage.py platform remove new-platform

# 模型条目与模型级覆盖
python manage.py model add example-platform model-new --field first_chunk_timeout --value 30
python manage.py model set example-platform model-a --field weight --value 5
python manage.py model unset example-platform model-a --field weight   # 回落到平台取值
python manage.py model remove example-platform model-a
```

模型级可覆盖的字段：`timeout`、`weight`、`enabled`、`quota_period`、`retry_count`、
`stream_probe_timeout`、`connect_timeout`、`first_chunk_timeout`、`enable_tool_call_conversion`。

**爬虫平台**（插件带 `scrape_url`）的模型由插件灌入，`model remove` 会被拒绝：

```text
错误 [platforms.scraper-platform.blacklist] 平台 scraper-platform 的模型由插件爬取，不能删除条目；请改用 blacklist 屏蔽该模型
```

先预演再写盘：

```bash
python manage.py model remove example-platform model-a --dry-run
```

```text
（--dry-run：没有写盘）
变更 1 处：
  platforms.example-platform.models
    之前: ['model-a', 'model-b']
    之后: ['model-b']
行级差异：
--- 当前配置
+++ 应用补丁后
@@ -12,3 +12,2 @@
-    - name: "model-a"
-      first_chunk_timeout: 45  # - "model-legacy"
     - name: "model-b"
警告 [配置] 改动后这条注释消失了: - "model-legacy"
```

## 别名与审计段

```bash
python manage.py alias list
python manage.py alias set alias-a --selector all --selector "example-platform|model-a"

python manage.py audit show                                  # 审计配置 + 审计库状态
python manage.py audit set --field retention_days --value 45 # 写 models.yaml 顶层 audit: 段
```

`alias set` 是**整列表覆盖**：附着在原来那些行上的注释会丢掉，命令会在警告里逐条列出。
别名不存在时这条命令会直接把它建出来（管理台里的「新增别名」用的是同一条操作）。
指向不存在的平台只报 warning，不会拦住写盘：`别名 alias-a 指向的平台 ghost-platform 不存在，这条指向不会命中任何模型`。

## 用量统计

```bash
python manage.py usage summary --group-by model --since 7d      # 聚合：用量 + 性能
python manage.py usage models --sort p95                        # 谁最慢
python manage.py usage daily --since 90d                        # 全量日用量（按天/用户，含输入/缓存/输出 tokens）
python manage.py usage dimensions --group-by model,size --since 30d  # 永久维度日汇总（透传接口）
python manage.py usage summary --group-by dim:model,size --since 7d  # 明细范围内按维度聚合
python manage.py usage timeseries --bucket day --since 30d      # 趋势
python manage.py usage attempts --outcome failure --limit 20    # 明细下钻
python manage.py usage chain 1a2b3c4d                           # 还原整条故障转移链
python manage.py usage stats                                    # 审计自身健康状态
python manage.py usage prune --before 30d                       # 清理旧明细（全量日用量不受影响）
```

聚合维度 `--group-by`：`model` / `platform` / `user` / `endpoint` / `selector` / `outcome` / `error` / `day` / `hour`，
以及 `dim:model,size` 这种任意维度组合。
`usage dimensions` 读永久表 `usage_dimension_daily`，支持 `day` / `user` / `endpoint` 与任意透传维度名，
例如 `--group-by model,size`、`--group-by day,user,model,size`。
排序 `--sort`：`requests` / `p95` / `duration` / `ttft` / `throughput` / `failover` / `errors` / `tokens`。
过滤：`--since` / `--until`（ISO 时间、日期或 `7d`、`24h`、`30m`）、`--platform`、`--model`、`--endpoint`。

`usage attempts` 与 `usage chain` 都带一列 `[DONE]`：`已发完` 表示终止标记已经转发给客户端（客户端随后关闭连接属正常，记 `success`），
`未发完` 才是内容被掐断（记 `aborted`）；`--json` 输出里对应原始字段 `done_sent`（0/1）。

```text
按 model 聚合，共 3 组
分组                        请求  尝试  成功率     切换  首块ms  P95ms  平均ms   tokens/s  输出tokens
------------------------  --  --  ------  --  ----  -----  -----  --------  --------
example-platform|model-a  2   2   50.0%   0   130   1,000  1,000  42.50     400
2example|model-c          1   1   0.0%    0   130   1,000  1,000  42.50     200
```

成功率只统计已收尾的尝试（`finalized=1`），未收尾的流式尝试不进分母，详见 [AUDIT.md](AUDIT.md)。

审计库路径按运行时同样的规则解析：默认值 → `AUDIT_*` 环境变量 → `models.yaml` 的 `audit:` 段（YAML 优先）。
审计关闭或库还不存在时会明确告诉你原因，而不是创建一个空库。

## 与正在运行的网关是什么关系

`manage.py` 直接读写配置文件，**不会也不该去重启别人的进程**。写盘成功后命令会提示：

```text
提示：如果网关正在运行，让改动立即生效有两种做法：
  1) 调用管理接口 POST /admin/config/reload（带 GATEWAY_API_KEY）
  2) 自行重启网关进程
```

```bash
curl -X POST -H "Authorization: Bearer $GATEWAY_API_KEY" \
  http://localhost:8000/admin/config/reload
```

- 平台级/模型级参数、别名、`audit:` 段：进程内重载即可生效；
- 新增/删除平台、改 `plugin` 结构：命令会标记「需要你自行重启服务」，进程内重载做不到。

服务本身永远不会自行重启。

## 脚本里怎么用

```bash
# 预演必须通过才真写盘
python manage.py platform set example-platform --field weight --value 5 --dry-run --json \
  | python -c 'import json,sys; p=json.load(sys.stdin); sys.exit(0 if p["valid"] else 1)' \
&& python manage.py platform set example-platform --field weight --value 5 --note "调权重"

# 把模型榜导给别的工具
python manage.py usage models --sort errors --json > /tmp/ranking.json
```

`--json` 模式下：
- 输出是纯 JSON，不会插提示文本；
- 校验失败也会输出 `{"valid": false, "errors": [...]}`，配合退出码 `1` 判断；
- 版本冲突输出 `{"valid": false, "conflict": true, "actual_version": "..."}`，退出码 `2`。

## 相关文档

- [审计与用量统计](AUDIT.md)
- [管理台前端](ADMIN_UI.md)
- [接口契约](../web/API.md)
- [配置指南](CONFIGURATION_GUIDE.md)
