/**
 * 悬浮提示（toast）+ 最近消息列表
 *
 * 提示浮在窗口右下角，不占页面布局，也不再被视图切换清空。
 * 底部那条进度条就是「还剩多久」：走完自动消失，鼠标停在提示上时暂停倒计时，
 * 这样长消息可以慢慢读，短消息也不会一直挂在页面上。
 * 消失掉的提示会留在顶栏「消息」按钮里，最多回看最近 10 条。
 */

import { el } from "./ui.js";

// 不同语气的停留时长：错误信息通常更长，给够阅读时间
const LIFETIME = { info: 5000, ok: 5000, warn: 6500, bad: 8000 };
// 同时最多挂几条，超出先送走最早的一条
const MAX_ITEMS = 4;
// 消息列表保留的条数
const MAX_HISTORY = 10;
// 退场动画时长；系统关掉动效时用这个定时器兜底移除节点
const EXIT_MS = 200;

export class Toast {
  /**
   * Args:
   *   dock: 右下角常驻容器，按钮、提示与消息列表都由这里生成
   */
  /**
   * Args:
   *   menu: 顶栏里的消息入口，按钮与最近消息下拉都挂在这里
   *   dock: 右下角的提示坞，只放浮动提示
   */
  constructor(menu, dock) {
    this.menu = menu;
    this.dock = dock;
    this.history = [];
    this.unread = 0;

    this.liveRegion = el("div", { class: "toast-region" });
    this.logList = el("ul", { class: "toast-log-list" });
    this.clearButton = el("button", {
      class: "toast-log-clear",
      type: "button",
      text: "清空",
      title: "清空最近消息",
      onclick: () => this.clear(),
    });
    this.log = el("div", { class: "toast-log", hidden: true }, [
      el("div", { class: "toast-log-head" }, [
        el("span", { class: "toast-log-title", text: "消息" }),
        el("span", { class: "toast-log-tools" }, [
          el("span", { class: "toast-log-meta", text: `最近 ${MAX_HISTORY} 条` }),
          this.clearButton,
        ]),
      ]),
      this.logList,
    ]);
    this.count = el("span", { class: "toast-log-count", hidden: true });
    this.toggle = el("button", {
      class: "toast-log-toggle",
      type: "button",
      "aria-expanded": "false",
      title: "查看最近的消息",
      onclick: () => this.setOpen(this.log.hidden),
    }, [el("span", { text: "消息" }), this.count]);

    // 按钮在顶栏、列表从按钮下方展开；提示坞只负责浮动的提示
    if (this.menu) this.menu.replaceChildren(this.toggle, this.log);
    if (this.dock) this.dock.replaceChildren(this.liveRegion);
    this.renderLog();

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !this.log.hidden) this.setOpen(false);
    });
    // 点页面空白处收起消息列表；点提示本身不算「外面」，免得读消息时被关掉
    document.addEventListener("click", (event) => {
      if (this.log.hidden) return;
      if (this.menu && this.menu.contains(event.target)) return;
      if (this.dock && this.dock.contains(event.target)) return;
      this.setOpen(false);
    });
  }

  /** 弹一条提示，返回节点（方便调用方自己提前关掉） */
  show(message, tone = "info") {
    if (!this.dock) return null;
    this.remember(message, tone);

    const item = el("div", {
      class: `toast-item ${tone}`,
      role: tone === "bad" ? "alert" : "status",
    }, [
      el("p", { class: "toast-text", text: message }),
      el("button", {
        class: "toast-close",
        type: "button",
        "aria-label": "关闭提示",
        title: "关闭",
        text: "×",
        onclick: () => this.dismiss(item),
      }),
    ]);
    // 进度条是 ::after，时长写进自定义属性：语气不同，停留时间也不同
    item.style.setProperty("--toast-life", `${LIFETIME[tone] || LIFETIME.info}ms`);

    // 进度条走完（没有被悬停暂停）就自动退场
    item.addEventListener("animationend", (event) => {
      if (event.animationName === "toast-countdown") this.dismiss(item);
    });

    this.liveRegion.appendChild(item);
    this.trimToasts();
    return item;
  }

  /** 收进消息列表：按时间倒序保留最近 MAX_HISTORY 条，收起时累计未读 */
  remember(message, tone) {
    this.history.unshift({ message, tone, time: clockTime() });
    this.history.length = Math.min(this.history.length, MAX_HISTORY);
    if (this.log.hidden) this.unread += 1;
    this.renderLog();
    this.updateCount();
  }

  /** 清空最近消息：列表留在原地显示空态 */
  clear() {
    this.history = [];
    this.unread = 0;
    this.renderLog();
    this.updateCount();
  }

  /** 退场：先播淡出动画，再移除节点 */
  dismiss(item) {
    if (!item.isConnected || item.classList.contains("toast-out")) return;
    item.classList.add("toast-out");
    // 系统开启「减少动态效果」时淡出动画不播，animationend 也不会来，靠定时器兜底
    window.setTimeout(() => item.remove(), EXIT_MS);
  }

  /** 同时挂着的提示不超过 MAX_ITEMS 条 */
  trimToasts() {
    const items = Array.from(this.liveRegion.querySelectorAll(".toast-item"));
    items.slice(0, Math.max(0, items.length - MAX_ITEMS)).forEach((node) => this.dismiss(node));
  }

  /** 展开 / 收起最近消息；打开时把未读清零并从最新一条看起 */
  setOpen(open) {
    this.log.hidden = !open;
    this.toggle.setAttribute("aria-expanded", String(open));
    if (open) {
      this.unread = 0;
      this.updateCount();
      this.log.scrollTop = 0;
    }
  }

  /** 未读角标：收起时累计，超过 9 条只显示 9+ */
  updateCount() {
    this.count.textContent = this.unread > 9 ? "9+" : String(this.unread);
    this.count.hidden = this.unread === 0;
  }

  /** 最近消息列表：最新的一条在最上面 */
  renderLog() {
    if (!this.logList) return;
    if (!this.history.length) {
      this.logList.replaceChildren(el("li", { class: "toast-log-empty", text: "还没有消息" }));
      return;
    }
    this.logList.replaceChildren(...this.history.map((entry) => el("li", {
      class: `toast-log-item ${entry.tone}`,
    }, [
      el("span", { class: "toast-log-time", text: entry.time }),
      el("p", { class: "toast-log-text", text: entry.message }),
    ])));
  }
}

/** 列表里的时间只到秒：回看时够定位，又不占宽度 */
function clockTime(date = new Date()) {
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}