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
 *
 * 外面套一层 .table-scroll：列多的表在窄屏只在自己的区域里横向平移，不会把整页撑出横向滚动条。
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
    if (options.onRow) tr.addEventListener("click", () => options.onRow(row));
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

export function cards(items) {
  return el("div", { class: "cards" }, items.map((item) =>
    el("div", { class: `card ${item.tone || ""}` }, [
      el("div", { class: "value", text: item.value }),
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
