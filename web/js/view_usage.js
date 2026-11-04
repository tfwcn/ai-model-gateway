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

import { button, cards, control, el, empty, fieldGroup, fmtMs, fmtNumber, fmtRate, panel, select, table, textInput, userLabel } from "./ui.js";
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

// 常用维度组合：直接写成页面上的按钮，省得用户记不住键名和组合方式
const DIMENSION_PRESETS = [
  { value: "user,model", label: "用户 × 模型（user,model）" },
  { value: "month,user,model", label: "月 × 用户 × 模型（month,user,model）" },
  { value: "day,user,model", label: "日 × 用户 × 模型（day,user,model）" },
  { value: "model", label: "模型（model）" },
  { value: "user", label: "用户（user）" },
  { value: "endpoint,model", label: "入口 × 模型（endpoint,model）" },
];

// dimension-keys 只返回 JSON 维度；user/endpoint/day/month 是固定分组键，页面一起列出来
const DIMENSION_GROUP_KEYS = ["user", "endpoint", "day", "month"];

// 维度表的固定指标列：维度列由用户填写的组合决定，这里把固定列先说清楚
const DIMENSION_METRIC_HINT =
  "结果固定包含：请求、尝试、成功、失败、中断、输入 tokens、缓存 tokens、输出 tokens、成功率。";

// 用量页内的分页签：默认看概览，其余表格与图表各占一页，避免一屏堆太多内容
const USAGE_TABS = [
  { key: "overview", label: "概览" },
  { key: "daily", label: "每日用量" },
  { key: "models", label: "模型榜" },
  { key: "dimensions", label: "维度汇总" },
  { key: "aggregate", label: "聚合明细" },
];

export async function renderUsage(root, ctx) {
  // 全局筛选：所有页签共用
  const sinceNode = select(SINCE_OPTIONS, "");
  const userNode = textInput("", "留空表示不限");

  // 概览：平台 / 模型 / 曲线粒度
  const overviewPlatform = textInput("", "留空表示不限");
  const overviewModel = textInput("", "留空表示不限");
  const overviewBucket = select([{ value: "day", label: "按天" }, { value: "hour", label: "按小时" }], "day");
  const overviewButton = button("查询概览", "primary");

  const dailyButton = button("刷新", "primary");

  // 模型榜：排序
  const modelsSort = select(SORT_OPTIONS, "requests");
  const modelsButton = button("查询模型榜", "primary");

  // 聚合明细：平台 / 模型 / 聚合维度 / 排序
  const aggregatePlatform = textInput("", "留空表示不限");
  const aggregateModel = textInput("", "留空表示不限");
  const aggregateGroupBy = select(GROUP_OPTIONS, "day");
  const aggregateSort = select(SORT_OPTIONS, "requests");
  const aggregateButton = button("查询聚合", "primary");

  const loadButton = button("查询全部", "primary");
  const kpiBox = el("div");
  const dailyBox = el("div");
  const chartCanvas = el("canvas", { class: "chart" });
  const slowCanvas = el("canvas", { class: "chart" });
  const aggregateBox = el("div");
  const rankingBox = el("div");
  const dimensionBox = el("div");

  const dimGroup = textInput("model", "例如 user,model 或 month,user,model");
  const dimEndpoint = textInput("", "例如 passthrough:example-service");
  const dimButton = button("查询维度", "primary");
  const dimKeysBox = el("div", { class: "option-chips" }, [
    el("span", { class: "muted", text: "读取可用维度中…" }),
  ]);
  const dimPresetRow = el("div", { class: "control-row" }, [
    el("span", { class: "field-label", text: "常用组合" }),
    ...DIMENSION_PRESETS.map((preset) => el("button", {
      class: "ghost",
      type: "button",
      text: preset.label,
      title: `填入 ${preset.value}`,
      onclick: () => {
        dimGroup.value = preset.value;
        dimGroup.focus();
      },
    })),
  ]);
  const dimensionControls = [
    dimPresetRow,
    el("div", { class: "filters" }, [
      control("维度组合", dimGroup),
      control("入口", dimEndpoint),
    ]),
    fieldGroup("可用维度 / 分组键", dimKeysBox),
    el("p", { class: "muted", text: "维度名用英文逗号分隔；user=用户，endpoint=入口，day/month=时间，其余是透传路由声明的维度。时间范围和用户标签取上方「全局筛选」；点「查询维度」只刷新本页签。" }),
    el("p", { class: "muted", text: DIMENSION_METRIC_HINT }),
  ];

  const globalPanel = panel("全局筛选", [
    el("div", { class: "filters" }, [
      control("时间范围", sinceNode),
      control("用户标签", userNode),
    ]),
    el("p", { class: "muted", text: "这两个条件对所有页签生效；改完点「查询全部」刷新所有页签，或在各页签内单独查询。" }),
  ], loadButton);

  // 最近一次查询结果：切回图表页签时要用它重画，避免隐藏状态下量不到宽度
  const latest = { series: null, ranking: null };

  // 每个页签一个内容容器，筛选条件也放在各自页签里
  const paneNodes = {
    overview: el("div", {}, [
      panel("概览筛选", [
        el("div", { class: "filters" }, [
          control("平台", overviewPlatform),
          control("模型名", overviewModel),
          control("曲线粒度", overviewBucket),
        ]),
        el("p", { class: "muted", text: "平台 / 模型影响趋势和成功率卡片；输入 / 缓存 / 输出 tokens 卡片来自日汇总，只按全局时间和用户统计。" }),
      ], overviewButton),
      kpiBox,
      panel("趋势（请求数 / P95 耗时）", [chartCanvas]),
    ]),
    daily: el("div", {}, [
      panel("每日用量（全量，按天 / 用户，不随审计清理）", [
        el("p", { class: "muted", text: "使用上方全局筛选（时间范围、用户标签）。" }),
        dailyBox,
      ], dailyButton),
    ]),
    models: el("div", {}, [
      panel("模型榜（按所选排序）", [
        el("div", { class: "filters" }, [control("排序", modelsSort)]),
        slowCanvas,
        rankingBox,
      ], modelsButton),
    ]),
    dimensions: el("div", {}, [
      panel("透传维度汇总（永久日汇总）", [...dimensionControls, dimensionBox], dimButton),
    ]),
    aggregate: el("div", {}, [
      panel("聚合明细（审计明细范围内）", [
        el("div", { class: "filters" }, [
          control("平台", aggregatePlatform),
          control("模型名", aggregateModel),
          control("聚合维度", aggregateGroupBy),
          control("排序", aggregateSort),
        ]),
        aggregateBox,
      ], aggregateButton),
    ]),
  };
  const tabButtons = USAGE_TABS.map((item) => el("button", {
    class: "tab",
    type: "button",
    dataset: { tab: item.key },
    text: item.label,
    onclick: () => selectTab(item.key),
  }));
  const tabBar = el("div", { class: "tabs view-tabs" }, tabButtons);

  root.append(globalPanel, tabBar, ...Object.values(paneNodes));
  selectTab("overview");

  function selectTab(name) {
    Object.entries(paneNodes).forEach(([key, pane]) => {
      pane.hidden = key !== name;
    });
    tabButtons.forEach((node) => {
      node.setAttribute("aria-selected", String(node.dataset.tab === name));
    });
    // 图表在隐藏容器里量不到宽度，切回可见状态后再画一次
    window.requestAnimationFrame(drawVisibleCharts);
  }

  function drawVisibleCharts() {
    if (!paneNodes.overview.hidden && latest.series) drawTrend(chartCanvas, latest.series);
    if (!paneNodes.models.hidden && latest.ranking) drawRankingChart(slowCanvas, latest.ranking);
  }

  loadButton.addEventListener("click", () => loadAll());
  overviewButton.addEventListener("click", () => loadOverview());
  dailyButton.addEventListener("click", () => loadDaily());
  modelsButton.addEventListener("click", () => loadModels());
  aggregateButton.addEventListener("click", () => loadAggregate());
  dimButton.addEventListener("click", () => loadDimensions());

  async function loadAll(quiet = false) {
    loadButton.disabled = true;
    try {
      await Promise.all([
        loadOverview(true),
        loadDaily(true),
        loadModels(true),
        loadAggregate(true),
        loadDimensions(true),
      ]);
      if (!quiet) ctx.notice("已刷新所有页签", "ok");
    } finally {
      loadButton.disabled = false;
    }
  }

  async function loadOverview(quiet = false) {
    const since = sinceNode.value;
    const user = userNode.value.trim();
    const platform = overviewPlatform.value.trim();
    const model = overviewModel.value.trim();
    overviewButton.disabled = true;
    try {
      const [totals, daily, series] = await Promise.all([
        ctx.api.postQuery("/admin/usage/summary", { since, platform, model, user, group_by: "outcome", limit: 20 }),
        ctx.api.postQuery("/admin/usage/daily", { since, user, limit: 2000 }),
        ctx.api.postQuery("/admin/usage/timeseries", { since, platform, model, user, bucket: overviewBucket.value }),
      ]);
      renderCards(kpiBox, totals, daily.totals);
      latest.series = series;
      drawVisibleCharts();
      if (!quiet) ctx.notice("概览已刷新", "ok");
    } catch (error) {
      ctx.guard(error);
    } finally {
      overviewButton.disabled = false;
    }
  }

  async function loadDaily(quiet = false) {
    dailyButton.disabled = true;
    try {
      const daily = await ctx.api.postQuery("/admin/usage/daily", {
        since: sinceNode.value,
        user: userNode.value.trim(),
        limit: 2000,
      });
      renderDaily(dailyBox, daily);
      if (!quiet) ctx.notice(`每日用量已刷新（${daily.count} 行日汇总）`, "ok");
    } catch (error) {
      ctx.guard(error);
    } finally {
      dailyButton.disabled = false;
    }
  }

  async function loadModels(quiet = false) {
    modelsButton.disabled = true;
    try {
      const ranking = await ctx.api.postQuery("/admin/usage/models", {
        since: sinceNode.value,
        user: userNode.value.trim(),
        sort: modelsSort.value,
        limit: 10,
      });
      latest.ranking = ranking;
      drawVisibleCharts();
      renderRanking(rankingBox, ranking);
      if (!quiet) ctx.notice("模型榜已刷新", "ok");
    } catch (error) {
      ctx.guard(error);
    } finally {
      modelsButton.disabled = false;
    }
  }

  async function loadAggregate(quiet = false) {
    const since = sinceNode.value;
    const user = userNode.value.trim();
    const platform = aggregatePlatform.value.trim();
    const model = aggregateModel.value.trim();
    aggregateButton.disabled = true;
    try {
      const grouped = await ctx.api.postQuery("/admin/usage/summary", {
        since,
        platform,
        model,
        user,
        group_by: aggregateGroupBy.value,
        limit: 60,
        sort: aggregateSort.value,
      });
      renderAggregate(aggregateBox, grouped);
      if (!quiet) ctx.notice("聚合明细已刷新", "ok");
    } catch (error) {
      ctx.guard(error);
    } finally {
      aggregateButton.disabled = false;
    }
  }

  async function loadDimensions(quiet = false) {
    dimButton.disabled = true;
    try {
      const payload = await ctx.api.postQuery("/admin/usage/dimensions", {
        group_by: dimGroup.value.trim() || "model",
        since: sinceNode.value,
        endpoint: dimEndpoint.value.trim(),
        user: userNode.value.trim(),
        limit: 500,
      });
      renderDimensions(dimensionBox, payload);
      if (!quiet) ctx.notice(`维度汇总：${payload.count} 组（${payload.group_by.join(" × ")}）`, "ok");
    } catch (error) {
      ctx.guard(error);
    } finally {
      dimButton.disabled = false;
    }
  }

  async function loadDimensionKeys() {
    try {
      const payload = await ctx.api.postQuery("/admin/usage/dimension-keys");
      renderDimensionKeys(dimKeysBox, payload.keys || [], (name) => {
        dimGroup.value = appendDimension(dimGroup.value, name);
        dimGroup.focus();
      });
    } catch {
      // 候选读不到不影响手填维度名，不弹全局错误
      dimKeysBox.replaceChildren(el("span", { class: "muted", text: "可用维度读取失败，可直接手填维度名" }));
    }
  }

  await Promise.all([loadAll(true), loadDimensionKeys()]);
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

function drawTrend(canvas, series) {
  const points = series.points || [];
  const labels = points.map((point) => point.bucket.slice(-5));

  lineChart(canvas, labels, [
    { label: "请求数", values: points.map((point) => point.requests) },
    { label: "P95 耗时(ms)", values: points.map((point) => point.p95_duration_ms) },
    { label: "首块耗时(ms)", values: points.map((point) => point.avg_ttft_ms) },
  ]);
}

function drawRankingChart(canvas, ranking) {
  const rows = (ranking.items || []).slice(0, 10);
  barChart(canvas, rows.map((item) => shortBucket(item.bucket)), rows.map((item) => item.p95_duration_ms || 0));
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
      { key: "input_tokens", label: "输入 tokens", num: true },
      { key: "cached_tokens", label: "缓存 tokens", num: true },
      { key: "output_tokens", label: "输出 tokens", num: true },
      { key: "success_rate", label: "成功率", num: true, render: (row) => fmtRate(row.success_rate) },
    ], items),
  );
}

/** 可用维度候选：点一下追加到组合输入框，避免用户猜键名 */
function renderDimensionKeys(box, keys, onPick) {
  const stored = [...new Set((keys || []).map((key) => String(key).trim()).filter(Boolean))];
  const names = [...DIMENSION_GROUP_KEYS, ...stored.filter((name) => !DIMENSION_GROUP_KEYS.includes(name))];
  box.replaceChildren(
    el("span", { class: "muted", text: "点一下追加：" }),
    ...names.map((name) => el("button", {
      class: "ghost",
      type: "button",
      text: name,
      title: `追加到维度组合：${name}`,
      onclick: () => onPick(name),
    })),
  );
}

/** 把维度名追加进逗号分隔的组合里，已存在时保持原样 */
function appendDimension(current, name) {
  const names = String(current || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  if (!names.includes(name)) names.push(name);
  return names.join(",");
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
