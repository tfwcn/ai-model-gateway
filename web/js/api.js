/**
 * 管理接口的访问层
 *
 * - 密钥只存在 sessionStorage（关掉标签就没了），不写进 URL
 * - 所有请求都带 Bearer；401/403 时通知上层弹出行内提示
 * - 出错时抛出 ApiError，把后端 detail（可能是 {message, errors}）规整成统一形状
 */

const KEY_STORAGE = "gateway.admin.apiKey";

// HTTP 请求头只接受可打印的 ISO-8859-1 字符。密钥里混进中文、全角符号、零宽字符时，
// 浏览器会在 fetch 里直接抛 "String contains non ISO-8859-1 code point"，
// 完全不提是哪个字符，所以这里先自己检查并给出可读的原因。
const HEADER_UNSAFE = /[^\x20-\x7e]/;

// 粘贴时容易带进来的空白与零宽字符：密钥本身不会包含它们，直接去掉
const STRIP_CHARS = /[\s\u200b\u200c\u200d\ufeff]+/g;

export class ApiError extends Error {
  constructor(status, message, payload) {
    super(message);
    this.status = status;
    this.payload = payload || null;
    const body = payload || {};
    const error = body.error || {};
    this.code = error.code || null;
    this.details = error.details || null;
    // 兼容两种校验错误：旧形状 detail.errors，新形状 error.details.errors
    this.issues = (body.detail && body.detail.errors)
      || (this.details && this.details.errors)
      || [];
    // 版本冲突等业务细节：优先用新形状的 details，其次回落到旧 detail 对象
    this.detail = body.detail && typeof body.detail === "object" ? body.detail : this.details;
  }
}

/** 密钥本身不合法（还没发请求就拦下），与后端返回的错误区分开 */
export class KeyProblem extends Error {}

/**
 * 校验并清理密钥
 *
 * Args:
 *   raw: 用户在密钥输入框里填的原始文本
 * Returns:
 *   清理后的密钥；空串表示「清除密钥」
 * Throws:
 *   KeyProblem: 含无法放进 HTTP 头的字符
 */
export function normalizeKey(raw) {
  const value = String(raw === null || raw === undefined ? "" : raw).replace(STRIP_CHARS, "");
  const bad = value.match(HEADER_UNSAFE);
  if (bad) throw new KeyProblem(`密钥里有不能放进请求头的字符：「${bad[0]}」(U+${hexCode(bad[0])})。密钥只能是 ASCII 字符，检查一下是否粘进了中文或全角符号。`);
  return value;
}

function hexCode(char) {
  return char.charCodeAt(0).toString(16).toUpperCase().padStart(4, "0");
}

/** 读取本标签页里保存的密钥；旧版本存进来的非法值直接丢弃，避免卡住之后所有请求 */
function readStoredKey() {
  let problem = null;
  let value = "";
  try {
    value = normalizeKey(sessionStorage.getItem(KEY_STORAGE) || "");
  } catch (exc) {
    problem = `本标签页里保存的密钥不合法（${exc.message}），已清除，请重新输入 GATEWAY_API_KEY。`;
    sessionStorage.removeItem(KEY_STORAGE);
  }
  return { value, problem };
}

export class Api {
  constructor(base = "") {
    this.base = base;
    const stored = readStoredKey();
    this.key = stored.value;
    // 上层据此提示用户「保存的密钥不合法」
    this.storageProblem = stored.problem;
  }

  setKey(key) {
    this.key = normalizeKey(key);
    if (this.key) {
      sessionStorage.setItem(KEY_STORAGE, this.key);
    } else {
      sessionStorage.removeItem(KEY_STORAGE);
    }
    this.storageProblem = null;
  }

  hasKey() {
    return Boolean(this.key);
  }

  get(path, params) {
    return this.request("GET", path, params, undefined);
  }

  post(path, body, ifMatch) {
    return this.request("POST", path, null, body, ifMatch);
  }

  /** 非转发接口统一走 POST：查询参数放 URL，不带请求体 */
  postQuery(path, params) {
    return this.request("POST", path, params, undefined);
  }

  del(path, params) {
    return this.request("DELETE", path, params, undefined);
  }

  async request(method, path, params, body, ifMatch) {
    const url = new URL(this.base + path, window.location.origin);
    Object.entries(params || {}).forEach(([name, value]) => {
      if (value !== null && value !== undefined && value !== "") {
        url.searchParams.set(name, String(value));
      }
    });

    const headers = {};
    if (this.key) headers.Authorization = `Bearer ${this.key}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (ifMatch) headers["If-Match"] = `"${ifMatch}"`;
    // 兜底：请求头装不下的字符一律在发请求前拦下，别让浏览器抛看不懂的类型错误
    Object.entries(headers).forEach(([name, value]) => {
      const bad = String(value).match(HEADER_UNSAFE);
      if (bad) throw new ApiError(0, `请求头 ${name} 含非法字符「${bad[0]}」(U+${hexCode(bad[0])})`);
    });

    let response;
    try {
      response = await fetch(url.pathname + url.search, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (exc) {
      throw new ApiError(0, `无法连接网关：${exc.message}`);
    }

    const payload = await parseBody(response);

    if (!response.ok) {
      throw new ApiError(response.status, messageFor(response, payload), payload);
    }
    return payload;
  }
}

async function parseBody(response) {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch (exc) {
    return { raw: text };
  }
}

function messageFor(response, payload) {
  const detail = payload && payload.detail;
  if (typeof detail === "string" && detail) return detail;
  if (detail && detail.message) return detail.message;
  const error = payload && payload.error;
  if (error && error.message) return error.message;

  if (response.status === 403) return "网关未配置 GATEWAY_API_KEY，管理接口已关闭";
  if (response.status === 401) return "密钥缺失或不正确";
  if (response.status === 404) return "接口不存在";
  return `请求失败（HTTP ${response.status}）`;
}
