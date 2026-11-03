/**
 * 视图：备份与回滚
 *
 * 数据来源：
 *   GET  /admin/config/backups   每次写盘前自动留下的备份
 *   POST /admin/config/rollback  回滚到指定备份（当前内容也会先备份，所以回滚可再回滚）
 *   POST /admin/config/reload    改完 models.yaml 后不重启进程也能生效
 *   GET  /admin/config/validate  当前配置文件的校验结果
 * 服务永远不会自己重启：插件/爬虫结构变化时这里只提示需要你自己重启。
 */

import { button, el, empty, fmtNumber, fmtTime, issues, panel, table, textInput } from "./ui.js";

export async function renderBackups(root, ctx) {
  const listBox = el("div");
  const resultBox = el("div");
  const validateBox = el("div");
  const refreshButton = button("刷新", "primary");
  const reloadButton = button("进程内重载配置");
  const noteInput = textInput("", "回滚备注（写进备份元数据）");

  root.append(
    panel("可回滚的备份", [listBox], el("div", { class: "row-actions" }, [refreshButton, reloadButton, noteInput])),
    resultBox,
    panel("当前配置校验", [validateBox]),
  );

  refreshButton.addEventListener("click", () => load());
  reloadButton.addEventListener("click", () => run(() => ctx.api.post("/admin/config/reload", {}), (result) => {
    ctx.notice(result.message || "已重载", "ok");
  }));

  async function load() {
    try {
      const [backups, validation] = await Promise.all([
        ctx.api.postQuery("/admin/config/backups"),
        ctx.api.postQuery("/admin/config/validate"),
      ]);
      renderList(listBox, backups.items, rollback);
      validateBox.replaceChildren(
        el("p", { class: validation.valid ? "issue" : "issue error" },
          validation.valid ? "当前配置文件校验通过" : "当前配置文件存在硬错误"),
        issues(validation.errors, validation.warnings) || el("span"),
      );
      ctx.notice(`共有 ${backups.items.length} 个可回滚备份`, "ok");
    } catch (error) {
      ctx.guard(error);
    }
  }

  async function rollback(name) {
    if (!window.confirm(`确认回滚到 ${name}？当前内容会先备份，回滚本身也能再回滚。`)) return;
    await run(() => ctx.api.post("/admin/config/rollback", {
      name,
      reload: true,
      note: noteInput.value.trim(),
    }), (result) => {
      resultBox.replaceChildren(panel("回滚结果", [
        el("dl", { class: "kv" }, [
          el("dt", { class: "muted", text: "已回滚到" }), el("dd", { text: result.rolled_back_to }),
          el("dt", { class: "muted", text: "回滚前内容备份" }), el("dd", { text: result.backup }),
          el("dt", { class: "muted", text: "新版本" }), el("dd", { text: (result.version_after || "").slice(0, 12) }),
          el("dt", { class: "muted", text: "进程内重载" }), el("dd", { text: result.reloaded ? "已重载" : "未重载" }),
        ]),
        result.needs_restart
          ? el("p", { class: "issue warning", text: "插件/爬虫结构有变化，需要你自行重启服务才会完全生效。" })
          : null,
      ]));
      ctx.notice(`已回滚到 ${result.rolled_back_to}`, "ok");
    });
    await load();
  }

  async function run(request, onDone) {
    try {
      onDone(await request());
    } catch (error) {
      ctx.guard(error);
    }
  }

  await load();
}

function renderList(box, items, rollback) {
  if (!items.length) {
    box.replaceChildren(empty("还没有备份：每次写盘前都会自动备份当前文件"));
    return;
  }
  box.replaceChildren(table([
    { key: "name", label: "备份文件" },
    { key: "created_at", label: "时间", render: (row) => fmtTime(row.created_at) },
    { key: "bytes", label: "字节", num: true, render: (row) => fmtNumber(row.bytes) },
    { key: "version", label: "版本", render: (row) => String(row.version || "").slice(0, 12) },
    { key: "note", label: "备注" },
    {
      key: "name",
      label: "操作",
      render: (row) => {
        const node = button("回滚到此版本");
        node.addEventListener("click", (event) => { event.stopPropagation(); rollback(row.name); });
        return node;
      },
    },
  ], items));
}
