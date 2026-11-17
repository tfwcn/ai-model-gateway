/**
 * 管理台入口：密钥、视图切换、统一提示
 *
 * 前端不自己实现任何业务规则：所有数据与校验都来自 /admin/* 接口，
 * 前端只负责把接口结果呈现出来、把用户意图翻译成补丁操作。
 */

import { Api, ApiError } from "./api.js";
import { fitTables } from "./ui.js";
import { Toast } from "./toast.js";
import { renderUsage } from "./view_usage.js";
import { renderAudit } from "./view_audit.js";
import { renderUsers } from "./view_users.js";
import { renderConfig } from "./view_config.js";
import { renderBackups } from "./view_backups.js";

const VIEWS = {
  usage: { title: "用量与性能", render: renderUsage },
  audit: { title: "审计明细", render: renderAudit },
  users: { title: "用户密钥", render: renderUsers },
  config: { title: "模型配置", render: renderConfig },
  backups: { title: "备份与回滚", render: renderBackups },
};

/** 地址栏里的位置：#audit/aggregate → { view: "audit", tab: "aggregate" } */
function parseHash() {
  const raw = decodeURIComponent(location.hash.replace(/^#/, ""));
  const [view = "", tab = ""] = raw.split("/");
  return { view, tab };
}

class AdminApp {
  constructor() {
    this.api = new Api();
    this.toast = new Toast(document.getElementById("notice-menu"), document.getElementById("toast-dock"));
    this.main = document.getElementById("view");
    this.tabs = document.getElementById("tabs");
    this.current = "";
    // 当前视图注册的回调：切页内页签、未保存改动检查、切走前的清理
    this.tabSelector = null;
    this.leaveGuard = null;
    this.teardowns = [];

    const { view, tab } = parseHash();
    this.bindShell();
    this.watchLayout();
    this.showView(VIEWS[view] ? view : "usage", tab);
  }

  // ---------- 外壳 ----------

  /**
   * 表格高度跟着它在页面里的位置走
   *
   * 视图重绘、查询结果、窗口尺寸变化、折叠区展开收起都会改变可用高度，
   * 统一在这里量一次，避免最外层再出现滚动条。
   */
  watchLayout() {
    let queued = false;
    const refit = () => {
      if (queued) return;
      queued = true;
      window.requestAnimationFrame(() => {
        queued = false;
        fitTables(this.main);
      });
    };
    new MutationObserver(refit).observe(this.main, {
      childList: true,
      subtree: true,
      // 切页签、展开折叠区改的是 hidden：显示出来之后要按新位置重新量一次
      attributes: true,
      attributeFilter: ["hidden"],
    });
    window.addEventListener("resize", refit);
    // toggle 不冒泡，用捕获阶段接住折叠区的展开/收起
    document.addEventListener("toggle", refit, true);
  }

  bindShell() {
    this.tabs.addEventListener("click", (event) => {
      const button = event.target.closest("button.tab");
      if (button) this.showView(button.dataset.view);
    });

    const input = document.getElementById("apikey");
    document.getElementById("keybox").addEventListener("submit", (event) => {
      event.preventDefault();
      try {
        this.api.setKey(input.value);
      } catch (exc) {
        // 密钥不合法：保留原来可用的密钥，也不刷新视图
        this.notice(exc.message, "bad");
        return;
      }
      input.value = "";
      if (this.api.hasKey()) {
        this.notice("密钥已保存在本标签页，关闭标签即失效", "ok");
      } else {
        this.notice("已清除密钥：管理接口不再可用", "warn");
      }
      input.placeholder = this.api.hasKey() ? "已连接，重新输入可替换" : "GATEWAY_API_KEY";
      // 保留当前页内页签：换密钥不需要把人踹回默认页签
      this.showView(this.current, parseHash().tab);
    });

    // 手改地址栏或前进后退：换视图就整页重渲染，同视图只切页内页签
    window.addEventListener("hashchange", () => this.followHash());
    // 刷新 / 关闭页面时，视图可以拦住「还有改动没写盘」的情况
    window.addEventListener("beforeunload", (event) => {
      const reason = this.leaveGuard ? this.leaveGuard() : "";
      if (!reason) return;
      event.preventDefault();
      event.returnValue = "";
    });

    // 已连接的状态只写在 placeholder 上，绝不写进 input.value：
    // 那串文字会被当成密钥发出去，而 HTTP 请求头装不下中文
    if (this.api.hasKey()) input.placeholder = "已连接，重新输入可替换";
  }

  followHash() {
    const { view, tab } = parseHash();
    if (!VIEWS[view]) return;
    if (view !== this.current) {
      this.showView(view, tab);
      return;
    }
    if (tab && this.tabSelector) this.tabSelector(tab);
  }

  /** 离开当前视图前先问视图：有没有没提交的改动 */
  confirmLeave() {
    const reason = this.leaveGuard ? this.leaveGuard() : "";
    if (!reason) return true;
    return window.confirm(`${reason}，离开后这些改动会丢失。确定离开？`);
  }

  showView(name, tab = "") {
    const target = VIEWS[name] ? name : "usage";
    if (this.current && !this.confirmLeave()) return;

    // 旧视图注册的清理函数（窗口 resize 监听等）先全部退订
    this.teardowns.forEach((fn) => {
      try {
        fn();
      } catch {
        // 清理失败不该挡住切换
      }
    });
    this.teardowns = [];
    this.leaveGuard = null;
    this.tabSelector = null;
    this.current = target;
    this.pendingTab = tab;

    Array.from(this.tabs.querySelectorAll("button.tab")).forEach((button) => {
      button.setAttribute("aria-selected", String(button.dataset.view === target));
    });

    this.main.replaceChildren();
    if (!this.api.hasKey()) {
      this.notice(this.api.storageProblem || "还没有管理密钥：请输入 GATEWAY_API_KEY 再连接", "warn");
    }
    // 先按地址栏写上初始位置，视图初始化页签时会再校正一次
    this.setHash(target, tab);
    VIEWS[target].render(this.main, this.context());
  }

  /** 只改地址栏，不触发 hashchange 重新渲染 */
  setHash(view, tab = "") {
    const hash = `#${view}${tab ? `/${tab}` : ""}`;
    if (location.hash !== hash) history.replaceState(null, "", hash);
  }

  // 视图共用的一组能力：请求、提示、页内页签、离开保护
  context() {
    return {
      api: this.api,
      notice: this.notice.bind(this),
      reload: () => this.showView(this.current, parseHash().tab),
      guard: (error) => this.guard(error),
      // 视图初始化时要停在哪一个页内页签（来自地址栏）
      initialTab: this.pendingTab,
      // 页内页签变化时写回地址栏
      setTab: (tab) => this.setHash(this.current, tab),
      // 同一视图下地址栏被手改时，交给视图自己切页签
      registerTabs: (selector) => { this.tabSelector = selector; },
      // 未提交改动检查：返回提示语表示不能直接离开，返回空串表示安全
      setLeaveGuard: (check) => { this.leaveGuard = check; },
      // 视图切走前的清理（例如退订窗口 resize）
      onTeardown: (fn) => { this.teardowns.push(fn); },
    };
  }

  // ---------- 提示 ----------

  /** 统一提示：右下角悬浮 + 超时进度条，同时进入「最近消息」列表 */
  notice(message, tone = "info") {
    this.toast.show(message, tone);
  }

  /**
   * 统一处理请求失败
   *
   * 401/403 直接告诉用户该做什么（补密钥 / 网关未开管理接口），
   * 其它错误原样显示后端给的原因，校验类错误额外展开字段清单。
   */
  guard(error) {
    if (error instanceof ApiError) {
      if (error.status === 401) {
        this.notice("密钥缺失或不正确：请在右上角重新输入 GATEWAY_API_KEY", "bad");
        return;
      }
      if (error.status === 403) {
        if (error.code === "forbidden") {
          this.notice("当前密钥没有管理权限：请用 GATEWAY_API_KEY 连接", "bad");
          return;
        }
        this.notice("网关未配置 GATEWAY_API_KEY，管理接口已关闭", "bad");
        return;
      }
      if (error.issues && error.issues.length) {
        this.notice(`${error.message}（共 ${error.issues.length} 处问题）`, "bad");
        return;
      }
      this.notice(error.message, "bad");
      return;
    }
    this.notice(`出错：${error.message}`, "bad");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  new AdminApp();
});