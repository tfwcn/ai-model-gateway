/**
 * 取值控件：能列选项的字段一律不给自由输入框
 *
 * 存在理由：输入框会把长内容（别名指向、密钥占位符、屏蔽模式）显示成看不全的一截，
 * 而这些字段的取值要么有边界（数字）、要么本来就只有那么几个候选（bool、周期、selector）。
 *
 * 页面上没有「手填补充」这种入口：连爬虫平台的模型也从 /admin/config/models 给出候选。
 * 只有取值本身还不存在的字段（URL、密钥占位符、路径、新模型名）才留输入框。
 */

import { button, control, el, fieldGroup, select, textInput } from "./ui.js";

// 「这个字段不配置」在界面上的说法：走删除操作，回落到上层默认
export const NO_OPTION = { value: "", label: "不配置（删掉这一行，回落到上层）" };

const NUMERIC_KINDS = new Set(["int", "number", "seconds", "optional_seconds", "nonneg_int"]);
const INTEGER_KINDS = new Set(["int", "nonneg_int"]);

// 候选超过这么多就给出筛选框：爬虫平台一次灌进来几百个模型，裸下拉没法用
const FILTER_AT = 8;
// 下拉里最多放这么多项，剩下的靠筛选框缩小范围（否则下拉长到看不见头）
const MAX_OPTIONS = 40;

/**
 * 已选内容渲染成带 × 的标签
 *
 * 所有「一次只加一项」的控件共用这一份渲染：标签会换行，长取值（别名指向、模型名）
 * 不会被输入框截成一截看不全。
 *
 * Args:
 *   container: 放标签的容器
 *   entries:   [{key, text}]，key 给删除用，text 是显示出来的内容
 *   emptyText: 一项都没有时显示的说明
 *   onRemove:  (key) => void，点 × 时调用
 */
function renderChips(container, entries, emptyText, onRemove) {
  const nodes = entries.length
    ? entries.map((entry) => {
        const remove = button("×", "ghost");
        remove.setAttribute("aria-label", `去掉 ${entry.text}`);
        remove.addEventListener("click", () => onRemove(entry.key));
        return el("span", { class: "option-chip" }, [el("code", { text: entry.text }), remove]);
      })
    : [el("span", { class: "muted", text: emptyText })];
  container.replaceChildren(...nodes);
}

/**
 * 一次只加一项的列表输入框（新模型名这类挑不出候选的值用它）
 *
 * 存在理由：新增平台原本要在一行里写 `model-a, model-b`，写错逗号或多写空格都得重填。
 * 这里改成「填一个 → 加一项 → 变成带 × 的标签」，和别处的列表控件长得一样。
 *
 * Args:
 *   placeholder: 输入框提示
 *   initial:     已有的值
 *
 * Returns: {node, values, reset}
 */
export function chipsInput(placeholder, initial = []) {
  const chosen = new Map((initial || []).map((item) => [String(item), String(item)]));
  const input = textInput("", placeholder);
  const chips = el("div", { class: "option-chips" });
  const hint = el("span", { class: "muted", text: "" });

  function render() {
    renderChips(chips, [...chosen.keys()].map((item) => ({ key: item, text: item })),
      "（这里还没有条目）", (key) => {
        chosen.delete(key);
        render();
      });
  }

  function commit() {
    const item = input.value.trim();
    if (!item) {
      hint.textContent = "请先填一个名字";
      return;
    }
    chosen.set(item, item);
    input.value = "";
    hint.textContent = "";
    render();
  }

  const add = button("加一项", "ghost");
  add.addEventListener("click", commit);
  // 回车等于点「加一项」：连着填多个模型名时不必每次都去够按钮
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      commit();
    }
  });

  render();
  return {
    node: el("div", { class: "chips-input" }, [
      el("div", { class: "inline-form" }, [input, add, hint]),
      chips,
    ]),
    values: () => [...chosen.keys()],
    reset: () => {
      chosen.clear();
      render();
    },
  };
}

/**
 * 勾选式选项：已选内容用会换行的标签完整显示，新增从下拉里选
 *
 * 候选很多时（爬虫平台的模型是插件灌进来的，动辄上百个）自动多给一个筛选框：
 * 它只缩小下拉里的候选，永远不会变成取值。
 *
 * Args:
 *   available: [{value, label}] 全部候选（含已经选中的）
 *   selected:  当前已选的值
 *   note:      写在控件下面的一行说明（例如「通配模式要手改文件」）
 *
 * Returns: {node, values}，values() 给出当前选中列表
 */
export function optionPicker(available, selected = [], note = "") {
  const pool = (available || []).map((item) => ({ value: String(item.value), label: String(item.label || item.value) }));
  const chosen = (selected || []).map(String);
  let filter = "";

  const chips = el("div", { class: "option-chips" });
  // 下拉与「清空这一行」放在这个槽里反复重建；筛选框留在外面，输入时才不会失去焦点
  const selectSlot = el("div", { class: "option-select" });
  const hint = el("span", { class: "muted", text: note });

  const filterBox = pool.length > FILTER_AT ? textInput("", "筛选候选（只是筛选，不作为取值）") : null;
  if (filterBox) {
    filterBox.className = `${filterBox.className} option-filter`.trim();
    filterBox.addEventListener("input", () => {
      filter = filterBox.value.trim();
      render();
    });
  }

  const actions = el("div", { class: "option-actions" }, filterBox ? [filterBox, selectSlot] : [selectSlot]);
  const box = el("div", { class: "option-picker" }, [chips, actions, hint]);

  /** 还没被选走、且过筛选框这一关的候选 */
  function candidates() {
    const rest = pool.filter((item) => !chosen.includes(item.value));
    if (!filter) return rest;
    const needle = filter.toLowerCase();
    return rest.filter((item) => `${item.value} ${item.label}`.toLowerCase().includes(needle));
  }

  function render() {
    renderChips(chips, chosen.map((value) => ({ key: value, text: value })),
      "（这里还没有选中项）", (key) => {
        chosen.splice(chosen.indexOf(key), 1);
        render();
      });

    const rest = candidates();
    const shown = rest.slice(0, MAX_OPTIONS);
    const label = rest.length > MAX_OPTIONS
      ? `选一个加进来…（共 ${rest.length} 个候选，只列前 ${MAX_OPTIONS} 个，用筛选框缩小范围）`
      : "选一个加进来…";
    const node = select([{ value: "", label }].concat(shown), "");
    node.addEventListener("change", () => {
      const value = node.value;
      if (!value || chosen.includes(value)) return;
      chosen.push(value);
      render();
    });

    const row = [node];
    if (chosen.length) {
      const clear = button("清空这一行", "ghost");
      clear.addEventListener("click", () => {
        chosen.length = 0;
        render();
      });
      row.push(clear);
    }
    selectSlot.replaceChildren(...row);
    hint.textContent = filter
      ? `${note ? `${note} ｜ ` : ""}筛出 ${rest.length} 个候选（筛选框只筛候选，不会写进配置）`
      : note;
  }

  render();
  return { node: box, values: () => chosen.slice() };
}

/**
 * 按字段类型建一个取值控件
 *
 * Args:
 *   spec.kind:       后端给的字段类型（bool / int / seconds / list_str / url / secret ...）
 *   spec.limit:      {min, max} 或 null
 *   spec.enumDoc:    {values: [...], allow_none: true} 或 null（有限取值 → 下拉）
 *   spec.current:    这个字段现在的取值（填进控件，避免「框里空着、看不见现状」）
 *   spec.available:  列表型字段的候选项（屏蔽模型勾的是「配置文件里的条目 + 进程里当前可用的模型」）
 *   spec.pickNote:   列表型字段下面的一行说明（例如通配模式要手改文件）
 *   spec.placeholder: 只有取值本身还不存在的字段（URL、密钥占位符、路径）才用到的输入框提示
 *
 * Returns: {node, read}；read() 返回 {value} | {unset: true} | {skip: true, reason}
 */
export function valueControl(spec) {
  const { kind, limit = null, enumDoc = null, current, available = [], placeholder = "新取值" } = spec;

  if (enumDoc) {
    const items = enumDoc.allow_none ? [NO_OPTION] : [];
    (enumDoc.values || []).forEach((value) => items.push({ value, label: value }));
    const initial = items.some((item) => item.value === String(current ?? "")) ? String(current) : items[0].value;
    const node = select(items, initial);
    return {
      node,
      read: () => (node.value === "" ? { unset: true } : { value: node.value }),
    };
  }

  if (kind === "bool") {
    const node = select([{ value: "true", label: "是（true）" }, { value: "false", label: "否（false）" }],
      current === false ? "false" : "true");
    return { node, read: () => ({ value: node.value === "true" }) };
  }

  if (kind === "list_str") {
    // 已选的模式用标签完整显示；新的从候选里勾（候选来自配置文件 + 进程里当前可用的模型）
    const picker = optionPicker(available, current, spec.pickNote || "");
    return {
      node: picker.node,
      read: () => ({ value: picker.values() }),
    };
  }

  if (NUMERIC_KINDS.has(kind)) {
    const node = el("input", {
      type: "number",
      class: "num",
      value: current === null || current === undefined ? "" : String(current),
      min: limit ? String(limit.min) : null,
      max: limit ? String(limit.max) : null,
      step: INTEGER_KINDS.has(kind) ? "1" : "0.1",
      placeholder: limit ? `${limit.min} ~ ${limit.max}` : "数字",
    });
    return {
      node,
      read: () => {
        const text = node.value;
        if (text === "") return { skip: true, reason: "请先填新取值" };
        const num = Number(text);
        return Number.isFinite(num) ? { value: num } : { skip: true, reason: "这里要填数字" };
      },
    };
  }

  // 剩下的确实只能手填：URL、密钥占位符、路径
  const node = textInput(current === null || current === undefined ? "" : String(current), placeholder);
  node.className = "wide";
  return {
    node,
    read: () => (node.value === "" ? { skip: true, reason: "请先填新取值" } : { value: node.value }),
  };
}

/**
 * 覆盖项挑选器：新增条目时用它，字段名与取值全部走选项
 *
 * 存在理由：新增模型条目原本靠一行 `weight=5, timeout=30` 的自由文本，
 * 字段名与边界都要照着文档抄，抄错就在预演阶段被拒。这里复用同一套取值控件。
 *
 * Args:
 *   fields:  可覆盖的字段名列表
 *   kinds:   字段名 → 类型
 *   options: {limits, enums} 后端 schema 给的边界与取值选项
 *
 * Returns: {node, values, reset}；values() 给出覆盖对象，reset() 清空已选覆盖
 */
export function overridesPicker(fields, kinds, { limits = {}, enums = {} } = {}) {
  const chosen = new Map();
  const fieldSelect = select(fields.map((field) => ({ value: field, label: field })), fields[0]);
  const slot = el("div", { class: "value-slot" });
  const chips = el("div", { class: "option-chips" });
  const hint = el("span", { class: "muted", text: "" });
  let active = valueControl({ kind: "str" });

  function syncField() {
    const field = fieldSelect.value;
    active = valueControl({
      kind: kinds[field] || "str",
      limit: limits[field] || null,
      enumDoc: enums[field] || null,
    });
    slot.replaceChildren(active.node);
  }

  fieldSelect.addEventListener("change", syncField);
  syncField();

  function renderChosen() {
    renderChips(chips, [...chosen.entries()].map(([field, value]) => ({ key: field, text: `${field}=${value}` })),
      "不带覆盖（全部继承平台级取值）", (field) => {
        chosen.delete(field);
        renderChosen();
      });
  }

  renderChosen();

  const addOne = button("加一项覆盖", "ghost");
  addOne.addEventListener("click", () => {
    const field = fieldSelect.value;
    const result = active.read();
    if (result.skip) {
      hint.textContent = result.reason || "请先选取值";
      return;
    }
    if (result.unset) {
      // 选「不配置」就是不写这一行：让条目继承平台级，而不是往文件里写 null
      chosen.delete(field);
      renderChosen();
      hint.textContent = `${field} 不写覆盖，继承平台级`;
      return;
    }
    chosen.set(field, result.value);
    renderChosen();
    hint.textContent = "";
  });

  return {
    node: el("div", { class: "overrides-picker" }, [
      el("div", { class: "control-row" }, [
        control("要覆盖的字段", fieldSelect),
        fieldGroup("新取值", slot),
        addOne,
        hint,
      ]),
      chips,
    ]),
    values: () => Object.fromEntries(chosen),
    reset: () => {
      chosen.clear();
      renderChosen();
    },
  };
}
