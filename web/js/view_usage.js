/**
 * 视图：用量与性能
 *
 * 数据来源（只读永久汇总，不碰审计明细）：
 *   POST /admin/usage/daily       全量日用量（概览卡片，永久保留）
 *   POST /admin/usage/dimensions  永久维度日汇总（概览趋势 + 维度汇总）
 * 审计明细只保留 30 天，相关的明细、聚合与趋势统一放到「审计明细」页。
 * 成功率只统计已收尾的尝试（finalized=1），未收尾的 stream_open 不算分母。
 */

import { button, cards, control, el, empty, fmtCompact, fmtMs, fmtNumber, fmtRate, panel, select, table, textInput } from "./ui.js";
import { lineChart } from "./charts.js";
import { DateRange } from "./date_range.js";
import { LastDimensionQuery, RecentCombos } from "./recent_combos.js";

// 组合按钮的展示名：只翻译常见键，未知维度名原样显示
const DIMENSION_LABELS = {
  user: "用户",
  endpoint: "入口",
  day: "日",
  month: "月",
  model: "模型",
  platform: "平台",
  selector: "请求 model",
  outcome: "结局",
  error: "错误",
};

// 维度汇总可用的排序指标：只列出永久日汇总表里真实存在的列
const DIMENSION_SORT_OPTIONS = [
  { value: "requests", label: "请求数" },
  { value: "attempts", label: "尝试数" },
  { value: "success", label: "成功次数" },
  { value: "failure", label: "失败次数" },
  { value: "aborted", label: "中断次数" },
  { value: "retries", label: "重试次数" },
  { value: "duration", label: "平均耗时" },
  { value: "ttft", label: "首块耗时" },
  { value: "throughput", label: "tokens/s" },
  { value: "p95", label: "P95 耗时（估算）" },
  { value: "input_tokens", label: "输入 tokens" },
  { value: "cached_tokens", label: "缓存 tokens" },
  { value: "output_tokens", label: "输出 tokens" },
  { value: "success_rate", label: "成功率" },
];

// 自动方向：维度按升序、指标按降序，符合看报表的直觉
const DIMENSION_ORDER_OPTIONS = [
  { value: "", label: "自动（维度升序 / 指标降序）" },
  { value: "desc", label: "降序" },
  { value: "asc", label: "升序" },
];

// dimension-keys 只返回 JSON 维度；user/endpoint/day/month 是固定分组键，页面一起列出来
const DIMENSION_GROUP_KEYS = ["user", "endpoint", "day", "month"];

// 维度表的固定指标列：维度列由用户填写的组合决定，这里把固定列先说清楚
const DIMENSION_METRIC_HINT =
  "结果固定包含：请求、尝试、成功、失败、中断、重试、首块、P95（按耗时直方图估算）、平均耗时、tokens/s、输入 tokens、缓存 tokens、输出 tokens、成功率。";

// 用量页内的分页签：默认看概览，其余表格与图表各占一页，避免一屏堆太多内容
const USAGE_TABS = [
  { key: "overview", label: "概览" },
  { key: "dimensions", label: "维度汇总" },
];

export async function renderUsage(root, ctx) {
  // 全局筛选：默认当月，清空日期即可查全部永久数据
  const range = new DateRange({ currentMonth: true });
  const userNode = textInput("", "留空表示不限");

  // 概览只读永久日汇总，不再提供平台 / 模型筛选（30 天范围的数据在审计页）
  const overviewButton = button("查询概览", "primary");

  const loadButton = button("查询全部", "primary");
  const kpiBox = el("div");
  const chartCanvas = el("canvas", { class: "chart" });
  const dimensionBox = el("div");

  // 组合与上一次查询条件都存在浏览器里；没有记录时给一个能直接查的默认值
  const recentCombos = new RecentCombos();
  const lastDimensionQuery = new LastDimensionQuery();
  const initialRecent = recentCombos.load();
  const lastCondition = lastDimensionQuery.load() || initialRecent[0] || {
    group_by: "model", endpoint: "", sort: "requests", order: "",
  };
  const dimGroup = textInput(
    lastCondition.group_by,
    "例如 user,model 或 month,user,model",
  );
  const dimEndpoint = textInput(lastCondition.endpoint || "", "例如 passthrough:example-service");
  const dimSort = select(DIMENSION_SORT_OPTIONS, "requests");
  const dimOrder = select(DIMENSION_ORDER_OPTIONS, lastCondition.order || "");
  const dimButton = button("查询维度", "primary");
  const dimExportButton = button("导出 CSV");
  const dimKeysBox = el("div", { class: "dim-chip-row" }, [
    el("span", { class: "muted", text: "读取可用维度中…" }),
  ]);
  // 维度候选来自接口，排序下拉在拿到候选后重建；先放固定分组键
  let availableDimensionNames = [...DIMENSION_GROUP_KEYS];

  // 按所选维度动态生成筛选输入框；值按维度名保存在节点映射里
  const dimFilterBox = el("div", { class: "dim-filter-grid" });
  let dimensionFilterNodes = new Map();
  let dimensionFilterValues = {};
  let renderedFilterKey = "";

  // 最近组合按钮：内容来自 localStorage，节点引用用于同步高亮
  let dimPresetButtons = [];
  const dimPresetRow = el("div", { class: "dim-chip-row" }, []);

  const dimensionControls = [
    el("div", { class: "dim-builder" }, [
      el("div", { class: "dim-builder-main" }, [
        control("维度组合", dimGroup),
        control("入口", dimEndpoint),
        control("排序", dimSort),
        control("方向", dimOrder),
      ]),
      el("div", { class: "dim-section" }, [
        el("div", { class: "dim-section-head" }, [
          el("span", { class: "field-label", text: "按维度筛选" }),
          el("span", { class: "dim-hint", text: "只筛当前组合里的维度，精确匹配；多个值用英文逗号分隔" }),
        ]),
        dimFilterBox,
      ]),
      el("div", { class: "dim-section" }, [
        el("div", { class: "dim-section-head" }, [
          el("span", { class: "field-label", text: "最近组合" }),
          el("span", { class: "dim-hint", text: "自动记录前 10 个，查询后置顶" }),
        ]),
        dimPresetRow,
      ]),
      el("div", { class: "dim-section" }, [
        el("div", { class: "dim-section-head" }, [
          el("span", { class: "field-label", text: "可用维度 / 分组键" }),
          el("span", { class: "dim-hint", text: "点一下加入，再点一下移除" }),
        ]),
        dimKeysBox,
      ]),
      el("details", { class: "dim-help" }, [
        el("summary", { text: "维度写法与结果说明" }),
        el("p", { class: "muted", text: "维度名用英文逗号分隔；user=用户，endpoint=入口，day/month=时间，其余是路由声明的维度。日期范围和用户标签取上方「全局筛选」；点「查询维度」只刷新本页签。" }),
        el("p", { class: "muted", text: DIMENSION_METRIC_HINT }),
      ]),
    ]),
    el("div", { class: "dim-results" }, [dimensionBox]),
  ];

  const globalPanel = panel("全局筛选", [
    el("div", { class: "filters" }, [
      ...range.controls(),
      control("用户标签", userNode),
    ]),
    el("p", { class: "muted", text: "默认当月（1 日到今天）；日期留空表示不限，结束日期当天包含在内。这两个条件对所有页签生效。本页只读永久日汇总，30 天内的明细、聚合与趋势请到「审计明细」页。" }),
  ], loadButton);

  // 最近一次查询结果：切回图表页签时要用它重画，避免隐藏状态下量不到宽度
  const latest = { series: null };

  // 每个页签一个内容容器，筛选条件也放在各自页签里
  const paneNodes = {
    overview: el("div", {}, [
      panel("永久用量概览", [
        el("p", { class: "muted", text: "卡片来自 usage_daily，趋势来自 usage_dimension_daily，均只累加、不随审计明细清理；P95 为按耗时直方图估算。" }),
      ], overviewButton),
      kpiBox,
      panel("趋势（请求数 / P95 耗时 / 首块耗时）", [chartCanvas]),
    ]),
    dimensions: el("div", {}, [
      panel("维度汇总（永久日汇总）", dimensionControls,
        el("div", { class: "row-actions" }, [dimButton, dimExportButton])),
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
  }

  loadButton.addEventListener("click", () => loadAll());
  overviewButton.addEventListener("click", () => loadOverview());
  dimButton.addEventListener("click", () => loadDimensions());
  dimExportButton.addEventListener("click", () => exportDimensions());
  dimGroup.addEventListener("input", () => {
    syncDimensionFilters();
    syncDimensionSortOptions();
    syncDimensionPresetState();
    refreshDimensionKeyButtons();
  });
  dimGroup.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.isComposing) return;
    event.preventDefault();
    loadDimensions();
  });
  dimEndpoint.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.isComposing) return;
    event.preventDefault();
    loadDimensions();
  });

  async function loadAll(quiet = false) {
    loadButton.disabled = true;
    try {
      await Promise.all([
        loadOverview(true),
        loadDimensions(true),
      ]);
      if (!quiet) ctx.notice("已刷新所有页签", "ok");
    } finally {
      loadButton.disabled = false;
    }
  }

  async function loadOverview(quiet = false) {
    const { since, until } = range.params();
    const user = userNode.value.trim();
    overviewButton.disabled = true;
    try {
      const [daily, trend] = await Promise.all([
        ctx.api.postQuery("/admin/usage/daily", { since, until, user, limit: 2000 }),
        ctx.api.postQuery("/admin/usage/dimensions", {
          since, until, user, group_by: "day", sort: "day", order: "asc", limit: 1000,
        }),
      ]);
      renderCards(kpiBox, daily.totals);
      latest.series = trend;
      drawVisibleCharts();
      if (!quiet) ctx.notice("概览已刷新", "ok");
    } catch (error) {
      ctx.guard(error);
    } finally {
      overviewButton.disabled = false;
    }
  }

  function readDimensionFilterInputs() {
    const result = {};
    dimensionFilterNodes.forEach((node, name) => {
      const value = node.value.trim();
      if (value) result[name] = value;
    });
    return result;
  }

  /** 按当前组合重建筛选输入框；切换组合时保留同名维度的输入 */
  function renderDimensionFilters(names, values) {
    dimensionFilterNodes = new Map();
    const selected = names.filter(Boolean);
    if (!selected.length) {
      dimFilterBox.replaceChildren(
        el("span", { class: "muted", text: "先在维度组合里选择维度" }),
      );
      return;
    }
    dimFilterBox.replaceChildren(...selected.map((name) => {
      const input = textInput(values[name] || "", "留空表示不限");
      input.title = `按 ${name} 精确筛选；多个值用英文逗号分隔`;
      dimensionFilterNodes.set(name, input);
      return control(`筛选 ${name}`, input);
    }));
  }

  function syncDimensionFilters() {
    const names = parseDimensionNames(dimGroup.value);
    const key = names.join(",");
    if (key === renderedFilterKey) return;
    const current = readDimensionFilterInputs();
    const next = {};
    names.forEach((name) => {
      if (current[name]) next[name] = current[name];
    });
    dimensionFilterValues = next;
    renderedFilterKey = key;
    renderDimensionFilters(names, dimensionFilterValues);
  }

  function currentDimensionFilters() {
    syncDimensionFilters();
    return readDimensionFilterInputs();
  }

  /** 把输入框里的逗号分隔值转成后端要的字符串或字符串数组 */
  function encodeDimensionFilters(filters) {
    const payload = {};
    Object.entries(filters || {}).forEach(([name, raw]) => {
      const values = String(raw || "")
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean);
      if (!values.length) return;
      payload[name] = values.length === 1 ? values[0] : values;
    });
    return Object.keys(payload).length ? JSON.stringify(payload) : "";
  }

  /** 当前完整条件：组合、入口、排序、方向、维度筛选 */
  function currentDimensionCondition() {
    return {
      group_by: parseDimensionNames(dimGroup.value).join(",") || "model",
      endpoint: dimEndpoint.value.trim(),
      sort: dimSort.value,
      order: dimOrder.value,
      filters: currentDimensionFilters(),
    };
  }

  function dimensionParams() {
    const condition = currentDimensionCondition();
    return {
      ...range.params(),
      ...condition,
      user: userNode.value.trim(),
      filters: encodeDimensionFilters(condition.filters),
    };
  }

  /** 渲染最近组合：每条记录同时带排序 / 方向，点一下恢复整套条件 */
  function renderRecentCombos(values) {
    dimPresetButtons = (values || []).map((condition) => {
      const value = condition.group_by;
      const orderLabel = condition.order ? ` ${condition.order}` : " 自动";
      const filterText = Object.entries(condition.filters || {})
        .map(([name, raw]) => `${name}=${raw}`)
        .join("；");
      const node = el("button", {
        class: "dim-chip",
        type: "button",
        title: `填入 ${value}（入口 ${condition.endpoint || "不限"}；排序 ${condition.sort}${orderLabel}${filterText ? `；筛选 ${filterText}` : ""}）`,
        onclick: () => {
          applyDimensionCondition(condition);
          dimGroup.focus();
        },
      }, [
        el("span", { text: comboLabel(value) }),
        el("code", { text: value }),
      ]);
      return { value, node };
    });
    if (!dimPresetButtons.length) {
      dimPresetRow.replaceChildren(
        el("span", { class: "muted", text: "查询一次后会自动记录，最多保留 10 个" }),
      );
      return;
    }
    dimPresetRow.replaceChildren(...dimPresetButtons.map((item) => item.node));
  }

  /** 恢复一整套维度条件：组合、入口、排序、方向、维度筛选 */
  function applyDimensionCondition(condition) {
    dimGroup.value = condition.group_by || "model";
    dimEndpoint.value = condition.endpoint || "";
    const names = parseDimensionNames(dimGroup.value);
    const rawFilters = condition.filters || {};
    const values = {};
    names.forEach((name) => {
      const text = String(rawFilters[name] || "").trim();
      if (text) values[name] = text;
    });
    dimensionFilterValues = values;
    renderedFilterKey = names.join(",");
    renderDimensionFilters(names, values);
    syncDimensionSortOptions();
    const sort = condition.sort && [...dimSort.options].some((option) => option.value === condition.sort)
      ? condition.sort
      : "requests";
    dimSort.value = sort;
    const order = [...dimOrder.options].some((option) => option.value === (condition.order || ""))
      ? (condition.order || "")
      : "";
    dimOrder.value = order;
    syncDimensionPresetState();
    refreshDimensionKeyButtons();
  }

  /** 最近组合按钮跟着输入框高亮，手改组合后自动取消高亮 */
  function syncDimensionPresetState() {
    const current = parseDimensionNames(dimGroup.value).join(",");
    dimPresetButtons.forEach(({ value, node }) => {
      node.setAttribute("aria-pressed", String(value === current));
    });
  }

  /** 重建排序下拉：指标固定，维度只列当前「维度组合」里选中的键 */
  function syncDimensionSortOptions() {
    const names = parseDimensionNames(dimGroup.value);
    const current = dimSort.value;
    const metricOptions = DIMENSION_SORT_OPTIONS.map((option) => el("option", {
      value: option.value,
      selected: option.value === current,
      text: option.label,
    }));
    const dimensionOptions = names.map((name) => el("option", {
      value: name,
      selected: name === current,
      text: name,
    }));
    const valid = DIMENSION_SORT_OPTIONS.some((option) => option.value === current) || names.includes(current);
    dimSort.replaceChildren(
      el("optgroup", { label: "指标" }, metricOptions),
      el("optgroup", { label: "维度" }, dimensionOptions),
    );
    if (!valid) dimSort.value = "requests";
  }

  async function loadDimensions(quiet = false) {
    const condition = currentDimensionCondition();
    const params = {
      ...range.params(),
      ...condition,
      user: userNode.value.trim(),
      filters: encodeDimensionFilters(condition.filters),
    };
    dimButton.disabled = true;
    try {
      const payload = await ctx.api.postQuery("/admin/usage/dimensions", {
        ...params,
        limit: 500,
      });
      renderDimensions(dimensionBox, payload);
      // 查询成功才记录：同一组合覆盖排序 / 方向 / 筛选，并置顶
      lastDimensionQuery.save(condition);
      renderRecentCombos(recentCombos.record(condition));
      applyDimensionCondition(condition);
      if (!quiet) ctx.notice(`维度汇总：${payload.count} 组（${payload.group_by.join(" × ")}）`, "ok");
    } catch (error) {
      ctx.guard(error);
    } finally {
      dimButton.disabled = false;
    }
  }

  async function exportDimensions() {
    dimExportButton.disabled = true;
    try {
      const file = await ctx.api.download(
        "/admin/usage/dimensions/export",
        dimensionParams(),
        "dimensions.csv",
      );
      saveBlob(file.blob, file.filename);
      ctx.notice("维度汇总 CSV 已导出", "ok");
    } catch (error) {
      ctx.guard(error);
    } finally {
      dimExportButton.disabled = false;
    }
  }

  /** 候选键按当前组合刷新高亮：已选中的再点一次会移除 */
  function refreshDimensionKeyButtons() {
    renderDimensionKeys(
      dimKeysBox,
      availableDimensionNames,
      parseDimensionNames(dimGroup.value),
      toggleDimensionKey,
    );
  }

  /** 多选式切换：加入或移除一个维度键，并同步排序与组合高亮 */
  function toggleDimensionKey(name) {
    dimGroup.value = toggleDimension(dimGroup.value, name);
    syncDimensionFilters();
    syncDimensionSortOptions();
    syncDimensionPresetState();
    refreshDimensionKeyButtons();
    dimGroup.focus();
  }

  async function loadDimensionKeys() {
    try {
      const payload = await ctx.api.postQuery("/admin/usage/dimension-keys");
      availableDimensionNames = [...new Set([...DIMENSION_GROUP_KEYS, ...(payload.keys || [])])];
      refreshDimensionKeyButtons();
      syncDimensionSortOptions();
      syncDimensionPresetState();
    } catch {
      // 候选读不到不影响手填维度名，不弹全局错误
      dimKeysBox.replaceChildren(el("span", { class: "muted", text: "可用维度读取失败，可直接手填维度名" }));
    }
  }

  renderRecentCombos(initialRecent);
  applyDimensionCondition(lastCondition);
  await Promise.all([loadAll(true), loadDimensionKeys()]);
}

// ---------- 呈现 ----------

/** 触发浏览器下载：Blob 转临时 URL，点完即回收 */
function saveBlob(blob, filename) {
  const href = URL.createObjectURL(blob);
  const link = el("a", { href, download: filename, style: "display: none" });
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(href);
}

function renderCards(box, totals = {}) {
  const rate = totals.success_rate;
  box.replaceChildren(cards([
    { label: "请求数（永久）", value: fmtCompact(totals.requests), title: fmtNumber(totals.requests) },
    { label: "输入 tokens（永久）", value: fmtCompact(totals.input_tokens), title: fmtNumber(totals.input_tokens) },
    { label: "输出 tokens（永久）", value: fmtCompact(totals.output_tokens), title: fmtNumber(totals.output_tokens) },
    { label: "缓存 tokens（永久）", value: fmtCompact(totals.cached_tokens), title: fmtNumber(totals.cached_tokens) },
    { label: "成功率（永久）", value: rate === null || rate === undefined ? "-" : fmtRate(rate), title: rate === null || rate === undefined ? undefined : `成功率 ${fmtRate(rate)}`, tone: rate !== null && rate !== undefined && rate < 0.9 ? "bad" : "ok" },
    { label: "失败尝试（永久）", value: fmtCompact(totals.failure), title: fmtNumber(totals.failure), tone: totals.failure ? "bad" : "" },
    { label: "客户端中断（永久）", value: fmtCompact(totals.aborted), title: fmtNumber(totals.aborted) },
    { label: "重试尝试（永久）", value: fmtCompact(totals.retries), title: fmtNumber(totals.retries) },
  ]));
}

function drawTrend(canvas, series) {
  const points = series.items || [];
  const labels = points.map((point) => String((point.values || {}).day || point.bucket || "").slice(-5));

  lineChart(canvas, labels, [
    { label: "请求数", values: points.map((point) => point.requests) },
    { label: "P95 耗时(ms)", values: points.map((point) => point.p95_duration_ms) },
    { label: "首块耗时(ms)", values: points.map((point) => point.avg_ttft_ms) },
  ]);
}

function renderDimensions(box, payload) {
  const items = payload.items || [];
  if (!items.length) {
    box.replaceChildren(empty("这个范围内还没有维度汇总：产生调用后会自动累计，且不随审计明细清理"));
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
      { key: "retries", label: "重试", num: true },
      { key: "avg_ttft_ms", label: "首块", num: true, render: (row) => fmtMs(row.avg_ttft_ms) },
      { key: "p95_duration_ms", label: "P95", num: true, render: (row) => fmtMs(row.p95_duration_ms) },
      { key: "avg_duration_ms", label: "平均耗时", num: true, render: (row) => fmtMs(row.avg_duration_ms) },
      { key: "avg_throughput", label: "tokens/s", num: true, render: (row) => fmtNumber(row.avg_throughput, 1) },
      { key: "input_tokens", label: "输入 tokens", num: true },
      { key: "cached_tokens", label: "缓存 tokens", num: true },
      { key: "output_tokens", label: "输出 tokens", num: true },
      { key: "success_rate", label: "成功率", num: true, render: (row) => fmtRate(row.success_rate) },
    ], items),
  );
}

/** 可用维度候选：多选式按钮，选中态就是 aria-pressed */
function renderDimensionKeys(box, keys, selectedNames, onToggle) {
  const stored = [...new Set((keys || []).map((key) => String(key).trim()).filter(Boolean))];
  const names = [...DIMENSION_GROUP_KEYS, ...stored.filter((name) => !DIMENSION_GROUP_KEYS.includes(name))];
  const selected = new Set(selectedNames || []);
  box.replaceChildren(
    ...names.map((name) => {
      const active = selected.has(name);
      return el("button", {
        class: "dim-chip dim-chip-add",
        type: "button",
        text: name,
        "aria-pressed": active ? "true" : "false",
        title: active ? `从维度组合移除：${name}` : `加入维度组合：${name}`,
        onclick: () => onToggle(name),
      });
    }),
  );
}

/** 组合按钮的展示名：已知键翻译成中文，未知维度名原样显示 */
function comboLabel(value) {
  return parseDimensionNames(value)
    .map((name) => DIMENSION_LABELS[name] || name)
    .join(" × ");
}

/** 把逗号分隔的维度组合拆成去重后的名字列表 */
function parseDimensionNames(current) {
  return [...new Set(
    String(current || "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean),
  )];
}

/** 多选式切换：没有就加入，已有就移除 */
function toggleDimension(current, name) {
  const names = parseDimensionNames(current);
  const index = names.indexOf(name);
  if (index >= 0) names.splice(index, 1);
  else names.push(name);
  return names.join(",");
}


