/**
 * 日期范围控件
 *
 * 用两个原生 date 输入代替固定选项（7d / 30d / 90d）：
 * - 留空表示不限
 * - 结束日期按「包含当天」处理：后端按 ts < until 过滤，所以提交时自动加一天
 */
import { control, el } from "./ui.js";

export class DateRange {
  /**
   * Args:
   *   days: 默认显示最近几天（含今天）；不传表示默认留空、不限时间
   *   currentMonth: 默认显示当月（本月 1 日到今天），优先级高于 days
   */
  constructor({ days = null, currentMonth = false } = {}) {
    this.sinceNode = el("input", { type: "date" });
    this.untilNode = el("input", { type: "date" });
    if (currentMonth) {
      this.untilNode.value = todayIso();
      this.sinceNode.value = monthStartIso();
    } else if (days) {
      this.untilNode.value = todayIso();
      this.sinceNode.value = daysAgoIso(days - 1);
    }
  }

  /** 两个带标签的控件，直接塞进 filters 行里 */
  controls() {
    return [
      control("开始日期", this.sinceNode),
      control("结束日期", this.untilNode),
    ];
  }

  /** 查询参数；结束日期转成次日零点，保证结束日当天被包含 */
  params() {
    return {
      since: this.sinceNode.value,
      until: nextDayIso(this.untilNode.value),
    };
  }

  /** 折叠摘要用的一行日期描述：收起筛选区时也能看出当前查的是哪一段 */
  summary() {
    const since = this.sinceNode.value;
    const until = this.untilNode.value;
    if (!since && !until) return "不限时间";
    return `${since || "最早"} ~ ${until || "今天"}`;
  }

  /** 日期一变就刷新摘要（调用方传回调，避免这里直接碰视图） */
  onChange(handler) {
    [this.sinceNode, this.untilNode].forEach((node) => {
      node.addEventListener("input", handler);
      node.addEventListener("change", handler);
    });
  }
}

/** 本地时区的今天（YYYY-MM-DD） */
function todayIso() {
  return formatLocalDate(new Date());
}

/** 本地时区当月的 1 日（YYYY-MM-DD） */
function monthStartIso() {
  const today = new Date();
  return formatLocalDate(new Date(today.getFullYear(), today.getMonth(), 1));
}

/** 本地时区往前推 N 天 */
function daysAgoIso(days) {
  const date = new Date();
  date.setDate(date.getDate() - days);
  return formatLocalDate(date);
}

/** Date 转 YYYY-MM-DD（本地时区） */
function formatLocalDate(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

/** YYYY-MM-DD 的次日；空值或非法值返回空串 */
function nextDayIso(value) {
  if (!value) return "";
  const date = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return "";
  date.setUTCDate(date.getUTCDate() + 1);
  return date.toISOString().slice(0, 10);
}
