/**
 * 页内页签
 *
 * 一个视图里放多块内容时用它切换：同一时刻只显示一个 pane，
 * 其余 pane 用 hidden 收起（不是移出 DOM），切回来时不用重新请求。
 */

import { el } from "./ui.js";

export class Tabs {
  /**
   * Args:
   *   items: [{ key, label }]，顺序就是页签顺序
   *   panes: { key: 节点 }，键要和 items 里的 key 对上
   *   options: { onSelect(key) }，切换后回调（例如重画图表、重新量高度）
   */
  constructor(items, panes, options = {}) {
    this.panes = panes;
    this.onSelect = options.onSelect || null;
    this.current = "";

    this.buttons = items.map((item) => el("button", {
      class: "tab",
      type: "button",
      dataset: { tab: item.key },
      text: item.label,
      onclick: () => this.select(item.key),
    }));
    this.node = el("div", { class: "tabs view-tabs" }, this.buttons);
  }

  /** 切到某个页签：只改可见性，已渲染的内容保持原样 */
  select(key) {
    if (!this.panes[key]) return;
    this.current = key;
    Object.entries(this.panes).forEach(([name, pane]) => {
      pane.hidden = name !== key;
    });
    this.buttons.forEach((button) => {
      button.setAttribute("aria-selected", String(button.dataset.tab === key));
    });
    if (this.onSelect) this.onSelect(key);
  }
}