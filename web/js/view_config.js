/**
 * 视图：模型配置
 *
 * 流程（与后端一致，避免「预演通过、写盘却不同」）：
 *   1. POST /admin/config/document + /admin/config/schema  → 渲染表单
 *   2. 用户改动 → 攒成补丁操作（ops）
 *   3. POST /admin/config/plan  → 看行级差异与校验结果
 *   4. POST /admin/config/apply → 带 If-Match 版本号写盘（冲突返回 409）
 * 写盘前后端一定会备份；插件/爬虫结构变化会标记 needs_restart，由用户自行重启。
 */

import { button, el, empty, fmtTime, issues, openTitles, panel, restoreTitles, setCollapsed, textInput } from "./ui.js";
import { addPlatformPanel, aliasPanel, auditPanel, fieldDictionary, platformPanel, removePlatformButton } from "./config_fields.js";
import { diffLegend, diffSections, stagedGrid } from "./diff_view.js";

// 改动刚加入清单时不要立刻打接口：连着点几条时合成一次预演
const LIVE_PLAN_DELAY_MS = 350;

export async function renderConfig(root, ctx) {
  const state = { document: null, schema: null, runtime: null, ops: [], plan: null, planFor: "" };
  let liveTimer = null;
  const stagedBox = el("div");
  const planBox = el("div");
  const platformsBox = el("div");

  const noteInput = textInput("", "写进备份元数据的备注");
  const planButton = button("重新预演", "ghost");
  const applyButton = button("应用并写盘");
  const clearButton = button("清空改动", "ghost");
  const reloadButton = button("让改动立即生效（进程内重载）");
  const expandButton = button("全部展开", "ghost");
  const collapseButton = button("全部折叠", "ghost");

  root.append(
    el("section", { id: "config-head" }),
    panel("待提交的改动", [stagedBox], el("div", { class: "row-actions" }, [planButton, applyButton, clearButton, noteInput])),
    planBox,
    el("div", { class: "row-actions" }, [
      el("span", { class: "muted", text: "下面每个平台与每一段配置都是折叠的，摘要就够判断要不要展开：" }),
      expandButton, collapseButton,
    ]),
    platformsBox,
    el("div", { id: "config-tail" }),
  );

  const stage = (op) => {
    state.ops.push(op);
    renderStaged();
    scheduleLivePlan();
  };

  const undoAt = (index) => {
    state.ops.splice(index, 1);
    renderStaged();
    scheduleLivePlan();
  };

  planButton.addEventListener("click", () => runPlan());
  applyButton.addEventListener("click", () => runApply());
  clearButton.addEventListener("click", () => {
    state.ops = [];
    renderStaged();
    showPlan(null);
  });
  expandButton.addEventListener("click", () => setCollapsed(root, true));
  collapseButton.addEventListener("click", () => setCollapsed(root, false));
  reloadButton.addEventListener("click", async () => {
    try {
      const result = await ctx.api.post("/admin/config/reload", {});
      ctx.notice(result.message || "已重载", "ok");
    } catch (error) {
      ctx.guard(error);
    }
  });

  function editables() {
    const editable = state.document.editable;
    const schema = state.schema;
    return {
      platformFields: editable.platform_fields,
      modelFields: editable.model_fields,
      auditFields: editable.audit_fields,
      kinds: { ...schema.platform_fields, ...schema.model_fields, ...schema.audit_fields },
      limits: schema.limits,
      // 只有有限几种取值的字段：界面给下拉，不让人在输入框里猜写法
      enums: schema.enums || {},
      // 字段字典按作用域给出：同一个 enabled 在平台级和 audit 段含义不同
      docs: schema.docs || {},
    };
  }

  // ---------- 读取 ----------

  async function refresh() {
    try {
      const [document, schema, runtime] = await Promise.all([
        ctx.api.postQuery("/admin/config/document"),
        ctx.api.postQuery("/admin/config/schema"),
        // 爬虫平台的模型只存在于进程里：屏蔽模型与别名指向的候选要从这里拿
        // 这一路失败不该让整个配置页读不出来：候选变少而已，页面照常可用
        ctx.api.postQuery("/admin/config/models").catch(() => ({
          loaded: false, total: 0, platforms: [], note: "运行时模型清单读不到，下拉里只有配置文件里的条目",
        })),
      ]);
      state.document = document;
      state.schema = schema;
      state.runtime = runtime;
      renderHead(document);
      renderForm();
      renderStaged();
    } catch (error) {
      ctx.guard(error);
    }
  }

  function renderHead(document) {
    const runtime = state.runtime || {};
    root.querySelector("#config-head").replaceChildren(panel("当前配置", [
      el("dl", { class: "kv" }, [
        dt("文件"), dd(document.path),
        dt("版本"), dd(document.version.slice(0, 12)),
        dt("大小"), dd(`${document.bytes} 字节`),
        dt("修改时间"), dd(fmtTime(document.modified_at)),
        // 爬虫平台的模型只在进程里：这一行说明下拉里为什么有（或没有）候选
        dt("进程里的模型"), dd(runtime.loaded ? `${runtime.total} 个（含插件灌进来的）` : (runtime.note || "未知")),
      ]),
      issues(document.errors, document.warnings),
      document.errors.length ? el("p", { class: "issue error", text: "当前配置文件已有硬错误：先修好再改，否则任何写盘都会被拒绝" }) : null,
    ]));
  }

  function renderForm() {
    const fields = editables();
    // 刷新（写盘、回滚、重载）之后保留用户原本展开着的区块，别把正在编辑的平台缩回去
    const keep = openTitles(root);

    platformsBox.replaceChildren(...state.document.platforms.map((platform) => platformPanel(
      platform, fields, stage, [removePlatformButton(platform, stage)], state.runtime,
    )));

    root.querySelector("#config-tail").replaceChildren(
      ...fieldDictionary(fields.docs, state.schema.sections),
      aliasPanel(state.document, stage, state.runtime),
      auditPanel(state.document.audit, fields, stage),
      addPlatformPanel(stage),
      el("p", { class: "muted", text: `支持的补丁操作：${state.document.editable.ops.join(" / ")}` }),
      el("p", { class: "muted", text: "注释保留规则：只改取值不会动注释；删除条目或整列表覆盖会丢掉附着在那些行上的注释（预演结果里会逐条列出）。" }),
    );

    restoreTitles(root, keep);
  }

  // ---------- 待提交清单 ----------

  function renderStaged() {
    if (!state.ops.length) {
      stagedBox.replaceChildren(empty("还没有改动：在下面的表单里改字段，然后点「加入改动」"));
      return;
    }
    stagedBox.replaceChildren(stagedGrid(state.ops, undoAt));
  }

  // ---------- 实时差异（只读预演，不写盘） ----------

  /** 改动一进清单就自动预演：差异按新增/修改/删除着色，不必先点按钮 */
  function scheduleLivePlan() {
    if (liveTimer) clearTimeout(liveTimer);
    liveTimer = setTimeout(() => {
      liveTimer = null;
      runPlan({ live: true });
    }, LIVE_PLAN_DELAY_MS);
  }

  function showPlan(plan) {
    state.plan = plan;
    state.planFor = plan ? opsSignature() : "";
    if (!plan) {
      planBox.replaceChildren();
      return;
    }
    planBox.replaceChildren(panel("实时差异（预演出来的，没有写盘）", renderPlanBody(plan)));
  }

  const opsSignature = () => JSON.stringify(state.ops);

  // ---------- 预演与写盘 ----------

  async function runPlan(options = {}) {
    if (!state.ops.length) {
      showPlan(null);
      if (!options.live) ctx.notice("没有改动可预演", "warn");
      return false;
    }
    planButton.disabled = true;
    try {
      const plan = await ctx.api.post("/admin/config/plan", { ops: state.ops, version: state.document.version });
      showPlan(plan);
      if (!options.live) {
        ctx.notice(plan.valid ? `预演通过：${plan.changes.length} 处变更` : `预演失败：${plan.errors.length} 处问题`, plan.valid ? "ok" : "bad");
      }
      return plan.valid;
    } catch (error) {
      handleConflict(error);
      return false;
    } finally {
      planButton.disabled = false;
    }
  }

  async function runApply() {
    if (!state.ops.length) return ctx.notice("没有改动可写盘", "warn");
    // 预演的必须是「即将写盘的那批改动」：清单在预演之后又变过时，先补一次预演
    if (!state.plan || !state.plan.valid || state.planFor !== opsSignature()) {
      const passed = await runPlan();
      if (!passed) return ctx.notice("预演没有通过：先按差异里的错误改好，再写盘", "bad");
    }
    applyButton.disabled = true;
    try {
      const applied = await ctx.api.post("/admin/config/apply", {
        ops: state.ops,
        version: state.document.version,
        note: noteInput.value.trim(),
        reload: true,
      });
      planBox.replaceChildren(panel("写盘结果", [
        el("dl", { class: "kv" }, [
          dt("备份"), dd(applied.backup),
          dt("新版本"), dd((applied.version_after || "").slice(0, 12)),
          dt("进程内重载"), dd(applied.reloaded ? "已重载" : "未重载"),
        ]),
        diffLegend(),
        ...diffSections(applied),
        issues(applied.errors, applied.warnings),
        applied.needs_restart
          ? el("p", { class: "issue warning", text: "这些改动涉及插件/爬虫结构，需要你自行重启服务才会完全生效。" })
          : el("p", { class: "issue", text: applied.message || "改动已生效。" }),
      ]));
      state.ops = [];
      await refresh();
      ctx.notice(`已写盘（旧内容备份在 ${applied.backup}）`, "ok");
    } catch (error) {
      handleConflict(error);
    } finally {
      applyButton.disabled = false;
    }
  }

  function handleConflict(error) {
    if (error.status === 409) {
      const actual = (error.detail && error.detail.actual) || "";
      ctx.notice(`配置在你编辑期间被别人改过（当前版本 ${actual.slice(0, 12)}）：已刷新页面，请重新提交改动`, "bad");
      state.ops = [];
      refresh();
      return;
    }
    ctx.guard(error);
    if (error.issues && error.issues.length) {
      planBox.replaceChildren(panel("校验未通过", [issues(error.issues, [])]));
    }
  }

  await refresh();
}

// ---------- 呈现细节 ----------

function renderPlanBody(plan) {
  return [
    el("dl", { class: "kv" }, [
      dt("变更数"), dd(plan.changes.length),
      dt("需要重启"), dd(plan.needs_restart ? "是" : "否"),
      dt("校验"), dd(plan.valid ? "通过" : "不通过"),
    ]),
    diffLegend(),
    ...diffSections(plan),
    issues(plan.errors, plan.warnings),
  ];
}

function dt(text) { return el("dt", { class: "muted", text }); }
function dd(value) { return el("dd", { text: value === null || value === undefined ? "-" : String(value) }); }
