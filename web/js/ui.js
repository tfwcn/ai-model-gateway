/**
 * 界面小工具：建元素、格式化数值、渲染表格
 *
 * 这些函数不做任何请求，纯展示，方便各视图共用同一套排版。
 */

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  Object.entries(attrs).forEach(([name, value]) => {
    if (value === null || value === undefined || value === false) return;
    if (name === "class") node.className = value;
    else if (name === "text") node.textContent = value;
    else if (name.startsWith("on")) node.addEventListener(name.slice(2).toLowerCase(), value);
    else if (name === "dataset") Object.entries(value).forEach(([k, v]) => { node.dataset[k] = v; });
    else if (name === "open") node.open = Boolean(value);
    else node.setAttribute(name, String(value));
  });
  (Array.isArray(children) ? children : [children]).forEach((child) => {
    if (child === null || child === undefined) return;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  });
  return node;
}

export function fmtNumber(value, digits = 0) {
  if (value === null || value === undefined || value === "") return "-";
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  return num.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/**
 * 大数字的紧凑写法：卡片里只显示 1.2K / 3.4M / 5.6B，具体值放在 title 里
 *
 * 只用于展示，不改变接口返回值；没有单位时按原数字输出。
 */
export function fmtCompact(value) {
  if (value === null || value === undefined || value === "") return "-";
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  const abs = Math.abs(num);
  const units = [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]];
  for (const [size, unit] of units) {
    if (abs >= size) {
      const scaled = (num / size).toFixed(1).replace(/\.0$/, "");
      return `${scaled}${unit}`;
    }
  }
  return String(num);
}

export function fmtRate(value) {
  if (value === null || value === undefined) return "-";
  return `${(Number(value) * 100).toFixed(1)}%`;
}

export function fmtMs(value) {
  if (value === null || value === undefined) return "-";
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  return num >= 1000 ? `${(num / 1000).toFixed(2)}s` : `${Math.round(num)}ms`;
}

export function fmtTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}

/**
 * 调用方标签的展示名
 *
 * admin 是管理密钥自己的流量；未启用鉴权或历史数据的空标签也按 admin 显示。
 * 用户创建自己的标签后按原样显示。
 */
export function userLabel(value) {
  const name = String(value || "").trim();
  if (!name || name === "anonymous" || name === "admin") return "admin";
  return name;
}

export function truthyChip(value, yes = "是", no = "否") {
  return el("span", { class: value ? "chip ok" : "chip" }, value ? yes : no);
}

// 列数达到这个数量的表在窄屏需要横向平移，会在表格上方给一句提示
const WIDE_TABLE_COLUMNS = 8;

/**
 * 渲染表格
 *
 * columns: [{key, label, num?, render?}]，render 返回字符串或节点
 * options.onRow(row, tr)：点了某一行时的回调；拿到 tr 便于在行后插入展开内容
 *
 * 外面套一层 .table-scroll：列多的表在窄屏只在自己的区域里横向平移，不会把整页撑出横向滚动条；
 * 行数多的表由这一层自己竖向滚动（高度有上限），横向滚动条不用拉到整页最底部才能拖到。
 */
export function table(columns, rows, options = {}) {
  const head = el("tr", {}, columns.map((col) =>
    el("th", { class: col.num ? "num" : "" }, col.label)));

  const body = rows.map((row) => {
    const tr = el("tr", options.onRow ? { class: "clickable" } : {});
    columns.forEach((col) => {
      const value = col.render ? col.render(row) : row[col.key];
      const node = value instanceof Node ? value : document.createTextNode(String(value === null || value === undefined ? "-" : value));
      tr.appendChild(el("td", { class: col.num ? "num" : "" }, node));
    });
    if (options.onRow) tr.addEventListener("click", () => options.onRow(row, tr));
    return tr;
  });

  const tbody = el("tbody", {}, body);
  const parts = [el("div", { class: "table-scroll" }, [el("table", {}, [el("thead", {}, head), tbody])])];
  if (columns.length >= WIDE_TABLE_COLUMNS) {
    // 列多的表在窄屏确实需要平移：把这件事写在表格旁边，免得以为页面坏了
    parts.unshift(el("p", { class: "table-hint", text: "这一张表列多：窄屏时可以在表格区域里左右拖动，整页不会横向滚动。" }));
  }
  return el("div", { class: "table-box" }, parts);
}

/**
 * 按表格在页面里的实际位置收窄高度
 *
 * 固定写 `calc(100vh - 240px)` 在每页都会差一点：条件区占得多的页面会超出视口，
 * 于是最外层又冒出滚动条。这里统一按「视口高度 - 表格框顶部 - 底部留白」计算，
 * 表格框刚好填满剩余空间，页面本身就不用再为看表格底部而滚动。
 *
 * 调用时机：视图重绘、窗口尺寸变化、折叠区展开/收起之后。
 */
export function fitTables(root = document) {
  // 表格框下面还有面板内边距、面板下外边距和页面底部留白，一并留出来
  const reserve = 72;
  root.querySelectorAll(".table-scroll").forEach((box) => {
    const top = documentTop(box);
    const height = Math.max(200, Math.round(window.innerHeight - top - reserve));
    box.style.maxHeight = `${height}px`;
  });
}

/** 元素相对整个文档的顶部偏移：与页面当前滚动位置无关，量一次可以一直用 */
function documentTop(node) {
  let top = 0;
  let current = node;
  while (current) {
    top += current.offsetTop;
    current = current.offsetParent;
  }
  return top;
}

export function cards(items) {
  return el("div", { class: "cards" }, items.map((item) =>
    el("div", { class: `card ${item.tone || ""}` }, [
      // title 用于悬停时显示完整数字，不占用卡片空间
      el("div", { class: "value", text: item.value, title: item.title }),
      el("div", { class: "label", text: item.label }),
    ])));
}

export function issues(list) {
  if (!list || !list.length) return null;
  return el("div", { class: "issues" }, list.map((issue) =>
    el("div", { class: `issue ${issue.level || "info"}` },
      `${issue.path ? `[${issue.path}] ` : ""}${issue.message}`)));
}

export function empty(text) {
  return el("p", { class: "muted", text });
}

/**
 * 表单小控件：统一「小标签 + 控件」的排版
 *
 * 用 label 而不是 fieldset：fieldset 自带边框，放在表单区外面会变成一个巨大的空框。
 */
export function control(labelText, node) {
  return el("label", { class: "control" }, [el("span", { class: "field-label", text: labelText }), node]);
}

/** 带小标签的一组控件：复合控件（输入框 + 按钮 + 已选标签）别用 label 包，否则点按钮会顺带聚焦到输入框 */
export function fieldGroup(labelText, node) {
  return el("div", { class: "control" }, [el("span", { class: "field-label", text: labelText }), node]);
}

export function select(options, value = "") {
  const node = el("select");
  options.forEach((option) => {
    const item = typeof option === "string" ? { value: option, label: option } : option;
    node.appendChild(el("option", { value: item.value, selected: item.value === value }, item.label));
  });
  return node;
}

export function textInput(value = "", placeholder = "") {
  return el("input", { type: "text", value, placeholder });
}

export function button(label, kind = "", type = "button") {
  return el("button", { class: kind, type }, label);
}

export function panel(title, children, actions = null) {
  const head = actions
    ? el("div", { class: "row-actions" }, [el("h3", { text: title }), actions])
    : el("h3", { text: title });
  return el("section", { class: "panel" }, [head, ...children]);
}

/**
 * 可折叠区块：用原生 details/summary，不需要额外的展开/折叠脚本
 *
 * Args:
 *   title:    折叠标题，任何状态都可见
 *   children: 展开后才显示的节点
 *   options:  {
 *             meta:   折叠状态也显示的摘要（一行就够，用来判断要不要展开）
 *             actions: 展开后第一行的按钮组
 *             open:   默认是否展开
 *           }
 */
export function collapsible(title, children, options = {}) {
  const { meta = "", actions = null, open = false } = options;
  const summary = el("summary", {}, [
    el("span", { class: "panel-title", text: title }),
    meta ? el("span", { class: "panel-meta", text: meta }) : null,
  ]);
  const head = actions ? el("div", { class: "row-actions" }, actions) : null;
  const body = el("div", { class: "panel-body" }, [
    head, ...(Array.isArray(children) ? children : [children]),
  ]);
  // dataset.title 让上层视图能在刷新前后对上同一个区块，展开状态不会一刷新就丢
  return el("details", { class: "panel collapsible", open, dataset: { title } }, [summary, body]);
}

/**
 * 可折叠的筛选区：默认收起，只留「标题 + 当前条件摘要 + 操作按钮」一行
 *
 * 筛选项一多就会占掉大半屏，表格反而被挤到下面要往下翻。收起后：
 *   - 折叠状态也能一眼看出「现在筛的是什么」（meta 摘要）
 *   - 查询按钮放在标题行里，不展开也能直接点
 *
 * Args:
 *   title:    区块标题（如「筛选条件」）
 *   children: 展开后才显示的筛选项
 *   options:  {
 *             meta:    条件摘要文本，或调用方自己创建的节点（便于随条件更新）
 *             actions: 常驻操作按钮（单个节点或数组）
 *             open:    默认是否展开，默认收起
 *           }
 */
export function filterPanel(title, children, options = {}) {
  const { meta = "", actions = null, open = false } = options;
  const metaNode = typeof meta === "string"
    ? el("span", { class: "panel-meta", text: meta })
    : meta;
  const summary = el("summary", {}, [
    el("span", { class: "panel-title", text: title }),
    metaNode,
  ]);

  if (actions) {
    const actionBar = el("span", { class: "summary-actions" }, Array.isArray(actions) ? actions : [actions]);
    // 按钮住在 summary 里：点按钮只是查询，不应该顺带展开/收起
    actionBar.addEventListener("click", (event) => event.preventDefault());
    summary.appendChild(actionBar);
  }

  const body = el("div", { class: "panel-body" }, Array.isArray(children) ? children : [children]);
  // dataset.title 与 collapsible 保持一致，刷新后能按标题恢复展开状态
  return el("details", { class: "panel collapsible filter-panel", open, dataset: { title } }, [summary, body]);
}

/** 批量展开/折叠容器里的可折叠区块 */
export function setCollapsed(root, open) {
  root.querySelectorAll("details.collapsible").forEach((node) => {
    node.open = open;
  });
}

/** 收集当前展开着的区块标题，供刷新后恢复 */
export function openTitles(root) {
  const titles = new Set();
  root.querySelectorAll("details.collapsible").forEach((node) => {
    if (node.open) titles.add(node.dataset.title);
  });
  return titles;
}

/** 按标题恢复展开状态（记不住的区块保持默认折叠） */
export function restoreTitles(root, titles) {
  root.querySelectorAll("details.collapsible").forEach((node) => {
    if (titles.has(node.dataset.title)) node.open = true;
  });
}

/** 读取一组控件的当前取值：controls 为 [{name, node}] */
export function readControls(controls) {
  const values = {};
  controls.forEach(({ name, node }) => {
    const value = node.value;
    if (value !== "" && value !== null && value !== undefined) values[name] = value;
  });
  return values;
}
