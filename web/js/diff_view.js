/**
 * 差异展示：新增绿、修改黄、删除红，行与行用同一套列宽纵向对齐
 *
 * 待提交清单也走同一套列宽：清单、「改了哪些字段」、「配置文件里改动的行」三块
 * 列宽一致，眼睛能一路对下来，不用在脑子里把改动和差异重新对齐。
 */

import { button, el, empty } from "./ui.js";
import { formatValue, overridesText } from "./config_fields.js";

// 后端 line_diff.py 给出的三类：页面上的说法
const KIND_TEXT = { add: "新增", modify: "修改", remove: "删除" };

// 补丁操作属于哪一类（清单里先按这个上色，真实取值以预演回来的差异为准）
const OP_KIND = {
  add_platform: "add",
  add_model: "add",
  remove_platform: "remove",
  remove_model: "remove",
  unset_platform_field: "remove",
  unset_model_field: "remove",
  set_platform_field: "modify",
  set_model_field: "modify",
  set_audit_field: "modify",
  set_alias_selectors: "modify",
};

// 每一列的类名：列宽只写在 style.css 的 .align-row 里，这里不重复
const CELLS = ["cell-kind", "cell-path", "cell-old", "cell-new"];

/** 一组列宽相同的行（表头 + 数据行），纵向对齐全靠它 */
class AlignGrid {
  constructor(labels) {
    this.node = el("div", { class: "align-grid" });
    this.node.appendChild(el("div", { class: "align-row head" },
      labels.map((label, index) => el("span", { class: CELLS[index], text: label }))));
  }

  /**
   * 追加一行
   *
   * Args:
   *   kind:  add / modify / remove（决定这一行的颜色）
   *   cells: 四个格子，依次是 状态 / 位置 / 原 / 新；
   *          每个格子是 {text}、{node} 或 {placeholder}（空值时显示的灰字）
   */
  addRow(kind, cells) {
    this.node.appendChild(el("div", { class: `align-row ${kind}` }, cells.map((cell, index) => {
      const wrap = el("span", { class: CELLS[index] });
      if (cell.node) wrap.appendChild(cell.node);
      else if (cell.text === undefined || cell.text === "") wrap.appendChild(el("span", { class: "muted", text: cell.placeholder || "" }));
      else wrap.appendChild(el("code", { text: String(cell.text) }));
      return wrap;
    })));
  }

  /** 一行都没有时也要说明原因，不能只留一个空表 */
  addEmpty(text) {
    this.node.appendChild(empty(text));
  }
}

/** 颜色口径写在页面上，不用翻文档 */
export function diffLegend() {
  return el("div", { class: "diff-legend" }, [
    el("span", { class: "legend add", text: "新增" }),
    el("span", { class: "legend modify", text: "修改（原值 → 新值）" }),
    el("span", { class: "legend remove", text: "删除 / 删掉这一行回落到上层" }),
    el("span", { class: "muted", text: "这些都是预演出来的差异，没有写盘" }),
  ]);
}

/**
 * 待提交的改动：状态 / 位置 / 要改什么 / 撤掉
 *
 * Args:
 *   ops:    攒着的补丁操作
 *   undoAt: (index) => void，点「撤掉」时把这一条从清单里去掉
 */
export function stagedGrid(ops, undoAt) {
  const grid = new AlignGrid(["状态", "位置", "要改什么", "操作"]);
  ops.forEach((op, index) => {
    const columns = opColumns(op);
    const undo = button("撤掉", "ghost");
    undo.addEventListener("click", () => undoAt(index));
    grid.addRow(columns.kind, [
      { text: KIND_TEXT[columns.kind] },
      { text: columns.path },
      { text: columns.detail },
      { node: undo },
    ]);
  });
  return grid.node;
}

/** 预演回来的字段级差异：状态 / 位置 / 原值 / 新值 */
export function changesGrid(changes) {
  const grid = new AlignGrid(["状态", "位置", "原值", "新值"]);
  const list = changes || [];
  list.forEach((change) => {
    const kind = changeKind(change);
    grid.addRow(kind, [
      { text: KIND_TEXT[kind] },
      { text: change.path || change.op },
      cell(change.before, "（原来没有这一行）"),
      cell(change.after, "（删掉这一行）"),
    ]);
  });
  if (!list.length) grid.addEmpty("这一批改动不会真的改变文件内容（取值本来就一样）");
  return grid.node;
}

/** 预演回来的行级差异：状态 / 这一段 / 文件里原来 / 改完以后 */
export function fileDiffGrid(diffRows) {
  const grid = new AlignGrid(["状态", "这一段", "文件里原来", "改完以后"]);
  const rows = diffRows || [];
  rows.forEach((row) => {
    grid.addRow(row.kind, [
      { text: KIND_TEXT[row.kind] || row.kind },
      { text: row.context, placeholder: "（文件开头附近）" },
      cell(row.before, "（原来没有这一行）"),
      cell(row.after, "（这一行会被删掉）"),
    ]);
  });
  if (!rows.length) grid.addEmpty("没有行级差异");
  return grid.node;
}

/** 「改了哪些字段」+「文件里改动的行」两个区块，标题也一起给出 */
export function diffSections(plan) {
  return [
    el("h4", { text: "改了哪些字段" }),
    changesGrid(plan.changes),
    el("h4", { text: "配置文件里改动的行" }),
    fileDiffGrid(plan.diff_rows),
  ];
}

/** 把一条补丁操作拆成「状态 / 位置 / 内容」，让清单和差异表用同一套列宽 */
function opColumns(op) {
  const kind = OP_KIND[op.op] || "modify";
  const pieces = [op.platform, op.model, op.alias];
  if (!pieces.some(Boolean) && String(op.op).includes("audit")) pieces.push("audit 段");
  if (op.field) pieces.push(op.field);

  const details = [op.op];
  if (op.value !== undefined) details.push(`= ${formatValue(op.value)}`);
  if (op.selectors !== undefined) details.push(`→ ${op.selectors.join(", ")}`);
  if (op.overrides && Object.keys(op.overrides).length) details.push(`覆盖 ${overridesText(op.overrides)}`);
  if (op.config) details.push(JSON.stringify(op.config));

  return { kind, path: pieces.filter(Boolean).join(" · ") || op.op, detail: details.join(" ") };
}

/** 取值格子：没有取值时给一句灰字说明，而不是留一个看不懂的空白 */
function cell(value, placeholder) {
  if (value === null || value === undefined || value === "") return { text: "", placeholder };
  return { text: formatValue(value) };
}

function changeKind(change) {
  const missing = (value) => value === null || value === undefined;
  if (missing(change.before)) return "add";
  if (missing(change.after)) return "remove";
  return "modify";
}
