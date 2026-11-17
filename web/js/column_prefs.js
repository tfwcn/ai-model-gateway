/**
 * 表格显示列开关
 *
 * 用于「查询用的维度列永远显示、指标列由用户自己挑」的场景：
 * 点一下隐藏、再点一下显示，高亮态就是「这一列现在显示」。
 * 选择记在 localStorage 里，刷新后保持；存储不可用时只在本次页面内生效。
 */

import { el } from "./ui.js";

export class ColumnPicker {
  /**
   * Args:
   *   columns: [{ key, label }]，顺序就是表格里的顺序
   *   options: {
   *              storageKey: localStorage 的键；不传则只保留在内存
   *              onChange:   可见列变化时的回调，参数是新的可见 key 集合
   *            }
   */
  constructor(columns, options = {}) {
    this.columns = columns;
    this.storageKey = options.storageKey || "";
    this.onChange = options.onChange || null;
    this.visibleKeys = new Set(this._load());
    this.node = el("div", { class: "dim-chip-row" });
    this._render();
  }

  /** 当前可见的列 key；每次返回新集合，调用方随便改不会影响内部状态 */
  visible() {
    return new Set(this.visibleKeys);
  }

  _load() {
    const all = this.columns.map((col) => col.key);
    if (!this.storageKey) return all;
    try {
      const raw = JSON.parse(localStorage.getItem(this.storageKey) || "null");
      // 没存过：默认全部显示
      if (!Array.isArray(raw)) return all;
      const known = new Set(all);
      const keys = raw.filter((key) => known.has(key));
      // 存过非空但一列都对不上（列改名了）：退回默认，别给一张空表
      return raw.length && !keys.length ? all : keys;
    } catch {
      return all;
    }
  }

  _save() {
    if (!this.storageKey) return;
    try {
      // 按列顺序存，方便肉眼看出表头顺序
      const keys = this.columns.map((col) => col.key).filter((key) => this.visibleKeys.has(key));
      localStorage.setItem(this.storageKey, JSON.stringify(keys));
    } catch {
      // 隐私模式或存储被禁用时只保留内存状态
    }
  }

  _toggle(key) {
    if (this.visibleKeys.has(key)) this.visibleKeys.delete(key);
    else this.visibleKeys.add(key);
    this._save();
    this._render();
    if (this.onChange) this.onChange(this.visible());
  }

  _render() {
    this.node.replaceChildren(...this.columns.map((col) => {
      const shown = this.visibleKeys.has(col.key);
      return el("button", {
        class: "dim-chip",
        type: "button",
        text: col.label,
        "aria-pressed": shown ? "true" : "false",
        title: shown ? `隐藏「${col.label}」列` : `显示「${col.label}」列`,
        onclick: () => this._toggle(col.key),
      });
    }));
  }
}