/**
 * 管理台入口：密钥、视图切换、统一提示
 *
 * 前端不自己实现任何业务规则：所有数据与校验都来自 /admin/* 接口，
 * 前端只负责把接口结果呈现出来、把用户意图翻译成补丁操作。
 */

import { Api, ApiError } from "./api.js";
import { el } from "./ui.js";
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

class AdminApp {
  constructor() {
    this.api = new Api();
    this.noticeBox = document.getElementById("notice");
    this.main = document.getElementById("view");
    this.tabs = document.getElementById("tabs");
    this.current = "usage";

    this.bindShell();
    this.showView(this.current);
  }

  // ---------- 外壳 ----------

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
      this.showView(this.current);
    });

    // 已连接的状态只写在 placeholder 上，绝不写进 input.value：
    // 那串文字会被当成密钥发出去，而 HTTP 请求头装不下中文
    if (this.api.hasKey()) input.placeholder = "已连接，重新输入可替换";
  }

  showView(name) {
    const view = VIEWS[name] || VIEWS.usage;
    this.current = VIEWS[name] ? name : "usage";

    Array.from(this.tabs.querySelectorAll("button.tab")).forEach((button) => {
      button.setAttribute("aria-selected", String(button.dataset.view === this.current));
    });

    this.main.replaceChildren();
    if (!this.api.hasKey()) {
      this.notice(this.api.storageProblem || "还没有管理密钥：请输入 GATEWAY_API_KEY 再连接", "warn");
    }
    view.render(this.main, this.context());
  }

  // 视图共用的一组能力：请求、提示、回到当前视图
  context() {
    return {
      api: this.api,
      notice: this.notice.bind(this),
      reload: () => this.showView(this.current),
      guard: (error) => this.guard(error),
    };
  }

  // ---------- 提示 ----------

  notice(message, tone = "info") {
    this.noticeBox.hidden = false;
    this.noticeBox.className = `notice ${tone}`;
    this.noticeBox.textContent = message;
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
  const app = new AdminApp();
  const info = document.getElementById("build-info");
  if (info) info.textContent = `当前视图：${VIEWS[app.current].title}`;
});
