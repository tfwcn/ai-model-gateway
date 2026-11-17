/**
 * 视图：审计明细
 *
 * 数据来源：
 *   POST /admin/audit                  明细下钻（一次上游尝试一行）
 *   POST /admin/audit/chain/{id}       一次请求的完整故障转移链
 *   POST /admin/audit/stats            审计自身健康状态
 * 点击任意一行会展开它所属请求的整条链路。
 */

import { button, control, el, empty, filterPanel, fmtMs, fmtNumber, fmtRate, fmtTime, panel, readControls, select, table, textInput, userLabel } from "./ui.js";
import { lineChart } from "./charts.js";
import { DateRange } from "./date_range.js";
import { Tabs } from "./tabs.js";

const OUTCOME_OPTIONS = [
  { value: "", label: "全部结局" },
  { value: "success", label: "成功" },
  { value: "failure", label: "失败" },
  { value: "aborted", label: "客户端中断（未发完）" },
  { value: "stream_open", label: "流未收尾" },
];

const LABELS = {
  outcome: "结局",
  platform: "平台",
  model: "模型名",
  user: "用户标签",
  gateway_request_id: "请求 ID",
};

// 30 天聚合：口径与 /admin/usage/summary 一致
const GROUP_OPTIONS = [
  { value: "model", label: "按模型" },
  { value: "platform", label: "按平台" },
  { value: "user", label: "按用户" },
  { value: "endpoint", label: "按入口" },
  { value: "selector", label: "按请求的 model" },
  { value: "outcome", label: "按结局" },
  { value: "error", label: "按错误分类" },
  { value: "day", label: "按天" },
];

const SORT_OPTIONS = [
  { value: "requests", label: "请求数" },
  { value: "p95", label: "P95 耗时" },
  { value: "ttft", label: "首块耗时" },
  { value: "throughput", label: "tokens/s" },
  { value: "failover", label: "故障转移次数" },
  { value: "errors", label: "失败次数" },
  { value: "tokens", label: "输出 tokens" },
];

const TREND_BUCKET_OPTIONS = [
  { value: "day", label: "按天" },
  { value: "hour", label: "按小时" },
];

// 明细与聚合各占一个页签：两张长表叠在一屏，谁也看不全
const AUDIT_TABS = [
  { key: "list", label: "审计明细" },
  { key: "aggregate", label: "聚合 / 趋势" },
];

// 结局口径写在页面上，避免把「[DONE] 已发完后客户端关闭」误读成中断
const OUTCOME_NOTE =
  "结局口径：success 含「[DONE] 已转发后客户端才关闭连接」；"
  + "aborted 只统计 [DONE] 之前断开（done_sent=0）；"
  + "stream_open 是未收尾的流，不计入成功率。";

/**
 * 折叠筛选区的一行摘要：日期范围 + 结局 + 有值的平台 / 模型 / 用户 / 请求 ID
 *
 * 收起状态看不到输入框，这行文字就是唯一线索，所以只写已经填了值的条件。
 */
function describeAuditFilters(range, values) {
  const parts = [range.summary()];
  const outcome = OUTCOME_OPTIONS.find((item) => item.value === (values.outcome || ""));
  parts.push((outcome || OUTCOME_OPTIONS[0]).label);
  [["platform", "平台"], ["model", "模型"], ["user", "用户"]].forEach(([name, label]) => {
    const value = String(values[name] || "").trim();
    if (value) parts.push(`${label} ${value}`);
  });
  const requestId = String(values.gateway_request_id || "").trim();
  if (requestId) parts.push(`请求 ${requestId}`);
  return parts.join(" · ");
}

export async function renderAudit(root, ctx) {
  // 明细接口有保留期，默认只看最近 30 天；清空日期即可查全部已保留记录
  const range = new DateRange({ days: 30 });
  const filters = [
    { name: "outcome", node: select(OUTCOME_OPTIONS, "") },
    { name: "platform", node: textInput("", "留空表示不限") },
    { name: "model", node: textInput("", "留空表示不限") },
    { name: "user", node: textInput("", "留空表示不限") },
    { name: "gateway_request_id", node: textInput("", "精确匹配一次请求") },
  ];
  const page = { limit: 50, offset: 0 };
  const loadButton = button("查询", "primary");
  const prev = button("上一页");
  const next = button("下一页");
  const listBox = el("div");
  const statusBox = el("div");

  // 30 天聚合与趋势：保留期内的明细聚合成表格和曲线
  const aggregateGroupBy = select(GROUP_OPTIONS, "day");
  const aggregateSort = select(SORT_OPTIONS, "requests");
  const trendBucket = select(TREND_BUCKET_OPTIONS, "day");
  const aggregateButton = button("查询聚合", "primary");
  const aggregateBox = el("div");
  const aggregateCanvas = el("canvas", { class: "chart" });

  const pageInfo = el("span", { class: "muted" });
  const paging = el("div", { class: "row-actions" }, [
    prev,
    next,
    pageInfo,
    el("span", { class: "muted", text: "点击任意行展开 / 收起整条链路" }),
  ]);
  prev.disabled = true;
  next.disabled = true;

  // 折叠摘要：收起筛选区时，这行字是「当前筛了什么」的唯一线索
  const filterMeta = el("span", { class: "panel-meta" });
  // 最近一次聚合曲线数据：页签隐藏时画不出来，切回来要重画
  let latestTrend = null;
  // 当前展开的链路行：同一时刻只保留一条
  let openChainRow = null;

  const listPane = el("div", {}, [
    panel("审计明细", [listBox], paging),
  ]);
  const aggregatePane = el("div", {}, [
    panel("聚合 / 趋势（审计明细范围内）", [
      el("div", { class: "filters compact" }, [
        control("聚合维度", aggregateGroupBy),
        control("排序", aggregateSort),
        control("曲线粒度", trendBucket),
      ]),
      el("p", { class: "muted", text: "这里的 P95 是明细精确值；「用量与性能」页的维度汇总是直方图估算值。沿用上方日期范围、用户、平台、模型；结局 / 请求 ID 不参与聚合。" }),
      aggregateCanvas,
      aggregateBox,
    ], aggregateButton),
  ]);

  const tabs = new Tabs(AUDIT_TABS, { list: listPane, aggregate: aggregatePane }, {
    onSelect: (key) => {
      // 页签写回地址栏：刷新和分享都能回到同一处
      ctx.setTab?.(key);
      // 曲线在隐藏容器里量不到宽度，切过来再画一次
      window.requestAnimationFrame(drawAggregateTrendIfVisible);
    },
  });
  ctx.registerTabs?.((key) => tabs.select(key));

  root.append(
    filterPanel("筛选条件", [
      el("div", { class: "filters compact" }, [
        ...range.controls(),
        ...filters.map((item) => control(LABELS[item.name], item.node)),
      ]),
      el("p", { class: "muted", text: "日期留空表示不限，结束日期当天包含在内。" }),
      el("p", { class: "muted", text: OUTCOME_NOTE }),
      el("p", { class: "muted", text: "明细默认保留 30 天（AUDIT_RETENTION_DAYS），更早记录会自动清理；更长时间范围的全量用量在「用量与性能」页查看。" }),
    ], { meta: filterMeta, actions: loadButton }),
    statusBox,
    tabs.node,
    listPane,
    aggregatePane,
  );
  // 地址栏指定了页签就停在那一页，否则默认看明细
  tabs.select(ctx.initialTab === "aggregate" ? "aggregate" : "list");

  // 窗口尺寸变了，canvas 的位图尺寸也要跟着变，否则会被拉伸发虚
  const handleResize = () => window.requestAnimationFrame(drawAggregateTrendIfVisible);
  window.addEventListener("resize", handleResize);
  ctx.onTeardown?.(() => window.removeEventListener("resize", handleResize));

  // 条件一变就刷新摘要，展开改完再收起来也能对得上
  [range.sinceNode, range.untilNode, ...filters.map((item) => item.node)].forEach((node) => {
    node.addEventListener("input", syncFilterMeta);
    node.addEventListener("change", syncFilterMeta);
  });
  syncFilterMeta();

  function syncFilterMeta() {
    // 只写已经生效的条件，空着的项略过，摘要才不至于一行全是「不限」
    filterMeta.textContent = describeAuditFilters(range, readControls(filters));
    filterMeta.title = filterMeta.textContent;
  }

  function runQuery() {
    page.offset = 0;
    load();
    loadAggregate(true);
  }

  loadButton.addEventListener("click", runQuery);
  // 筛选框里按 Enter 直接查，不用再去点按钮
  filters.forEach(({ node }) => {
    node.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.isComposing) return;
      event.preventDefault();
      runQuery();
    });
  });
  prev.addEventListener("click", () => { page.offset = Math.max(0, page.offset - page.limit); load(); });
  next.addEventListener("click", () => { page.offset += page.limit; load(); });
  aggregateButton.addEventListener("click", () => loadAggregate());

  /** 页码与上下页状态：第一页禁用「上一页」，最后一页禁用「下一页」 */
  function updatePaging(result) {
    const items = result.items || [];
    const total = Number(result.total) || 0;
    const from = items.length ? page.offset + 1 : 0;
    const to = page.offset + items.length;
    pageInfo.textContent = `第 ${fmtNumber(from)}-${fmtNumber(to)} 条 / 共 ${fmtNumber(total)} 条`;
    prev.disabled = page.offset <= 0;
    next.disabled = !items.length || to >= total;
  }

  async function load() {
    const params = { ...readControls(filters), ...range.params() };
    loadButton.disabled = true;
    try {
      const [result, health] = await Promise.all([
        ctx.api.postQuery("/admin/audit", { ...params, limit: page.limit, offset: page.offset }),
        ctx.api.postQuery("/admin/audit/stats"),
      ]);
      renderHealth(statusBox, health);
      // 新一页表格重建了，之前展开的链路行已随之移除
      openChainRow = null;
      renderList(listBox, result, toggleChain);
      updatePaging(result);
      ctx.notice(`审计共 ${fmtNumber(health.store.rows)} 行，当前筛出 ${fmtNumber(result.total)} 行`, "ok");
    } catch (error) {
      ctx.guard(error);
    } finally {
      loadButton.disabled = false;
    }
  }

  async function loadAggregate(quiet = false) {
    const { since, until } = range.params();
    const controls = readControls(filters);
    const user = (controls.user || "").trim();
    const platform = (controls.platform || "").trim();
    const model = (controls.model || "").trim();
    aggregateButton.disabled = true;
    try {
      const [grouped, series] = await Promise.all([
        ctx.api.postQuery("/admin/usage/summary", {
          since,
          until,
          platform,
          model,
          user,
          group_by: aggregateGroupBy.value,
          limit: 60,
          sort: aggregateSort.value,
        }),
        ctx.api.postQuery("/admin/usage/timeseries", {
          since, until, platform, model, user, bucket: trendBucket.value,
        }),
      ]);
      renderAggregate(aggregateBox, grouped);
      latestTrend = series;
      drawAggregateTrendIfVisible();
      if (!quiet) ctx.notice("聚合与趋势已刷新", "ok");
    } catch (error) {
      ctx.guard(error);
    } finally {
      aggregateButton.disabled = false;
    }
  }

  function drawAggregateTrendIfVisible() {
    if (!aggregatePane.hidden && latestTrend) drawAggregateTrend(aggregateCanvas, latestTrend);
  }

  /** 收起当前展开的链路行 */
  function collapseChain() {
    if (!openChainRow) return;
    openChainRow.node.remove();
    openChainRow.row.classList.remove("expanded");
    openChainRow = null;
  }

  /**
   * 在点击的行下面就地展开整条故障转移链
   *
   * 同一时刻只展开一条。链路内容长在表格自己的滚动区域里，
   * 页面不会因为链路块再多出一条滚动条。
   */
  async function toggleChain(row, gatewayRequestId, colspan) {
    if (openChainRow && openChainRow.row === row) {
      collapseChain();
      return;
    }
    collapseChain();

    const cell = el("td", { class: "chain-cell", colspan });
    const line = el("tr", { class: "chain-row" }, [cell]);
    cell.append(el("p", { class: "muted", text: "加载中…" }));
    row.after(line);
    row.classList.add("expanded");
    openChainRow = { row, node: line };

    try {
      const chain = await ctx.api.postQuery(`/admin/audit/chain/${encodeURIComponent(gatewayRequestId)}`);
      renderChain(cell, chain);
    } catch (error) {
      cell.replaceChildren(el("p", { class: "issue error", text: "链路读取失败" }));
      ctx.guard(error);
    }
  }

  await Promise.all([load(), loadAggregate(true)]);
}

// ---------- 呈现 ----------

function renderHealth(box, health) {
  const store = health.store || {};
  const collector = health.collector || {};
  box.replaceChildren(el("div", { class: "row-actions" }, [
    chip("审计", health.config.enabled),
    el("span", { class: "chip", text: `库行数 ${fmtNumber(store.rows)}` }),
    el("span", { class: "chip", text: `缓冲待写 ${fmtNumber(collector.pending_rows)}` }),
    el("span", { class: "chip", text: `丢弃 ${fmtNumber(collector.dropped_total)}` }),
    el("span", { class: "chip", text: `写库失败 ${fmtNumber(collector.write_errors_total)}` }),
    el("span", { class: "chip", text: `保留 ${health.config.retention_days} 天` }),
    el("span", { class: "chip", text: `最近刷盘 ${fmtTime(collector.last_flush_at)}` }),
  ]));
}

function drawAggregateTrend(canvas, series) {
  const points = series.points || [];
  const labels = points.map((point) => point.bucket.slice(-5));

  lineChart(canvas, labels, [
    { label: "请求数", values: points.map((point) => point.requests) },
    { label: "P95 耗时(ms)", values: points.map((point) => point.p95_duration_ms) },
    { label: "首块耗时(ms)", values: points.map((point) => point.avg_ttft_ms) },
  ]);
}

function renderAggregate(box, grouped) {
  if (!grouped.items.length) {
    box.replaceChildren(empty("这个时间范围内没有审计记录"));
    return;
  }
  box.replaceChildren(table([
    { key: "bucket", label: "分组", render: (row) => chips(row) },
    { key: "requests", label: "请求", num: true },
    { key: "attempts", label: "尝试", num: true },
    { key: "success_rate", label: "成功率", num: true, render: (row) => fmtRate(row.success_rate) },
    { key: "failover_count", label: "切换", num: true },
    { key: "input_tokens", label: "输入 tokens", num: true },
    { key: "cached_tokens", label: "缓存 tokens", num: true },
    { key: "output_tokens", label: "输出 tokens", num: true },
    { key: "avg_ttft_ms", label: "首块", num: true, render: (row) => fmtMs(row.avg_ttft_ms) },
    { key: "p95_duration_ms", label: "P95", num: true, render: (row) => fmtMs(row.p95_duration_ms) },
    { key: "avg_duration_ms", label: "平均耗时", num: true, render: (row) => fmtMs(row.avg_duration_ms) },
    { key: "avg_throughput", label: "tokens/s", num: true, render: (row) => fmtNumber(row.avg_throughput, 1) },
  ], grouped.items));
}

function chips(row) {
  const nodes = [el("span", { text: row.bucket })];
  if (row.disabled_models) {
    nodes.push(el("span", { class: "chip warn", text: `禁用 ${row.disabled_models}` }));
  }
  return el("span", {}, nodes);
}

function renderList(box, result, onExpand) {
  if (!result.items.length) {
    box.replaceChildren(empty("没有符合条件的审计行"));
    return;
  }
  const columns = [
    { key: "ts", label: "时间", render: (row) => fmtTime(row.ts) },
    { key: "gateway_request_id", label: "请求" },
    { key: "api_user", label: "用户", render: (row) => userLabel(row.api_user) },
    { key: "attempt_seq", label: "第几次", num: true },
    { key: "model_name", label: "模型" },
    { key: "outcome", label: "结局", render: outcomeChip },
    { key: "done_sent", label: "[DONE]", num: true, render: doneChip },
    { key: "duration_ms", label: "耗时", num: true, render: (row) => fmtMs(row.duration_ms) },
    { key: "ttft_ms", label: "首块", num: true, render: (row) => fmtMs(row.ttft_ms) },
    { key: "http_status", label: "状态码", num: true },
    { key: "error_category", label: "错误分类" },
    { key: "error_code", label: "错误码" },
  ];
  box.replaceChildren(table(columns, result.items, {
    // colspan 用整张表的列数：展开行要横向铺满
    onRow: (row, tr) => onExpand(tr, row.gateway_request_id, columns.length),
  }));
}

function renderChain(target, chain) {
  const attempts = chain.attempts || [];
  if (!attempts.length) {
    target.replaceChildren(el("p", { class: "muted", text: `请求 ${chain.gateway_request_id} 没有审计记录` }));
    return;
  }

  const rows = attempts.map((attempt) => el("div", { class: "detail" }, [
    el("h4", { text: `#${attempt.attempt_seq} ${attempt.model_name} ${outcomeLabel(attempt.outcome)}` }),
    el("dl", { class: "kv" }, [
      dt("用户"), dd(userLabel(attempt.api_user)),
      dt("耗时"), dd(fmtMs(attempt.duration_ms)),
      dt("首块"), dd(fmtMs(attempt.ttft_ms)),
      dt("探测"), dd(fmtMs(attempt.probe_ms)),
      dt("上游 ID"), dd(attempt.upstream_id || "-"),
      dt("状态码"), dd(attempt.http_status ?? "-"),
      dt("错误"), dd(`${attempt.error_category || "-"} / ${attempt.error_code || "-"}`),
      dt("禁用模型"), dd(attempt.disabled_model ? "是" : "否"),
      dt("[DONE] 已转发"), dd(doneLabel(attempt)),
      dt("tokens"), dd(`${attempt.input_tokens ?? "-"} → ${attempt.output_tokens ?? "-"}`),
      dt("tokens/s"), dd(attempt.throughput ?? "-"),
    ]),
  ]));

  target.replaceChildren(el("div", { class: "chain-inline" }, [
    el("h4", { class: "chain-title", text: `故障转移链：${chain.gateway_request_id}（${attempts.length} 次尝试）` }),
    ...rows,
  ]));
}

function outcomeChip(row) {
  const tones = { success: "ok", failure: "bad", aborted: "warn", stream_open: "" };
  return el("span", { class: `chip ${tones[row.outcome] || ""}` }, outcomeLabel(row.outcome));
}

function outcomeLabel(outcome) {
  return { success: "成功", failure: "失败", aborted: "客户端中断", stream_open: "流未收尾" }[outcome] || outcome;
}

/** [DONE] 是否已转发：非流式调用没有这个概念，不下结论 */
function doneLabel(row) {
  if (!row.stream) return "-";
  return row.done_sent ? "已发完" : "未发完";
}

function doneChip(row) {
  if (!row.stream) return el("span", { class: "chip", text: "-" });
  const sent = Boolean(row.done_sent);
  return el("span", { class: `chip ${sent ? "ok" : "warn"}` }, sent ? "已发完" : "未发完");
}

function dt(text) { return el("dt", { text }); }
function dd(value) { return el("dd", { text: String(value) }); }
function chip(label, enabled) {
  return el("span", { class: `chip ${enabled ? "ok" : "bad"}` }, `${label}：${enabled ? "开" : "关"}`);
}
