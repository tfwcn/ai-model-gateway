/**
 * 视图：用量与性能
 *
 * 数据来源：
 *   POST /admin/usage/daily       全量日用量（日期 × 用户，永久保留，含输入/缓存/输出 tokens）
 *   POST /admin/usage/summary     聚合（按模型/平台/入口等）
 *   POST /admin/usage/timeseries  按小时/天分桶的曲线
 *   POST /admin/usage/models      模型横向对比榜
 * 审计明细只保留 30 天；日汇总只累加、不清理，因此历史用量不会丢。
 * 成功率只统计已收尾的尝试（finalized=1），未收尾的 stream_open 不算分母。
 */

import { button, cards, control, el, empty, fmtMs, fmtNumber, fmtRate, panel, readControls, select, table, textInput, userLabel } from "./ui.js";
import { barChart, lineChart } from "./charts.js";

const SINCE_OPTIONS = [
  { value: "7d", label: "最近 7 天" },
  { value: "30d", label: "最近 30 天" },
  { value: "90d", label: "最近 90 天" },
  { value: "", label: "全部（保留所有记录）" },
];

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

export async function renderUsage(root, ctx) {
  const state = { since: "", groupBy: "day", sort: "requests", bucket: "day" };

  const filters = [
    { name: "since", label: "时间范围", node: select(SINCE_OPTIONS, state.since) },
    { name: "groupBy", label: "聚合维度", node: select(GROUP_OPTIONS, state.groupBy) },
    { name: "sort", label: "排序", node: select(SORT_OPTIONS, state.sort) },
    { name: "bucket", label: "曲线粒度", node: select([{ value: "day", label: "按天" }, { value: "hour", label: "按小时" }], state.bucket) },
    { name: "platform", label: "平台", node: textInput("", "留空表示不限") },
    { name: "model", label: "模型名", node: textInput("", "留空表示不限") },
    { name: "user", label: "用户标签", node: textInput("", "留空表示不限") },
  ];
  const loadButton = button("查询", "primary");
  const kpiBox = el("div");
  const dailyBox = el("div");
  const chartCanvas = el("canvas", { class: "chart" });
  const slowCanvas = el("canvas", { class: "chart" });
  const aggregateBox = el("div");
  const rankingBox = el("div");
  const dimensionBox = el("div");

  const dimGroup = textInput("model", "例如 model,size");
  const dimEndpoint = textInput("", "例如 passthrough:example-service");
  const dimUser = textInput("", "留空表示不限");
  const dimButton = button("查询维度", "primary");
  const dimensionFilter = panel("透传维度筛选", [
    el("div", { class: "filters" }, [
      control("维度组合", dimGroup),
      control("入口", dimEndpoint),
      control("用户标签", dimUser),
    ]),
  ], dimButton);

  const filterPanel = panel("筛选", [el("div", { class: "filters" }, filters.map((item) => control(item.label, item.node)))], loadButton);

  root.append(
    filterPanel,
    kpiBox,
    panel("每日用量（全量，按天 / 用户，不随审计清理）", [dailyBox]),
    panel("趋势（请求数 / P95 耗时）", [chartCanvas]),
    panel("模型榜（按所选维度排序）", [slowCanvas, rankingBox]),
    panel("透传维度汇总（永久日汇总）", [dimensionFilter, dimensionBox]),
    panel("聚合明细（审计明细范围内）", [aggregateBox]),
  );

  loadButton.addEventListener("click", () => load(readControls(filters)));
  dimButton.addEventListener("click", () => loadDimensions());

  async function load(params) {
    Object.assign(state, params);
    const query = {
      since: params.since,
      platform: params.platform,
      model: params.model,
      user: params.user,
    };
    loadButton.disabled = true;
    try {
      const [totals, daily, series, grouped, ranking] = await Promise.all([
        ctx.api.postQuery("/admin/usage/summary", { ...query, group_by: "outcome", limit: 20 }),
        ctx.api.postQuery("/admin/usage/daily", { since: params.since, user: params.user, limit: 2000 }),
        ctx.api.postQuery("/admin/usage/timeseries", { ...query, bucket: params.bucket || state.bucket }),
        ctx.api.postQuery("/admin/usage/summary", { ...query, group_by: params.groupBy || state.groupBy, limit: 60, sort: params.sort || state.sort }),
        ctx.api.postQuery("/admin/usage/models", { ...query, sort: params.sort || state.sort, limit: 10 }),
      ]);

      renderCards(kpiBox, totals, daily.totals);
      renderDaily(dailyBox, daily);
      drawCharts(chartCanvas, slowCanvas, series, ranking);
      renderAggregate(aggregateBox, grouped);
      renderRanking(rankingBox, ranking);
      ctx.notice(`已载入 ${daily.range.since || "全部历史"} 至 ${daily.range.until} 的全量用量（${daily.count} 行日汇总）`, "ok");
    } catch (error) {
      ctx.guard(error);
    } finally {
      loadButton.disabled = false;
    }
  }

  async function loadDimensions() {
    dimButton.disabled = true;
    try {
      const payload = await ctx.api.postQuery("/admin/usage/dimensions", {
        group_by: dimGroup.value.trim() || "model",
        since: state.since,
        endpoint: dimEndpoint.value.trim(),
        user: dimUser.value.trim(),
        limit: 500,
      });
      renderDimensions(dimensionBox, payload);
      ctx.notice(`维度汇总：${payload.count} 组（${payload.group_by.join(" × ")}）`, "ok");
    } catch (error) {
      ctx.guard(error);
    } finally {
      dimButton.disabled = false;
    }
  }

  await load({ since: state.since, groupBy: state.groupBy, sort: state.sort, bucket: state.bucket });
  await loadDimensions();
}

// ---------- 呈现 ----------

function renderCards(box, totals, dailyTotals = {}) {
  const sums = totals.items.reduce((acc, item) => {
    Object.keys(acc).forEach((key) => { acc[key] += Number(item[key] || 0); });
    return acc;
  }, { attempts: 0, requests: 0, finalized: 0, success: 0, failure: 0, aborted: 0, retries: 0 });

  const rate = sums.finalized ? sums.success / sums.finalized : null;
  box.replaceChildren(cards([
    { label: "请求数（全量）", value: fmtNumber(dailyTotals.requests) },
    { label: "输入 tokens（全量）", value: fmtNumber(dailyTotals.input_tokens) },
    { label: "输出 tokens（全量）", value: fmtNumber(dailyTotals.output_tokens) },
    { label: "缓存 tokens（全量）", value: fmtNumber(dailyTotals.cached_tokens) },
    { label: "成功率（明细范围）", value: rate === null ? "-" : fmtRate(rate), tone: rate !== null && rate < 0.9 ? "bad" : "ok" },
    { label: "失败尝试（明细范围）", value: fmtNumber(sums.failure), tone: sums.failure ? "bad" : "" },
    { label: "客户端中断（明细范围）", value: fmtNumber(sums.aborted) },
    { label: "重试尝试（明细范围）", value: fmtNumber(sums.retries) },
  ]));
}

function drawCharts(mainCanvas, rankingCanvas, series, ranking) {
  const points = series.points || [];
  const labels = points.map((point) => point.bucket.slice(-5));

  lineChart(mainCanvas, labels, [
    { label: "请求数", values: points.map((point) => point.requests) },
    { label: "P95 耗时(ms)", values: points.map((point) => point.p95_duration_ms) },
    { label: "首块耗时(ms)", values: points.map((point) => point.avg_ttft_ms) },
  ]);

  const rows = (ranking.items || []).slice(0, 10);
  barChart(rankingCanvas, rows.map((item) => shortBucket(item.bucket)), rows.map((item) => item.p95_duration_ms || 0));
}

function renderDaily(box, daily) {
  const items = daily.items || [];
  if (!items.length) {
    box.replaceChildren(empty("还没有日汇总：产生新的调用后会自动累计，且不会随审计明细清理而丢失"));
    return;
  }
  // 接口按日期升序返回，表格反过来先看最近几天
  const rows = [...items].reverse();
  box.replaceChildren(
    el("p", { class: "muted", text: "admin = 管理密钥流量；未启用鉴权或历史数据也统一归到 admin。" }),
    el("p", { class: "muted", text: `共 ${rows.length} 行（日期 × 用户），全量保留` }),
    table([
      { key: "day", label: "日期" },
      { key: "api_user", label: "用户", render: (row) => userLabel(row.api_user) },
      { key: "requests", label: "请求", num: true },
      { key: "attempts", label: "尝试", num: true },
      { key: "input_tokens", label: "输入 tokens", num: true },
      { key: "cached_tokens", label: "缓存 tokens", num: true },
      { key: "output_tokens", label: "输出 tokens", num: true },
      { key: "success", label: "成功", num: true },
      { key: "failure", label: "失败", num: true },
    ], rows),
  );
}

function renderDimensions(box, payload) {
  const items = payload.items || [];
  if (!items.length) {
    box.replaceChildren(empty("这个范围内还没有维度汇总：产生透传调用后会自动累计，且不随审计明细清理"));
    return;
  }
  const dims = payload.group_by || [];
  box.replaceChildren(
    el("p", { class: "muted", text: `按 ${dims.join(" × ")} 汇总；数值来自永久日汇总表` }),
    table([
      ...dims.map((name) => ({ key: name, label: name, render: (row) => (row.values || {})[name] })),
      { key: "requests", label: "请求", num: true },
      { key: "attempts", label: "尝试", num: true },
      { key: "success", label: "成功", num: true },
      { key: "failure", label: "失败", num: true },
      { key: "aborted", label: "中断", num: true },
      { key: "success_rate", label: "成功率", num: true, render: (row) => fmtRate(row.success_rate) },
    ], items),
  );
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

function renderRanking(box, ranking) {
  if (!ranking.items.length) {
    box.replaceChildren(empty("没有模型记录"));
    return;
  }
  box.replaceChildren(table([
    { key: "bucket", label: "模型" },
    { key: "requests", label: "请求", num: true },
    { key: "success_rate", label: "成功率", num: true, render: (row) => fmtRate(row.success_rate) },
    { key: "failover_count", label: "切换", num: true },
    { key: "p95_duration_ms", label: "P95", num: true, render: (row) => fmtMs(row.p95_duration_ms) },
    { key: "avg_ttft_ms", label: "首块", num: true, render: (row) => fmtMs(row.avg_ttft_ms) },
  ], ranking.items));
}

function chips(row) {
  const nodes = [el("span", { text: row.bucket })];
  if (row.disabled_models) {
    nodes.push(el("span", { class: "chip warn", text: `禁用 ${row.disabled_models}` }));
  }
  return el("span", {}, nodes);
}

function shortBucket(bucket) {
  const text = String(bucket);
  return text.length > 14 ? text.slice(-14) : text;
}
