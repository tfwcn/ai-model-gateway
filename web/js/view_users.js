/**
 * 视图：用户级密钥
 *
 * 数据来源：
 *   GET    /admin/users        用户清单（只有可读前缀，没有明文）
 *   POST   /admin/users        新增用户并生成密钥（可加密落库）
 *   POST   /admin/users/{id}/delete   删除用户，密钥立即失效
 *
 * 管理台本身仍然使用 GATEWAY_API_KEY 连接；这里发出的用户密钥只能访问 /v1/*。
 */

import { button, control, el, empty, fmtTime, panel, table, textInput } from "./ui.js";

const CREATE_NOTE =
  "新增后会生成一把用户级密钥，请立即保存；管理接口也可以按用户名取回。"
  + "用户密钥只能调用 /v1/* 数据接口，不能访问管理接口。";

export async function renderUsers(root, ctx) {
  const nameInput = textInput("", "user-a");
  const addButton = button("生成密钥", "primary");
  const revealBox = el("div");
  const listBox = el("div");

  root.append(
    panel("新增用户", [
      el("div", { class: "filters" }, [control("用户标签", nameInput)]),
      el("p", { class: "muted", text: CREATE_NOTE }),
    ], addButton),
    revealBox,
    panel("用户级密钥", [listBox]),
  );

  addButton.addEventListener("click", create);
  nameInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") create();
  });

  async function create() {
    const name = nameInput.value.trim();
    if (!name) {
      ctx.notice("请先填写用户标签", "warn");
      return;
    }
    addButton.disabled = true;
    try {
      const result = await ctx.api.post("/admin/users", { name });
      nameInput.value = "";
      renderReveal(revealBox, result);
      await load();
      ctx.notice("已生成新密钥，请立即保存", "ok");
    } catch (error) {
      ctx.guard(error);
    } finally {
      addButton.disabled = false;
    }
  }

  async function load() {
    listBox.replaceChildren(el("p", { class: "muted", text: "加载中…" }));
    try {
      const result = await ctx.api.postQuery("/admin/users/list");
      renderList(listBox, result, remove);
    } catch (error) {
      ctx.guard(error);
    }
  }

  async function remove(row) {
    if (!confirm(`删除用户「${row.name}」？该密钥会立即失效，审计历史保留。`)) return;
    try {
      await ctx.api.post(`/admin/users/${row.id}/delete`);
      revealBox.replaceChildren();
      await load();
      ctx.notice(`已删除用户「${row.name}」`, "ok");
    } catch (error) {
      ctx.guard(error);
    }
  }

  await load();
}

// ---------- 呈现 ----------

function renderReveal(box, result) {
  const input = el("input", { readonly: true, class: "key-reveal" });
  input.value = result.key;

  box.replaceChildren(panel("新密钥", [
    el("p", { class: "muted", text: `用户：${result.user.name}` }),
    input,
    el("p", { class: "muted", text: "请复制保存后再离开本页；关闭后无法再次查看明文。" }),
  ]));
}

function renderList(box, result, remove) {
  const items = result.items || [];
  if (!items.length) {
    box.replaceChildren(empty("还没有用户级密钥：在上方填写标签后生成"));
    return;
  }

  const columns = [
    { key: "id", label: "ID", num: true },
    { key: "name", label: "用户标签" },
    { key: "key_prefix", label: "密钥前缀", render: (row) => `${row.key_prefix}…` },
    { key: "created_at", label: "创建时间", render: (row) => fmtTime(row.created_at) },
    { key: "actions", label: "操作", render: (row) => removeButton(row, remove) },
  ];
  box.replaceChildren(el("p", { class: "muted", text: `共 ${items.length} 个用户` }), table(columns, items));
}

function removeButton(row, remove) {
  const node = button("删除", "danger");
  node.addEventListener("click", (event) => {
    event.stopPropagation();
    remove(row);
  });
  return node;
}
