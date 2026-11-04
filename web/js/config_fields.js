/**
 * 配置编辑的表单部件：把「用户改了哪个字段」翻译成补丁操作
 *
 * 前端不做最终校验（完整规则只有后端知道）：这里只把控件里的取值拼成 JSON，
 * 边界与合法性由 /admin/config/plan 与 /admin/config/apply 回报。
 * 取值控件全部来自 option_controls.js：能列选项的字段不给自由输入框。
 */

import { button, collapsible, control, el, empty, fieldGroup, select, textInput } from "./ui.js";
import { chipsInput, optionPicker, overridesPicker, valueControl } from "./option_controls.js";

/** 只有「取值本身还不存在」的字段才给输入框：按字段类型给例子，免得猜写法 */
function placeholderFor(kind) {
  if (kind === "url") return "https://api.example.com/v1";
  if (kind === "secret") return "${EXAMPLE_PLATFORM_API_KEY}";
  return "新取值";
}

/** 列表型字段下面那句说明：通配模式挑不出来，说清去哪儿改 */
const LIST_NOTE = "只能勾这里给出的模型；要按通配屏蔽（例如 prefix-*）就在 models.yaml 里手改这一行";

/**
 * 「选字段 → 选/填新取值 → 加入改动」的编辑行
 *
 * Args:
 *   fields: 可选字段名列表
 *   kinds:  字段名 → 类型
 *   options: {
 *     stage:      (op) => void，把改动放进待提交清单
 *     buildOp:    (field, value) => 设置该字段的补丁操作
 *     buildUnset: (field) => 删除该字段的补丁操作；传入时多一个「删除该字段」按钮
 *     limits:     字段名 → {min, max}
 *     enums:      字段名 → {values, allow_none}（有限取值 → 下拉，不让人猜写法）
 *     docs:       字段名 → 字段字典条目（来自 /admin/config/schema 的 docs）
 *     currentFor: (field) => 这个字段现在的取值（填进控件，别让用户对着空框猜现状）
 *     availableFor: (field) => 列表型字段的候选项 [{value, label}]
 *     pickNoteFor:  (field) => 列表型字段下面的一行说明（例如通配模式要手改文件）
 *     noteFor:    (field) => 一行「现在是多少 / 从哪儿继承」
 *   }
 */
export function fieldEditor(fields, kinds, options) {
  const {
    stage, buildOp, limits = {}, enums = {}, docs = {},
    buildUnset = null, currentFor = () => undefined, availableFor = () => [],
    pickNoteFor = () => "", noteFor = () => "",
  } = options;

  const fieldSelect = select(fields.map((field) => ({ value: field, label: field })), fields[0]);
  const valueSlot = el("div", { class: "value-slot" });
  const hint = el("span", { class: "muted", text: "" });
  const docBox = el("div", { class: "field-doc" });
  let active = valueControl({ kind: "str" });

  function syncField() {
    const field = fieldSelect.value;
    const kind = kinds[field] || "str";
    active = valueControl({
      kind,
      limit: limits[field] || null,
      enumDoc: enums[field] || null,
      current: currentFor(field),
      available: availableFor(field),
      pickNote: pickNoteFor(field),
      placeholder: placeholderFor(kind),
    });
    valueSlot.replaceChildren(active.node);

    const limit = limits[field];
    const notes = [
      kind === "bool" || !limit ? "" : `取值范围 ${limit.min} ~ ${limit.max}`,
      noteFor(field),
    ].filter(Boolean).join(" ｜ ");
    hint.textContent = notes;
    // 选到哪个字段就换上哪个字段的说明：这一页本身就是文档
    docBox.replaceChildren(...docNodes(docs[field], field));
  }

  fieldSelect.addEventListener("change", syncField);
  syncField();

  const addButton = button("加入改动", "primary");
  addButton.addEventListener("click", () => {
    const field = fieldSelect.value;
    const result = active.read();
    if (result.skip) {
      hint.textContent = result.reason || "请先填新取值";
      return;
    }
    if (result.unset) {
      // 「不配置」优先走删除：回落到上层默认，而不是往文件里写一个 null
      if (buildUnset) {
        stage(buildUnset(field));
        hint.textContent = "已加入待提交清单（删掉这一行，回落到上层）";
      } else {
        stage(buildOp(field, null));
        hint.textContent = "已加入待提交清单（写 null，即不配置）";
      }
      return;
    }
    stage(buildOp(field, result.value));
    hint.textContent = "已加入待提交清单";
  });

  const nodes = [fieldSelect, valueSlot, hint, addButton];

  if (buildUnset) {
    // 删除字段 = 回落到上层默认（模型回落到平台、平台回落到全局默认）
    const unsetButton = button("删除该字段（回落）", "ghost");
    unsetButton.addEventListener("click", () => {
      stage(buildUnset(fieldSelect.value));
      hint.textContent = "已加入待提交清单";
    });
    nodes.push(unsetButton);
  }

  return el("div", { class: "editor-block" }, [el("div", { class: "inline-form" }, nodes), docBox]);
}

/** 一个字段说明的节点组：名称 + 一句话 + 默认/取值/生效 + 写法示例 + 注意点 */
function docNodes(doc, field) {
  if (!doc) {
    return [el("span", { class: "muted", text: `${field}：字典里还没有这一条（后端 field_docs.py 补上就会显示）` })];
  }
  return [
    el("div", {}, [el("b", { text: doc.label }), el("span", { text: ` ${doc.summary}` })]),
    el("div", { class: "muted", text: `默认：${doc.default} ｜ 取值：${doc.unit} ｜ 改完 ${doc.effect}生效` }),
    el("div", {}, [el("span", { class: "muted", text: "写法 " }), el("code", { text: doc.example })]),
    doc.note ? el("div", { class: "doc-note", text: doc.note }) : null,
  ].filter(Boolean);
}

/**
 * 字段字典 + 配置结构说明：这一页本身就是文档，不必再去翻 md
 *
 * 每个作用域一个折叠块（而不是一整张六列大表）：整表展开时页面会长到看不清，
 * 卡片式排版在窄屏也能换行。
 *
 * Args:
 *   docs:     {platform: {字段: 说明}, model: {...}, audit: {...}}
 *   sections: [{title, body}]，来自后端 config_guide.py
 *
 * Returns: 折叠区块数组（调用方用展开运算符塞进容器）
 */
export function fieldDictionary(docs, sections) {
  const groups = [
    { scope: "platform", title: "字段字典：平台级字段", meta: "写在平台条目那一层" },
    { scope: "model", title: "字段字典：模型级可覆盖字段", meta: "写在模型条目里，覆盖平台级取值" },
    { scope: "audit", title: "字段字典：audit 段字段", meta: "写在 models.yaml 的 audit 段" },
    { scope: "passthrough", title: "字段字典：passthrough 段字段", meta: "写在透传路由里" },
  ];

  const blocks = groups.map((group) => {
    const entries = (docs || {})[group.scope] || {};
    const count = Object.keys(entries).length;
    return collapsible(group.title, [docGrid(entries)], {
      meta: `${count} 个字段 · ${group.meta}`,
      open: false,
    });
  });

  const guide = (sections || []).flatMap((section) => [
    el("h4", { text: section.title }),
    el("pre", { class: "guide-body", text: section.body }),
  ]);
  blocks.push(collapsible("配置结构说明", guide, {
    meta: "配置文件里哪几层、模型条目的两种写法、改动什么时候生效",
    open: false,
  }));

  return blocks;
}

/** 字段字典的卡片网格：窄屏自动换成单列，不会出现横向滚动 */
function docGrid(entries) {
  const names = Object.keys(entries || {});
  if (!names.length) return empty("这一段还没有字段字典");

  return el("div", { class: "doc-grid" }, names.map((field) => {
    const doc = entries[field];
    return el("div", { class: "doc-card" }, [
      el("div", { class: "doc-head" }, [
        el("code", { text: field }),
        el("span", { class: "chip", text: `${doc.effect}生效` }),
      ]),
      el("div", {}, [el("b", { text: doc.label }), el("span", { text: ` ${doc.summary}` })]),
      el("div", { class: "doc-meta", text: `默认：${doc.default} ｜ 取值：${doc.unit}` }),
      el("div", {}, [el("span", { class: "muted", text: "写法 " }), el("code", { text: doc.example })]),
      doc.note ? el("div", { class: "doc-note", text: doc.note }) : null,
    ].filter(Boolean));
  }));
}

// ---------- 平台 ----------

/**
 * Args:
 *   editables: {platformFields, modelFields, auditFields, kinds, limits, enums, docs}
 *              由 /admin/config/document 的 editable 与 /admin/config/schema 合并而来
 *   runtimeNames: 进程里这个平台当前正在用的模型条目名（爬虫平台的模型只存在于这里）
 */
export function platformPanel(platform, editables, stage, actions = [], runtime = null) {
  const apiKey = platform.apiKey || {};
  const runtimeNames = runtimeNamesOf(runtime, platform.name);
  const rows = [
    el("dl", { class: "kv" }, [
      key("入口"), val(platform.baseUrl),
      key("密钥"), val(`${apiKey.env || "-"}（${apiKey.env_set ? "已设置" : "未设置"}，写法 ${apiKey.kind || "missing"}）`),
      key("权重"), val(platform.weight),
      key("超时"), val(`${platform.timeout}s，重试 ${platform.retry_count} 次`),
      key("首块/探测"), val(`${formatValue(platform.first_chunk_timeout)} / ${formatValue(platform.stream_probe_timeout)}`),
      key("屏蔽模型"), val((platform.blacklist || []).join(", ") || "-"),
      key("插件"), val(platform.plugin ? `${platform.plugin.code}${platform.plugin.scraper ? "（爬虫模式：模型由插件灌入）" : ""}` : "-"),
    ]),
    fieldEditor(editables.platformFields, editables.kinds, {
      stage,
      limits: editables.limits,
      enums: editables.enums,
      docs: (editables.docs || {}).platform,
      buildOp: (field, value) => ({ op: "set_platform_field", platform: platform.name, field, value }),
      buildUnset: (field) => ({ op: "unset_platform_field", platform: platform.name, field }),
      currentFor: (field) => currentValue(platform, field),
      // 屏蔽模型的候选：配置里写着的条目 + 进程里正在用的条目（爬虫平台只有后者）
      availableFor: (field) => (field === "blacklist" ? modelOptions(platform, runtimeNames) : []),
      pickNoteFor: (field) => (field === "blacklist" ? LIST_NOTE : ""),
    }),
    modelRows(platform, editables, stage, runtimeNames),
  ];

  // 平台多的时候一屏全是表单：默认折叠，靠摘要判断要不要展开
  return collapsible(`平台：${platform.name}`, rows, {
    meta: platformMeta(platform, apiKey),
    actions,
    open: false,
  });
}

/** 折叠状态下也要一眼看清这个平台现在是什么情况 */
function platformMeta(platform, apiKey) {
  const bits = [
    `权重 ${formatValue(platform.weight)}`,
    `${formatValue(platform.timeout)}s · 重试 ${formatValue(platform.retry_count)}`,
    `模型 ${(platform.models || []).length}`,
  ];
  if ((platform.blacklist || []).length) bits.push(`屏蔽 ${platform.blacklist.length}`);
  if (apiKey.env && !apiKey.env_set) bits.push(`环境变量 ${apiKey.env} 未设置`);
  if (platform.plugin && platform.plugin.scraper) bits.push("爬虫模式");
  if (platform.enabled === false) bits.push("已停用");
  return bits.join(" · ");
}

/** 下拉里当前选中的模型条目 */
function selectedModel(models, target) {
  return models.find((model) => model.name === target.value) || models[0];
}

/**
 * 控件里回填「这个字段现在是什么」
 *
 * 密钥只在写成 ${ENV} 占位符时回填占位符本身：管理台永远不会拿到真实密钥。
 */
function currentValue(entry, field) {
  if (field === "apiKey") {
    const apiKey = entry.apiKey || {};
    return apiKey.kind === "env_placeholder" ? "${" + apiKey.env + "}" : "";
  }
  return entry[field];
}

/** 模型级取值：条目里写了就用条目，没写就是继承平台 */
function modelCurrentValue(platform, model, field) {
  const overrides = model.overrides || {};
  return field in overrides ? overrides[field] : platform[field];
}

function modelCurrentValueNote(platform, model, field) {
  const overrides = model.overrides || {};
  return field in overrides
    ? `这个条目现在写的是 ${formatValue(overrides[field])}`
    : `这个条目没写，继承平台的 ${formatValue(platform[field])}`;
}

/**
 * 屏蔽模型的候选：配置文件里写着的条目 + 进程里当前正在用的条目（爬虫平台只有后者）
 *
 * runtime 是 /admin/config/models 的返回：`{platforms: [{name, models: [{name, selector}]}]}`
 */
function runtimeNamesOf(runtime, platformName) {
  const entry = ((runtime || {}).platforms || []).find((platform) => platform.name === platformName);
  return ((entry || {}).models || []).map((model) => model.name);
}

/** 运行时清单里的全部 selector：插件灌进来的模型只有进程知道，别名指向靠它才有候选 */
function runtimeSelectors(runtime) {
  return ((runtime || {}).platforms || []).flatMap((platform) => (platform.models || []).map((model) => model.selector));
}

function modelOptions(platform, runtimeNames = []) {
  const names = new Map();
  (platform.models || []).forEach((model) => names.set(model.name, "配置文件里的条目"));
  (runtimeNames || []).forEach((name) => {
    if (!names.has(name)) names.set(name, "插件灌进来的条目");
  });
  return [...names.entries()].map(([name, source]) => ({ value: name, label: `屏蔽 ${name}（${source}）` }));
}

function modelRows(platform, editables, stage, runtimeNames = []) {
  const models = platform.models || [];
  // 「新增模型条目」放在小节最前面：它是这里最常用的动作，不该压在最后一行
  const nodes = [el("h4", { text: `模型条目（${models.length}）` }), addModelRow(platform, editables, stage)];

  if (models.length) {
    const target = select(models.map((model) => ({ value: model.name, label: model.selector || model.name })), models[0].name);

    nodes.push(table2(models));
    // 选择与删除放同一行：一眼看出这两个动作针对的是同一个模型（按底边对齐，按钮不会卡在标签与下拉框中间）
    nodes.push(el("div", { class: "control-row" }, [
      control("选择模型", target),
      removeModelButton(platform, target),
    ]));
    nodes.push(fieldEditor(editables.modelFields, editables.kinds, {
      stage,
      limits: editables.limits,
      enums: editables.enums,
      docs: (editables.docs || {}).model,
      buildOp: (field, value) => ({ op: "set_model_field", platform: platform.name, model: target.value, field, value }),
      buildUnset: (field) => ({ op: "unset_model_field", platform: platform.name, model: target.value, field }),
      currentFor: (field) => modelCurrentValue(platform, selectedModel(models, target), field),
      noteFor: (field) => modelCurrentValueNote(platform, selectedModel(models, target), field),
    }));
  } else if (runtimeNames.length) {
    nodes.push(empty(`这个平台没有静态模型条目：进程里现在有 ${runtimeNames.length} 个插件灌进来的模型，只能用「屏蔽模型」勾掉，不能删条目`));
  } else {
    nodes.push(empty("这个平台没有静态模型条目（爬虫模式的模型由插件灌入，不在此列）"));
  }

  return el("div", {}, nodes);
}

/**
 * 新增静态模型条目
 *
 * 只有模型名需要打字，覆盖一律走选项（字段下拉 + 取值控件 + 已选覆盖标签）：
 * 一条 op 就能「新增并带上覆盖」，不必先新增再刷新页面二次编辑，也不用照着文档抄 `weight=5, timeout=30`。
 */
function addModelRow(platform, editables, stage) {
  const nameInput = textInput("", "模型名，例如 model-new");
  const picker = overridesPicker(editables.modelFields, editables.kinds, {
    limits: editables.limits,
    enums: editables.enums,
  });
  const hint = el("span", { class: "muted", text: "" });
  const add = button("新增模型条目", "primary");

  add.addEventListener("click", () => {
    const model = nameInput.value.trim();
    if (!model) {
      hint.textContent = "请先填模型名";
      return;
    }
    stage({ op: "add_model", platform: platform.name, model, overrides: picker.values() });
    picker.reset();
    // 保留模型名：连着加下一条时不必再敲一遍
    hint.textContent = "已加入待提交清单（加多个模型就多点几次）";
  });

  return el("div", { class: "model-add" }, [
    el("div", { class: "control-row" }, [control("模型名", nameInput), hint, add]),
    picker.node,
  ]);
}

function removeModelButton(platform, target) {
  const node = button("删除选中的模型条目", "ghost");
  node.addEventListener("click", () => stage({ op: "remove_model", platform: platform.name, model: target.value }));
  return node;
}

function table2(models) {
  const rows = models.map((model) => el("tr", {}, [
    el("td", {}, el("code", { text: model.selector || model.name })),
    el("td", { class: "muted" }, overridesText(model.overrides)),
    el("td", { class: "muted" }, model.writable ? "可覆盖" : "字符串写法（覆盖后会变成对象条目）"),
  ]));
  return el("div", { class: "table-scroll" }, [el("table", { class: "wrap" }, [
    el("thead", {}, el("tr", {}, [el("th", {}, "模型"), el("th", {}, "模型级覆盖"), el("th", {}, "条目写法")])),
    el("tbody", {}, rows),
  ])]);
}

export function overridesText(overrides) {
  const entries = Object.entries(overrides || {});
  if (!entries.length) return "继承平台取值";
  return entries.map(([field, value]) => `${field}=${formatValue(value)}`).join("  ");
}

export function formatValue(value) {
  if (value === null || value === undefined) return "（未设置）";
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

// ---------- 别名 ----------

/**
 * 别名指向的全部合法写法：all / 平台名 / 平台名|模型名
 *
 * 候选全部枚举出来（配置文档里的条目 + 进程里正在用的条目），所以给下拉而不是让人拼字符串
 * （输入框里的长指向列表还会被截断成看不全的一截）。
 */
function selectorOptions(document, runtimeNames = []) {
  const options = [{ value: "all", label: "整组：all（所有平台的所有模型）" }];
  const seen = new Set(["all"]);
  const push = (value, label) => {
    if (seen.has(value)) return;
    seen.add(value);
    options.push({ value, label });
  };

  (document.platforms || []).forEach((platform) => {
    push(platform.name, `整个平台：${platform.name}`);
    (platform.models || []).forEach((model) => {
      const selector = model.selector || `${platform.name}|${model.name}`;
      push(selector, `单个模型：${selector}`);
    });
  });
  // 插件灌进来的模型不在配置文档里，它们的候选只能从进程里拿
  (runtimeNames || []).forEach((selector) => push(selector, `插件灌进来的模型：${selector}`));
  return options;
}

/**
 * 指向编辑器：已选指向用会换行的标签完整显示，新的从下拉里挑
 *
 * 没有手填入口：连插件灌进来的模型也从 /admin/config/models 给出了候选。
 */
function selectorEditor(options, selected) {
  const picker = optionPicker(options, selected, "指向只能从这些候选里挑；候选来自配置文档 + 进程里当前可用的模型");
  return { node: picker.node, values: picker.values };
}

export function aliasPanel(document, stage, runtime = null) {
  const aliases = document.aliases || [];
  const options = selectorOptions(document, runtimeSelectors(runtime));

  const blocks = aliases.map((alias) => {
    const editor = selectorEditor(options, alias.selectors);
    const apply = button("覆盖别名指向");
    apply.addEventListener("click", () => stage({
      op: "set_alias_selectors",
      alias: alias.name,
      selectors: editor.values(),
    }));
    // 网格排版：别名一列、指向一列、按钮一列，多行不会参差不齐
    return el("div", { class: "alias-row" }, [el("code", { text: alias.name }), editor.node, apply]);
  });

  // 别名（模型组昵称）就在这一节：没有别名时也留了新增的入口
  blocks.push(addAliasRow(options, stage));
  return collapsible(`模型组别名（${aliases.length}）`, blocks, {
    meta: "指向从下拉里挑；整列表覆盖会丢掉附着在这些行上的注释",
    open: false,
  });
}

/**
 * 新增别名
 *
 * 与「覆盖别名指向」用的是同一条 op：别名不存在时后端会把它建出来，
 * 所以不需要单独的 add_alias 操作。
 */
function addAliasRow(options, stage) {
  const nameInput = textInput("", "别名，例如 cheap");
  const editor = selectorEditor(options, []);
  const hint = el("span", { class: "muted", text: "" });
  const add = button("新增别名", "primary");

  add.addEventListener("click", () => {
    const alias = nameInput.value.trim();
    const selectors = editor.values();
    if (!alias || !selectors.length) {
      hint.textContent = "别名和指向都不能空";
      return;
    }
    stage({ op: "set_alias_selectors", alias, selectors });
    hint.textContent = "已加入待提交清单";
  });

  return el("div", { class: "alias-row" }, [nameInput, editor.node, add, hint]);
}

// ---------- audit 段 ----------

export function auditPanel(audit, editables, stage) {
  const current = audit || {};
  return collapsible("审计段（audit）", [
    el("dl", { class: "kv" }, [
      key("审计库"), val(current.db_path),
      key("保留天数"), val(current.retention_days),
      key("只记录失败"), val(current.record_failed_only),
      key("刷盘"), val(`每 ${formatValue(current.flush_interval)}s 或攒够 ${formatValue(current.batch_size)} 行`),
    ]),
    fieldEditor(editables.auditFields, editables.kinds, {
      stage,
      limits: editables.limits,
      enums: editables.enums,
      docs: (editables.docs || {}).audit,
      buildOp: (field, value) => ({ op: "set_audit_field", field, value }),
      // audit 段没有 unset 操作（OPS 里只有 set_audit_field）：所以这里不给「删除该字段」按钮
      currentFor: (field) => current[field],
    }),
  ], { meta: auditMeta(current), open: false });
}

function auditMeta(audit) {
  const enabled = audit.enabled === undefined ? "默认开启" : (audit.enabled ? "已开启" : "已关闭");
  return `${enabled} · 保留 ${formatValue(audit.retention_days)} 天`;
}

// ---------- 透传路由 ----------

export function passthroughPanel(routes, editables, stage) {
  const items = routes || [];
  const blocks = items.map((route) => {
    const apiKey = route.apiKey || {};
    const rows = [
      el("dl", { class: "kv" }, [
        key("前缀"), val(route.prefix),
        key("上游"), val(route.upstream),
        key("密钥"), val(`${apiKey.env || "-"}（${apiKey.env_set ? "已设置" : "未设置"}，写法 ${apiKey.kind || "missing"}）`),
        key("超时"), val(`${formatValue(route.timeout)}s，连接 ${formatValue(route.connect_timeout)}s`),
        key("维度"), val((Object.keys(route.dimensions || {}) || []).join(", ") || "-"),
      ]),
      fieldEditor(editables.passthroughFields, editables.kinds, {
        stage,
        limits: editables.limits,
        docs: (editables.docs || {}).passthrough,
        buildOp: (field, value) => ({ op: "set_passthrough_field", name: route.name, field, value }),
        buildUnset: (field) => ({ op: "unset_passthrough_field", name: route.name, field }),
        currentFor: (field) => passthroughCurrentValue(route, field),
        availableFor: (field) => (field === "forward_headers" ? passthroughHeaderOptions(route) : []),
        pickNoteFor: (field) => (field === "forward_headers"
          ? "自定义头名需要手改 models.yaml；Authorization/Cookie/Host 永远不会转发"
          : ""),
      }),
      el("p", { class: "muted", text: "维度键名就是汇总时的分组名；model 维度会覆盖默认的路由名。" }),
    ];
    return collapsible(`透传：${route.name}`, rows, {
      meta: `${route.prefix} → ${route.upstream} · ${Object.keys(route.dimensions || {}).length} 个维度`,
      actions: [removePassthroughButton(route, stage)],
      open: false,
    });
  });

  blocks.push(addPassthroughPanel(stage));
  return collapsible(`透传接口（${items.length}）`, blocks, {
    meta: "对外前缀统一 /proxy/；请求体、查询串与指定请求头原样转发",
    open: false,
  });
}

function passthroughCurrentValue(route, field) {
  if (field === "apiKey") {
    const apiKey = route.apiKey || {};
    return apiKey.kind === "env_placeholder" ? "${" + apiKey.env + "}" : "";
  }
  return route[field];
}

/** 允许额外转发的常见请求头候选；自定义头仍需手改 YAML */
function passthroughHeaderOptions(route) {
  const common = ["accept", "content-type", "user-agent", "accept-language", "x-request-id", "traceparent"];
  const selected = route.forward_headers || [];
  return [...new Set([...common, ...selected])].map((name) => ({ value: name, label: name }));
}

function removePassthroughButton(route, stage) {
  const node = button("删除透传路由", "ghost");
  node.addEventListener("click", () => {
    if (window.confirm(`确认删除透传路由 ${route.name}？删除前会先备份当前配置。`)) {
      stage({ op: "remove_passthrough", name: route.name });
    }
  });
  return node;
}

export function addPassthroughPanel(stage) {
  const name = textInput("", "路由名，例如 example-service");
  const upstream = textInput("", "https://api.example.com/v1");
  const apiKey = textInput("", "${EXAMPLE_PLATFORM_API_KEY}");
  const prefix = textInput("", "留空默认 /proxy/<路由名>");
  const dimensions = valueControl({ kind: "map_str", current: {} });
  const hint = el("span", { class: "muted", text: "" });
  const add = button("新增透传路由", "primary");

  add.addEventListener("click", () => {
    const routeName = name.value.trim();
    const upstreamValue = upstream.value.trim();
    if (!routeName || !upstreamValue) {
      hint.textContent = "路由名与 upstream 不能空";
      return;
    }
    const config = {
      upstream: upstreamValue,
      apiKey: apiKey.value.trim(),
      dimensions: dimensions.read().value,
    };
    if (prefix.value.trim()) config.prefix = prefix.value.trim();
    stage({ op: "add_passthrough", name: routeName, config });
    hint.textContent = "已加入待提交清单";
  });

  return collapsible("新增透传路由", [
    el("div", { class: "form-grid" }, [
      control("路由名", name),
      control("upstream", upstream),
      control("密钥占位符", apiKey),
      control("对外前缀", prefix),
    ]),
    fieldGroup("维度（键名 → body./query./header./path. 表达式）", dimensions.node),
    el("div", { class: "control-row" }, [add, hint]),
  ], { meta: "新增后热重载即可生效，不需要重启", open: false });
}


export function addPlatformPanel(stage) {
  const name = textInput("", "example-platform");
  const baseUrl = textInput("", "https://api.example.com/v1");
  const apiKey = textInput("", "${EXAMPLE_PLATFORM_API_KEY}");
  // 新平台的模型名挑不出候选（这个平台还不存在），但仍然一次只填一个：不让人写 `a, b, c` 这种容易写错的列表
  const models = chipsInput("模型名，例如 model-a");
  const add = button("加入新增平台");

  add.addEventListener("click", () => {
    stage({
      op: "add_platform",
      platform: name.value.trim(),
      config: {
        baseUrl: baseUrl.value.trim(),
        apiKey: apiKey.value.trim(),
        models: models.values(),
      },
    });
  });

  return collapsible("新增平台", [
    el("div", { class: "form-grid" }, [
      control("平台名", name),
      control("baseUrl", baseUrl),
      control("密钥占位符", apiKey),
    ]),
    // 模型条目单独一行：它自己就有「输入框 + 加一项 + 已填标签」三层，挤进栅格会把整行拉歪
    fieldGroup("模型条目（一次填一个）", models.node),
    el("div", { class: "control-row" }, [control("操作", add)]),
  ], { meta: "新增/删除平台与插件结构都需要你自行重启服务", open: false });
}

export function removePlatformButton(platform, stage) {
  const node = button("删除平台", "ghost");
  node.addEventListener("click", () => {
    if (window.confirm(`确认删除平台 ${platform.name}？删除前会先备份当前配置。`)) {
      stage({ op: "remove_platform", platform: platform.name });
    }
  });
  return node;
}

function key(text) { return el("dt", { class: "muted", text }); }
function val(value) { return el("dd", { text: value === null || value === undefined ? "-" : String(value) }); }
