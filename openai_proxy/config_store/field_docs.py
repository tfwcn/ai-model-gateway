"""
可编辑字段的字典 - 让管理台与 CLI 能直接把「这个字段是干什么的」显示出来

存在理由：字段名（timeout / weight / first_chunk_timeout ...）本身不解释行为，
只看配置很容易改错。这里的每条说明都对应代码里的实际语义，改代码语义时要同步改这里。

约定：
- 一律用通用占位符（example-platform / model-a / https://api.example.com/v1 / EXAMPLE_PLATFORM_API_KEY）
- default 写「不配置时发生什么」，包括回落到哪个环境变量
- effect 只有两种：热重载（进程内立即生效）/ 重启（进程内改不干净）
"""
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Tuple

RELOAD = "热重载"
RESTART = "重启"


@dataclass(frozen=True)
class FieldDoc:
    """一个字段的说得清的说明"""

    label: str       # 中文名，界面直接显示
    summary: str     # 它到底控制什么
    unit: str        # 取值单位或取值范围写法
    default: str     # 不配置时的行为（含回落来源）
    example: str     # models.yaml 里的写法示例
    effect: str      # RELOAD | RESTART
    note: str = ""             # 边界与容易踩的坑
    values: Tuple[str, ...] = ()  # 取值只有有限几种时列出：界面给下拉，不让人猜写法
    allow_none: bool = False      # 这个字段可以「不配置」（界面上是一个回落选项）


FIELD_DOCS: Dict[str, FieldDoc] = {
    # ---------- 时间与重试预算 ----------
    "timeout": FieldDoc(
        "总超时",
        "一次上游调用的时间预算。非流式是整个响应的读预算；流式是相邻两次读到数据之间的最大间隔，"
        "所以长回答本身不会被它掐断。它同时是连接超时、首块预算和探测窗口的上限。",
        "秒（整数）", "30 秒", "timeout: 120", RELOAD,
        "超时后换下一个模型，不会把这个模型拉黑。",
    ),
    "connect_timeout": FieldDoc(
        "连接超时",
        "只覆盖建立连接这一段（DNS + TCP + TLS），避免连接卡死把整个总超时吃光。",
        "秒（可小数）", "不写则用全局 5 秒（环境变量 STREAM_CONNECT_TIMEOUT）",
        "connect_timeout: 5", RELOAD,
        "实际取值是 min(这里写的值, 该模型的 timeout)；删掉这个字段就是回落到全局值。",
    ),
    "first_chunk_timeout": FieldDoc(
        "首块预算",
        "流式专用：从发出请求那一刻起，到「拿到响应头 + 首个有效内容」为止的总等待上限。",
        "秒（可小数）", "不写则用全局 20 秒（环境变量 STREAM_FIRST_CHUNK_TIMEOUT）",
        "first_chunk_timeout: 30", RELOAD,
        "预算耗尽时不重试同一个模型、也不拉黑模型，直接换下一个；非流式请求不使用它；"
        "它被 timeout 夹住，且读首块另有一个不可配置的 5 秒硬上限。",
    ),
    "stream_probe_timeout": FieldDoc(
        "内容探测窗口",
        "流式专用：已经拿到响应头之后，允许上游只发心跳/空行多久而不被判为「空响应」。",
        "秒（可小数）", "不写则用全局 10 秒（环境变量 STREAM_PROBE_TIMEOUT）",
        "stream_probe_timeout: 10", RELOAD,
        "与首块预算共用同一个起点，取 min(这里写的值, timeout, 首块预算剩余)；"
        "窗口内只有心跳会触发换模型并可能拉黑该模型。",
    ),
    "retry_count": FieldDoc(
        "单模型重试次数",
        "只在请求的 model 写成「平台名|模型名」时生效。写 all 或只写平台名时，每个模型只尝试一次，"
        "容错靠换模型，不靠重试。",
        "次数（整数）", "3 次", "retry_count: 1", RELOAD,
        "只试一次请写 1：写 0 会走到没有兜底的重试分支，非流式请求可能变成 500。"
        "重试是否发生还取决于错误类型（限流/5xx 才重试）。",
    ),
    # ---------- 选择与额度 ----------
    "weight": FieldDoc(
        "平台优先级",
        "值越大越先被尝试。平台之间的排序键是 (-weight, 文件里平台的书写顺序)。",
        "无量纲数值", "1", "weight: 10", RELOAD,
        "只有「该平台第一个模型」的 weight 会被采用；同一个平台内部不按权重排序，"
        "按 models 列表顺序逐个尝试。所以模型级 weight 基本只影响这一个条目。",
    ),
    "enabled": FieldDoc(
        "是否参与转发",
        "关掉后这个模型不进入候选池；整平台都被关掉时报「平台无可用模型」，并继续尝试下一个平台。",
        "true/false", "true", "enabled: false", RELOAD,
        "关掉的模型仍会出现在 GET /v1/models 里（只是 metadata 标记），别指望它从列表消失。",
    ),
    "quota_period": FieldDoc(
        "失败后拉黑多久",
        "决定模型被判失败后是否被周期性拉黑：不配置就只在本次请求的剩余尝试里避开它。",
        "hourly / daily / weekly / monthly", "不配置（不做周期拉黑）", "quota_period: hourly", RELOAD,
        "到期时间：hourly 下个整点、daily 次日 0 点、weekly 下周一 0 点、monthly 下月 1 号 0 点，"
        "其它写法按 daily 处理。拉黑状态存在进程内存：重启即清空，热重载也保留；"
        "正被拉黑的模型不会因为改这个字段而解禁。",
        values=("hourly", "daily", "weekly", "monthly"), allow_none=True,
    ),
    "enable_tool_call_conversion": FieldDoc(
        "工具调用格式改写",
        "是否把上游的非标准工具调用改写成标准 tool_calls 结构。",
        "true/false", "true", "enable_tool_call_conversion: false", RELOAD,
        "只有写在模型条目的覆盖里才生效（写在平台级当前不会被读取）；"
        "当前只有非流式响应会真正改写，流式没有可观察的差异。",
    ),
    # ---------- 上游与模型清单 ----------
    "baseUrl": FieldDoc(
        "上游入口",
        "上游聊天端点前缀：调用时拼成 <baseUrl>/chat/completions，所以一般写到 /v1 这一层。",
        "URL 字符串", "必填；缺失时整个平台在启动时被跳过",
        'baseUrl: "https://api.example.com/v1"', RELOAD,
        "这里不做 ${VAR} 环境变量展开（只有密钥做）；爬虫平台的插件不使用它。",
    ),
    "apiKey": FieldDoc(
        "密钥引用",
        "上游请求的 Bearer 密钥来源。",
        "字符串或 ${ENV_VAR} 占位符", "非爬虫模式必填；爬虫模式可省略",
        'apiKey: "${EXAMPLE_PLATFORM_API_KEY}"', RELOAD,
        "优先级：环境变量 {平台名大写}_API_KEY 高于 yaml 里的值。yaml 写的 ${VAR} 如果那个变量没设置，"
        "会把字面量 ${VAR} 当密钥发出去（上游返回 401，进而可能拉黑该模型）。"
        "管理台和接口永远不返回密钥内容，只告诉你引用了哪个环境变量、它是否已设置。",
    ),
    "models": FieldDoc(
        "静态模型条目",
        "字符串写法（- \"model-a\"）只声明模型，全部参数继承平台级；对象写法（- name: model-b）可以带覆盖。",
        "字符串 / 对象混合的列表", "空列表", '- "model-a"', RELOAD,
        "有插件时插件灌入的条目在前、静态条目在后（同名时静态覆盖优先），所以静态条目在平台内故障转移更靠后。"
        "爬虫平台的条目不能删除，只能用 blacklist 屏蔽。",
    ),
    "blacklist": FieldDoc(
        "屏蔽模型",
        "按通配符模式剔除模型（匹配的是原始模型名，不含平台前缀），可用于屏蔽一整批前缀相同的模型。",
        "字符串列表（界面只能勾列出来的模型条目）", "空列表", 'blacklist: ["prefix-*"]', RELOAD,
        "这是屏蔽爬虫灌入模型的唯一手段；被屏蔽的模型连 GET /v1/models 都不再出现。"
        "通配写法（例如 prefix-*）在界面上挑不出来：下拉只列真实条目，那种模式要手改 models.yaml。",
    ),
    "plugin": FieldDoc(
        "插件（模型来源）",
        "code 是可导入的模块路径，args 是插件自定义参数袋；插件负责把模型灌进这个平台。",
        "对象（code / cache_timeout / args）", "不配置即为纯静态平台",
        "plugin:\n    code: \"plugin.example_plugin\"\n    cache_timeout: 300\n    args:\n      page_size: 50", RESTART,
        "cache_timeout 写在 plugin 顶层（和 code 同级），不配置即 300 秒；写在 args 里不会被读到。"
        "插件的密钥来自环境变量 {插件模块名大写}_API_KEY，不读平台的 apiKey。插件实例按模块名复用，"
        "改 args 不重启不会喂给已存在的实例。args.scrape_url 存在即爬虫模式：不要求 apiKey，且禁止删除模型条目。",
    ),
    # ---------- audit 段（与平台字段同名时要按所属段落取，例如 enabled） ----------
}

AUDIT_DOCS: Dict[str, FieldDoc] = {
    "enabled": FieldDoc(
        "审计总开关",
        "关掉后不建库、不采集，转发路径零开销，用量与审计页面查空库。",
        "true/false", "true", "enabled: true", RESTART,
        "关掉之后即使 prometheus_enabled 为 true，/metrics 也只有零值：打点是在审计行落库时才喂给 Prometheus 的。"
        "审计库打不开时会自动降级为关闭审计，不影响启动。",
    ),
    "db_path": FieldDoc(
        "审计库路径",
        "SQLite 审计库文件位置，一次上游尝试一行。",
        "文件路径", "data/audit.db", "db_path: data/audit.db", RESTART,
        "相对路径按进程的工作目录解析，父目录会自动创建。改路径等于换库：旧数据不会迁移，"
        "历史查询会「看起来消失了」。",
    ),
    "retention_days": FieldDoc(
        "保留天数",
        "定期清理早于「现在 - N 天」的审计明细；全量日用量永久保留。",
        "天（整数）", "30 天", "retention_days: 45", RESTART,
        "启动时清理一次，运行中每 6 小时再检查一次；临时清理用 POST /admin/audit/prune，body 传 before。"
        "下限是 1，配置层面写不出「永不清理」，要长期保留就写一个大天数。",
    ),
    "queue_size": FieldDoc(
        "缓冲行数上限",
        "内存里最多挂多少条还没落库的审计行；超限时丢弃最旧的一条并计数。",
        "行数（整数）", "10000", "queue_size: 20000", RESTART,
        "丢弃计数在 POST /admin/audit/stats 里能看到。采集是同步非阻塞的：审计出问题不会变成转发失败。",
    ),
    "batch_size": FieldDoc(
        "刷盘块大小",
        "单次刷盘把缓冲切成多大一块写进 SQLite。",
        "行数（整数）", "200", "batch_size: 500", RESTART,
        "写库失败时会保留全部缓冲等下一轮重试，并计入写库失败次数。",
    ),
    "flush_interval": FieldDoc(
        "刷盘间隔",
        "后台刷盘任务的间隔，也就是「一条明细最多滞后多久能被查到」。",
        "秒（可小数）", "2.0 秒", "flush_interval: 1", RESTART, "",
    ),
    "record_failed_only": FieldDoc(
        "只记录失败",
        "只把失败的尝试写进审计库，成功的直接跳过并计数。",
        "true/false", "false", "record_failed_only: true", RESTART,
        "省磁盘的代价很大：耗时、首块时间、tokens/s 这些正向指标是随成功的审计行落库时才打点的，"
        "开了以后「用量与性能」页和 /metrics 基本没有正向数据。",
    ),
    "include_request_meta": FieldDoc(
        "记录请求消息条数",
        "是否在审计行里记「这个请求里有几条消息」这个计数。",
        "true/false", "false", "include_request_meta: true", RESTART,
        "任何情况都不记录消息内容，只记条数。",
    ),
    "prometheus_enabled": FieldDoc(
        "Prometheus 打点",
        "是否把落库的审计行同时喂给 Prometheus，并暴露 POST /metrics（GET 保留给 Prometheus 抓取）。",
        "true/false", "true", "prometheus_enabled: true", RESTART,
        "关掉时 /metrics 返回 404 并说明原因。没装 prometheus_client 时打点自动停用，服务照常运行。"
        "/metrics 同样受 GATEWAY_API_KEY 保护。",
    ),
    "metrics_label_model": FieldDoc(
        "指标带模型标签",
        "是否让 Prometheus 的指标多带一个 model 标签（爬虫模式下标签组合会爆炸，默认关掉）。",
        "true/false", "false", "metrics_label_model: false", RESTART,
        "写在 models.yaml 里目前没有运行时效果：标签集在指标注册时就定死了，真正的开关是环境变量 "
        "METRICS_LABEL_MODEL + 重启。改这里只会让文件与运行状态不一致。",
    ),
}

# 平台级与模型级共用同一份语义；audit 段单独一份（enabled 在两边含义完全不同）
_SCOPES = {"platform": FIELD_DOCS, "model": FIELD_DOCS, "audit": AUDIT_DOCS}


def doc_for(field: str, scope: str = "platform") -> Dict[str, Any]:
    """
    取一个字段的说明；没有说明的字段返回空 dict（界面不会显示假文档）

    取值选项转成 list：字典要同时喂给接口 JSON 与命令行 --json，
    tuple 在一边是 ()、在另一边是 []，会让「两边必须同一份」的测试对不上。
    """
    entry = _SCOPES.get(scope, FIELD_DOCS).get(field)
    if not entry:
        return {}
    document = asdict(entry)
    document["values"] = list(document["values"])
    return document


def docs_for(fields: List[str], scope: str = "platform") -> Dict[str, Dict[str, Any]]:
    """给一批字段名取说明，供 /admin/config/schema 一次性返回"""
    result: Dict[str, Dict[str, Any]] = {}
    for field in fields:
        entry = doc_for(field, scope)
        if entry:
            result[field] = entry
    return result


def documented_fields() -> List[str]:
    """所有已写字典的字段名（测试用它对照白名单）"""
    return sorted(set(FIELD_DOCS) | set(AUDIT_DOCS))


def enum_fields() -> Dict[str, Dict[str, Any]]:
    """
    取值只有有限几种的字段 → 候选取值

    界面对这些字段渲染下拉框（而不是让人在输入框里猜写法），说明与选项同一份来源。
    """
    result: Dict[str, Dict[str, Any]] = {}
    for table in (FIELD_DOCS, AUDIT_DOCS):
        for field, doc in table.items():
            if doc.values:
                result[field] = {"values": list(doc.values), "allow_none": doc.allow_none}
    return result


def missing_documentation(fields: List[str]) -> List[str]:
    """哪些字段还没有说明（测试用它保证界面不会出现「能改但没解释」的字段）"""
    return [field for field in fields if field not in FIELD_DOCS and field not in AUDIT_DOCS]
