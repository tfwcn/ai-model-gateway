/**
 * 视图：用量与性能
 *
 * 数据来源：
 *   GET /admin/usage/summary     聚合（按模型/平台/入口等）
 *   GET /admin/usage/timeseries  按小时/天分桶的曲线
 *   GET /admin/usage/models      模型横向对比榜
 * 成功率只统计已收尾的尝试（finalized=1），未收尾的 stream_open 不算分母。
 */

import { button, cards, control, el, empty, fmtMs, fmtNumber, fmtRate, panel, readControls, select, table, textInput } from "./ui.js";
import { barChart, lineChart } from "./charts.js";

const SINCE_OPTIONS = [
  { value: "24h", label: "最近 24 小时" },
  { value: "7d", label: "最近 7 天" },
  { value: "30d", label: "最近 30 天" },
  { value: "90d", label: "最近 90 天" },
  { value: "", label: "全部" },
];

const GROUP_OPTIONS = [
  { value: "model", label: "按模型" },
  { value: "platform", label: "按平台" },
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
  const state = { since: "24h", groupBy: "model", sort: "requests", bucket: "hour" };

  const filters = [
    { name: "since", label: "时间范围", node: select(SINCE_OPTIONS, state.since) },
    { name: "groupBy", label: "聚合维度", node: select(GROUP_OPTIONS, state.groupBy) },
    { name: "sort", label: "排序", node: select(SORT_OPTIONS, state.sort) },
    { name: "bucket", label: "曲线粒度", node: select([{ value: "hour", label: "按小时" }, { value: "day", label: "按天" }], state.bucket) },
    { name: "platform", label: "平台", node: textInput("", "留空表示不限") },
    { name: "model", label: "模型名", node: textInput("", "留空表示不限") },
  ];
  const loadButton = button("查询", "primary");
  const kpiBox = el("div");
  const chartCanvas = el("canvas", { class: "chart" });
  const slowCanvas = el("canvas", { class: "chart" });
  const aggregateBox = el("div");
  const rankingBox = el("div");

  const filterPanel = panel("筛选", [el("div", { class: "filters" }, filters.map((item) => control(item.name === "since" ? "时间范围" : item.name, item.node)))], loadButton);

  root.append(
    filterPanel,
    kpiBox,
    panel("趋势（请求数 / P95 耗时）", [chartCanvas]),
    panel("模型榜（按所选维度排序）", [slowCanvas, rankingBox]),
    panel("聚合明细", [aggregateBox]),
  );

  loadButton.addEventListener("click", () => load(readControls(filters)));

  async function load(params) {
    Object.assign(state, params);
    const query = {
      since: params.since,
      platform: params.platform,
      model: params.model,
    };
    loadButton.disabled = true;
    try {
      const [totals, series, grouped, ranking] = await Promise.all([
        ctx.api.get("/admin/usage/summary", { ...query, group_by: "outcome", limit: 20 }),
        ctx.api.get("/admin/usage/timeseries", { ...query, bucket: params.bucket || state.bucket }),
        ctx.api.get("/admin/usage/summary", { ...query, group_by: params.groupBy || state.groupBy, limit: 60, sort: params.sort || state.sort }),
        ctx.api.get("/admin/usage/models", { ...query, sort: params.sort || state.sort, limit: 10 }),
      ]);

      renderCards(kpiBox, totals);
      drawCharts(chartCanvas, slowCanvas, series, ranking);
      renderAggregate(aggregateBox, grouped);
      renderRanking(rankingBox, ranking);
      ctx.notice(`已载入 ${totals.range.since} 至 ${totals.range.until} 的数据（共 ${totals.count} 组）`, "ok");
    } catch (error) {
      ctx.guard(error);
    } finally {
      loadButton.disabled = false;
    }
  }

  await load({ since: state.since, groupBy: state.groupBy, sort: state.sort, bucket: state.bucket });
}

// ---------- 呈现 ----------

function renderCards(box, totals) {
  const sums = totals.items.reduce((acc, item) => {
    Object.keys(acc).forEach((key) => { acc[key] += Number(item[key] || 0); });
    return acc;
  }, { attempts: 0, requests: 0, finalized: 0, success: 0, failure: 0, aborted: 0, retries: 0, output_tokens: 0 });

  const rate = sums.finalized ? sums.success / sums.finalized : null;
  box.replaceChildren(cards([
    { label: "请求数", value: fmtNumber(sums.requests) },
    { label: "上游尝试数", value: fmtNumber(sums.attempts) },
    { label: "成功率（已收尾）", value: rate === null ? "-" : fmtRate(rate), tone: rate !== null && rate < 0.9 ? "bad" : "ok" },
    { label: "失败尝试", value: fmtNumber(sums.failure), tone: sums.failure ? "bad" : "" },
    { label: "客户端中断", value: fmtNumber(sums.aborted) },
    { label: "重试尝试", value: fmtNumber(sums.retries) },
    { label: "输出 tokens", value: fmtNumber(sums.output_tokens) },
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
    { key: "avg_ttft_ms", label: "首块", num: true, render: (row) => fmtMs(row.avg_ttft_ms) },
    { key: "p95_duration_ms", label: "P95", num: true, render: (row) => fmtMs(row.p95_duration_ms) },
    { key: "avg_duration_ms", label: "平均耗时", num: true, render: (row) => fmtMs(row.avg_duration_ms) },
    { key: "avg_throughput", label: "tokens/s", num: true, render: (row) => fmtNumber(row.avg_throughput, 1) },
    { key: "output_tokens", label: "输出 tokens", num: true },
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
