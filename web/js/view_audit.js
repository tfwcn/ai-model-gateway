/**
 * 视图：审计明细
 *
 * 数据来源：
 *   POST /admin/audit                  明细下钻（一次上游尝试一行）
 *   POST /admin/audit/chain/{id}       一次请求的完整故障转移链
 *   POST /admin/audit/stats            审计自身健康状态
 * 点击任意一行会展开它所属请求的整条链路。
 */

import { button, control, el, empty, fmtMs, fmtNumber, fmtTime, panel, readControls, select, table, textInput, userLabel } from "./ui.js";

const SINCE_OPTIONS = [
  { value: "24h", label: "最近 24 小时" },
  { value: "7d", label: "最近 7 天" },
  { value: "30d", label: "最近 30 天（默认保留期）" },
  { value: "", label: "全部（已保留的明细）" },
];

const OUTCOME_OPTIONS = [
  { value: "", label: "全部结局" },
  { value: "success", label: "成功" },
  { value: "failure", label: "失败" },
  { value: "aborted", label: "客户端中断（未发完）" },
  { value: "stream_open", label: "流未收尾" },
];

const LABELS = {
  since: "时间范围",
  outcome: "结局",
  platform: "平台",
  model: "模型名",
  user: "用户标签",
  gateway_request_id: "请求 ID",
};

// 结局口径写在页面上，避免把「[DONE] 已发完后客户端关闭」误读成中断
const OUTCOME_NOTE =
  "结局口径：success 含「[DONE] 已转发后客户端才关闭连接」；"
  + "aborted 只统计 [DONE] 之前断开（done_sent=0）；"
  + "stream_open 是未收尾的流，不计入成功率。";

export async function renderAudit(root, ctx) {
  const filters = [
    { name: "since", node: select(SINCE_OPTIONS, "30d") },
    { name: "outcome", node: select(OUTCOME_OPTIONS, "") },
    { name: "platform", node: textInput("", "留空表示不限") },
    { name: "model", node: textInput("", "留空表示不限") },
    { name: "user", node: textInput("", "留空表示不限") },
    { name: "gateway_request_id", node: textInput("", "精确匹配一次请求") },
  ];
  const page = { limit: 50, offset: 0 };
  const loadButton = button("查询", "primary");
  const prev = button("上一页");
  const next = button("下一页");
  const listBox = el("div");
  const chainBox = el("div");
  const statusBox = el("div");

  const paging = el("div", { class: "row-actions" }, [prev, next, el("span", { class: "muted", text: "点击任意行看整条链路" })]);

  root.append(
    panel("筛选", [
      el("div", { class: "filters" }, filters.map((item) => control(LABELS[item.name], item.node))),
      el("p", { class: "muted", text: OUTCOME_NOTE }),
      el("p", { class: "muted", text: "明细默认保留 30 天（AUDIT_RETENTION_DAYS），更早记录会自动清理；全量用量在「用量与性能」的日汇总里永久保留。" }),
    ], loadButton),
    statusBox,
    panel("审计明细", [listBox], paging),
    chainBox,
  );

  loadButton.addEventListener("click", () => { page.offset = 0; load(); });
  prev.addEventListener("click", () => { page.offset = Math.max(0, page.offset - page.limit); load(); });
  next.addEventListener("click", () => { page.offset += page.limit; load(); });

  async function load() {
    const params = readControls(filters);
    loadButton.disabled = true;
    try {
      const [result, health] = await Promise.all([
        ctx.api.postQuery("/admin/audit", { ...params, limit: page.limit, offset: page.offset }),
        ctx.api.postQuery("/admin/audit/stats"),
      ]);
      renderHealth(statusBox, health);
      renderList(listBox, result, page, openChain);
      ctx.notice(`审计共 ${fmtNumber(health.store.rows)} 行，当前筛出 ${fmtNumber(result.total)} 行`, "ok");
    } catch (error) {
      ctx.guard(error);
    } finally {
      loadButton.disabled = false;
    }
  }

  async function openChain(gatewayRequestId) {
    try {
      const chain = await ctx.api.postQuery(`/admin/audit/chain/${encodeURIComponent(gatewayRequestId)}`);
      renderChain(chainBox, chain);
    } catch (error) {
      ctx.guard(error);
    }
  }

  await load();
}

// ---------- 呈现 ----------

function renderHealth(box, health) {
  const store = health.store || {};
  const collector = health.collector || {};
  box.replaceChildren(el("div", { class: "row-actions" }, [
    chip("审计", health.config.enabled),
    el("span", { class: "chip", text: `库行数 ${fmtNumber(store.rows)}` }),
    el("span", { class: "chip", text: `缓冲待写 ${fmtNumber(collector.pending_rows)}` }),
    el("span", { class: "chip", text: `丢弃 ${fmtNumber(collector.dropped_total)}` }),
    el("span", { class: "chip", text: `写库失败 ${fmtNumber(collector.write_errors_total)}` }),
    el("span", { class: "chip", text: `保留 ${health.config.retention_days} 天` }),
    el("span", { class: "chip", text: `最近刷盘 ${fmtTime(collector.last_flush_at)}` }),
  ]));
}

function renderList(box, result, page, openChain) {
  if (!result.items.length) {
    box.replaceChildren(empty("没有符合条件的审计行"));
    return;
  }
  box.replaceChildren(table([
    { key: "ts", label: "时间", render: (row) => fmtTime(row.ts) },
    { key: "gateway_request_id", label: "请求" },
    { key: "api_user", label: "用户", render: (row) => userLabel(row.api_user) },
    { key: "attempt_seq", label: "第几次", num: true },
    { key: "model_name", label: "模型" },
    { key: "outcome", label: "结局", render: outcomeChip },
    { key: "done_sent", label: "[DONE]", num: true, render: doneChip },
    { key: "duration_ms", label: "耗时", num: true, render: (row) => fmtMs(row.duration_ms) },
    { key: "ttft_ms", label: "首块", num: true, render: (row) => fmtMs(row.ttft_ms) },
    { key: "http_status", label: "状态码", num: true },
    { key: "error_category", label: "错误分类" },
    { key: "error_code", label: "错误码" },
  ], result.items, { onRow: (row) => openChain(row.gateway_request_id) }));
}

function renderChain(box, chain) {
  const attempts = chain.attempts || [];
  if (!attempts.length) {
    box.replaceChildren(panel("故障转移链", [empty(`请求 ${chain.gateway_request_id} 没有审计记录`)]));
    return;
  }

  const rows = attempts.map((attempt) => el("div", { class: "detail" }, [
    el("h4", { text: `#${attempt.attempt_seq} ${attempt.model_name} ${outcomeLabel(attempt.outcome)}` }),
    el("dl", { class: "kv" }, [
      dt("用户"), dd(userLabel(attempt.api_user)),
      dt("耗时"), dd(fmtMs(attempt.duration_ms)),
      dt("首块"), dd(fmtMs(attempt.ttft_ms)),
      dt("探测"), dd(fmtMs(attempt.probe_ms)),
      dt("状态码"), dd(attempt.http_status ?? "-"),
      dt("错误"), dd(`${attempt.error_category || "-"} / ${attempt.error_code || "-"}`),
      dt("禁用模型"), dd(attempt.disabled_model ? "是" : "否"),
      dt("[DONE] 已转发"), dd(doneLabel(attempt)),
      dt("tokens"), dd(`${attempt.input_tokens ?? "-"} → ${attempt.output_tokens ?? "-"}`),
      dt("tokens/s"), dd(attempt.throughput ?? "-"),
    ]),
  ]));

  box.replaceChildren(panel(`故障转移链：${chain.gateway_request_id}（${attempts.length} 次尝试）`, rows));
}

function outcomeChip(row) {
  const tones = { success: "ok", failure: "bad", aborted: "warn", stream_open: "" };
  return el("span", { class: `chip ${tones[row.outcome] || ""}` }, outcomeLabel(row.outcome));
}

function outcomeLabel(outcome) {
  return { success: "成功", failure: "失败", aborted: "客户端中断", stream_open: "流未收尾" }[outcome] || outcome;
}

/** [DONE] 是否已转发：非流式调用没有这个概念，不下结论 */
function doneLabel(row) {
  if (!row.stream) return "-";
  return row.done_sent ? "已发完" : "未发完";
}

function doneChip(row) {
  if (!row.stream) return el("span", { class: "chip", text: "-" });
  const sent = Boolean(row.done_sent);
  return el("span", { class: `chip ${sent ? "ok" : "warn"}` }, sent ? "已发完" : "未发完");
}

function dt(text) { return el("dt", { text }); }
function dd(value) { return el("dd", { text: String(value) }); }
function chip(label, enabled) {
  return el("span", { class: `chip ${enabled ? "ok" : "bad"}` }, `${label}：${enabled ? "开" : "关"}`);
}
