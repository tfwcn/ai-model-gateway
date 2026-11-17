/**
 * 浏览器侧的维度查询条件记录
 *
 * - 最近组合：每次查询成功后把完整条件记到最前面；
 * - 同一个组合再次查询时只更新这一条，排序 / 方向以最后一次为准；
 * - 去重后只保留前 N 个；localStorage 不可用时退化为当前页面内存储。
 */
const STORAGE_KEY = "ai-model-gateway.usage.dimension-combos.v1";
const LAST_QUERY_KEY = "ai-model-gateway.usage.dimension-last.v1";
const DEFAULT_LIMIT = 10;

export class RecentCombos {
  constructor(key = STORAGE_KEY, limit = DEFAULT_LIMIT) {
    this.key = key;
    this.limit = limit;
    this.fallback = [];
  }

  /** 读取最近组合：过滤非法项、按组合去重，顺序就是最近使用的顺序 */
  load() {
    try {
      return this._normalizeList(JSON.parse(localStorage.getItem(this.key) || "[]"));
    } catch {
      return this._normalizeList(this.fallback);
    }
  }

  /** 记录一次查询：同一组合只保留最新一条，并置顶、截断到 limit */
  record(value) {
    const condition = normalizeCondition(value);
    if (!condition) return this.load();
    const rest = this.load().filter((item) => item.group_by !== condition.group_by);
    const next = [condition, ...rest].slice(0, this.limit);
    this._save(next);
    return next;
  }

  /** 清空记录（预留：以后需要「清空最近组合」时直接调用） */
  clear() {
    this._save([]);
  }

  _save(values) {
    this.fallback = values;
    try {
      localStorage.setItem(this.key, JSON.stringify(values));
    } catch {
      // 隐私模式或存储被禁用时只保留内存记录
    }
  }

  _normalizeList(values) {
    if (!Array.isArray(values)) return [];
    const seen = new Set();
    const result = [];
    values.forEach((value) => {
      const condition = normalizeCondition(value);
      if (!condition || seen.has(condition.group_by)) return;
      seen.add(condition.group_by);
      result.push(condition);
    });
    return result.slice(0, this.limit);
  }
}

/** 上一次的维度查询条件：组合、入口、排序、方向 */
export class LastDimensionQuery {
  constructor(key = LAST_QUERY_KEY) {
    this.key = key;
    this.fallback = null;
  }

  load() {
    try {
      return normalizeCondition(JSON.parse(localStorage.getItem(this.key) || "null"));
    } catch {
      return normalizeCondition(this.fallback);
    }
  }

  save(value) {
    const normalized = normalizeCondition(value);
    this.fallback = normalized;
    try {
      localStorage.setItem(this.key, JSON.stringify(normalized));
    } catch {
      // 隐私模式或存储被禁用时只保留内存记录
    }
  }
}

/** 查询条件统一成对象；兼容旧版只存组合字符串的记录 */
export function normalizeCondition(value) {
  if (typeof value === "string") value = { group_by: value };
  if (!value || typeof value !== "object") return null;
  const groupBy = normalizeCombo(value.group_by);
  if (!groupBy) return null;
  return {
    group_by: groupBy,
    endpoint: String(value.endpoint || "").trim(),
    sort: String(value.sort || "requests").trim(),
    order: String(value.order || "").trim(),
    filters: normalizeFilters(value.filters),
  };
}

/** 维度筛选统一成「维度名 -> 原始输入」；数组值用逗号拼回输入框 */
export function normalizeFilters(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const result = {};
  Object.entries(value).forEach(([name, raw]) => {
    const key = String(name || "").trim();
    if (!key) return;
    const text = Array.isArray(raw)
      ? raw.map((item) => String(item).trim()).filter(Boolean).join(",")
      : String(raw ?? "").trim();
    if (text) result[key] = text;
  });
  return result;
}

/** 组合统一成 `a,b,c`：去空格、去空项、保持现有顺序 */
export function normalizeCombo(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean)
    .join(",");
}