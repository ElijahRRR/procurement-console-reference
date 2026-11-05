/** 页面内面板（Shadow DOM，closed）：订单历史页默认展开，其余亚马逊页面是收起的悬浮按钮。
 *
 * 显示：服务端连接、设备名、绑定账号、身份状态、模拟模式标签、今日额度、执行时间、
 * 当前尝试进度、最近 50 行日志；按钮「开始执行（模拟）」「刷新身份」。
 * 数据来源：后台的状态快照（chrome.storage.local 变化即刷新）+ 本页引擎的实时状态。
 * 所有动态文本走 textContent，不拼 innerHTML。
 */

import type { EngineView } from "../core/engine.js";
import { isOrderHistoryPath, type IdentitySnapshot } from "../core/identity.js";
import type { StateSnapshot } from "../core/messages.js";
import { SIM_OUTCOMES } from "../core/simulate.js";
import { LOGO_SVG, themeCss } from "../ui/theme.js";
import { fetchState, onStateChange, toSw } from "../ui/bridge.js";
import {
  accountText,
  canStart,
  connectionText,
  h,
  identityText,
  quotaText,
  renderLogLines,
  scheduleText,
  type Tone,
} from "../ui/view.js";
import type { RunHost } from "./runner.js";

const PANEL_CSS = `
:host { all: initial; }
.wrap { position: fixed; right: 16px; bottom: 16px; z-index: 2147483000; font: 400 14px/20px var(--pc-font-sans);
        color: var(--pc-text-body); -webkit-font-smoothing: antialiased; }
.wrap * { box-sizing: border-box; }
.panel { width: 360px; max-width: calc(100vw - 32px); max-height: calc(100vh - 32px); overflow: auto; }
.fab { display: inline-flex; align-items: center; gap: 8px; height: 44px; padding: 0 14px 0 6px; cursor: pointer;
       border-radius: 9999px; background: var(--pc-bg-surface); border: 1px solid var(--pc-border-default);
       box-shadow: var(--pc-shadow-lg); color: var(--pc-text-strong); font: 500 14px/20px var(--pc-font-sans); }
.fab:focus-visible { outline: none; box-shadow: 0 0 0 2px var(--pc-bg-surface), 0 0 0 4px var(--pc-focus-ring); }
.fab .pc-logo { width: 32px; height: 32px; border-radius: 9999px; box-shadow: none; }
.grow { flex: 1; min-width: 0; }
.actions { display: flex; gap: 8px; margin-top: 14px; }
.actions .pc-btn { flex: 1; }
.section-title { font: 600 12px/16px var(--pc-font-sans); letter-spacing: 0.05em; color: var(--pc-text-muted); margin: 14px 0 8px; }
.run { margin-top: 12px; border: 1px solid var(--pc-border-subtle); border-radius: var(--pc-radius-xl); padding: 10px 12px; background: var(--pc-bg-muted); }
.run .line { display: flex; justify-content: space-between; gap: 8px; font-size: 13px; }
.counts { font-size: 12px; color: var(--pc-text-muted); margin-top: 6px; }
.status-cell { display: flex; align-items: center; gap: 6px; }
.notice-gap { margin-top: 12px; }
`;

const TONE_TAG: Record<Tone, string> = {
  success: "pc-tag pc-tag-success",
  warning: "pc-tag pc-tag-warning",
  danger: "pc-tag pc-tag-danger",
  muted: "pc-tag pc-tag-muted",
  brand: "pc-tag pc-tag-brand",
};
const TONE_DOT: Record<Tone, string> = {
  success: "pc-dot pc-dot-green",
  warning: "pc-dot pc-dot-yellow",
  danger: "pc-dot pc-dot-red",
  muted: "pc-dot",
  brand: "pc-dot pc-dot-green",
};

function logo(): HTMLElement {
  const el = h("span", { class: "pc-logo" });
  // LOGO_SVG 是本文件之外写死的静态图标，不含任何动态内容。
  el.innerHTML = LOGO_SVG;
  return el;
}

/** 输入：执行宿主、读身份的函数 → 输出：无。把面板挂到页面上并保持刷新。 */
export function mountPanel(host: RunHost, readIdentity: (force: boolean) => Promise<IdentitySnapshot | null>): void {
  const hostEl = document.createElement("div");
  hostEl.id = "pc-assistant-host";
  const root = hostEl.attachShadow({ mode: "closed" });
  const style = document.createElement("style");
  style.textContent = themeCss(":host") + PANEL_CSS;
  const wrap = h("div", { class: "wrap" });
  root.append(style, wrap);
  document.documentElement.append(hostEl);

  let state: StateSnapshot | null = null;
  let expanded = isOrderHistoryPath(location.pathname);
  let notice: { tone: "warning" | "danger" | "brand"; text: string } | null = null;
  let busyButton = false;

  const refresh = async (): Promise<void> => {
    state = await fetchState();
    render();
  };

  const setNotice = (n: typeof notice): void => {
    notice = n;
    render();
  };

  const onStart = async (): Promise<void> => {
    busyButton = true;
    setNotice(null);
    const r = await host.start("manual");
    busyButton = false;
    if (r.result !== "started") setNotice({ tone: r.result === "busy" ? "warning" : "danger", text: r.message });
    else render();
  };

  const onRefreshIdentity = async (): Promise<void> => {
    busyButton = true;
    render();
    const snap = await readIdentity(true);
    busyButton = false;
    await refresh();
    if (!snap) setNotice({ tone: "warning", text: "刷新身份失败：这一页没有读到亚马逊登录状态（页面上没有导航栏），请换一个亚马逊页面再试" });
    else setNotice({ tone: "brand", text: `已刷新身份：${state ? identityText(state).short : "—"}` });
  };

  function runBlock(s: StateSnapshot): HTMLElement | null {
    const local = host.view();
    const remote = s.run?.active ? s.run.view : null;
    const v: EngineView | null = local ?? remote;
    if (!v) {
      const last = host.last();
      return last ? h("div", { class: "run" }, h("div", { class: "pc-caption" }, last.message)) : null;
    }
    const elsewhere = !local && s.run?.active;
    const c = v.counts;
    return h(
      "div",
      { class: "run" },
      h(
        "div",
        { class: "line" },
        h("strong", {}, v.run_id ? `批次 #${v.run_id}` : "正在申请批次"),
        h("span", { class: "pc-tag pc-tag-brand" }, elsewhere ? "执行中（另一个标签页）" : "执行中"),
      ),
      v.attempt
        ? h(
            "div",
            { class: "line", style: "margin-top:6px" },
            h("span", { class: "pc-code" }, v.attempt.upstream_order_no ?? `尝试 #${v.attempt.attempt_id}`),
            h("span", {}, v.stage_label ?? "—"),
          )
        : h("div", { class: "pc-caption", style: "margin-top:6px" }, v.message ?? "—"),
      h("div", { class: "counts" }, `已领取 ${c.attempts} · 成功 ${c.succeeded} · 失败 ${c.failed} · 拦截 ${c.blocked} · 待核实 ${c.unknown} · 中止 ${c.aborted}`),
    );
  }

  function render(): void {
    wrap.replaceChildren();
    if (!expanded) {
      const s = state;
      const tone: Tone = !s || !s.device.paired ? "muted" : connectionText(s, Date.now()).tone;
      wrap.append(
        h(
          "button",
          { class: "fab", type: "button", title: "展开采购助手", onclick: () => { expanded = true; void refresh(); } },
          logo(),
          h("span", {}, "采购助手"),
          h("span", { class: TONE_DOT[tone] }),
        ),
      );
      return;
    }

    const s = state;
    const card = h("section", { class: "pc-card panel", "aria-label": "采购助手" });
    const header = h(
      "div",
      { class: "pc-card-hd" },
      logo(),
      h("div", { class: "grow" }, h("div", { class: "pc-title" }, "采购助手"), h("div", { class: "pc-sub" }, s?.device.device_name ?? "未配对")),
      h("span", { class: "pc-tag pc-tag-brand" }, "模拟模式"),
      h("button", { class: "pc-btn pc-btn-sm pc-btn-ghost", type: "button", onclick: () => { expanded = false; render(); } }, "收起"),
    );
    const body = h("div", { class: "pc-card-bd" });
    card.append(header, body);

    if (!s) {
      body.append(h("div", { class: "pc-notice pc-notice-danger" }, "插件后台没有响应，请刷新页面或在扩展管理里重新加载插件"));
      wrap.append(card);
      return;
    }

    if (!s.device.paired) {
      body.append(
        h("div", { class: "pc-notice pc-notice-brand" }, "尚未配对：打开设置页，填写控制台「账号与设备」里生成的 8 位配对码。"),
        h("div", { class: "actions" }, h("button", { class: "pc-btn pc-btn-primary", type: "button", onclick: () => void toSw({ type: "pc.openOptions" }) }, "打开设置")),
      );
      wrap.append(card);
      return;
    }

    const conn = connectionText(s, Date.now());
    const ident = identityText(s);
    const outcome = SIM_OUTCOMES.find((o) => o.value === s.settings.simulationOutcome)?.label ?? s.settings.simulationOutcome;
    body.append(
      h(
        "dl",
        { class: "pc-kv" },
        h("dt", {}, "服务端"),
        h("dd", {}, h("span", { class: "status-cell" }, h("span", { class: TONE_DOT[conn.tone] }), conn.text)),
        h("dt", {}, "设备"),
        h("dd", {}, s.device.device_name ?? "—"),
        h("dt", {}, "绑定账号"),
        h("dd", {}, accountText(s)),
        h("dt", {}, "身份"),
        h("dd", {}, h("span", { class: TONE_TAG[ident.tone] }, ident.short), h("div", { class: "pc-caption", style: "margin-top:4px" }, ident.long)),
        h("dt", {}, "今日额度"),
        h("dd", {}, quotaText(s)),
        h("dt", {}, "执行方式"),
        h("dd", {}, scheduleText(s)),
        h("dt", {}, "模拟结局"),
        h("dd", {}, outcome),
      ),
    );

    if (s.identity_status === "mismatch") {
      body.append(h("div", { class: "pc-notice pc-notice-danger notice-gap" }, "当前浏览器登录的亚马逊账号不是绑定账号，服务端不会下发任务。请切换回绑定账号后点「刷新身份」。"));
    }
    if (notice) body.append(h("div", { class: `pc-notice pc-notice-${notice.tone} notice-gap` }, notice.text));

    const rb = runBlock(s);
    if (rb) body.append(rb);

    const start = canStart(s);
    const runningHere = host.running;
    body.append(
      h(
        "div",
        { class: "actions" },
        runningHere
          ? h("button", { class: "pc-btn pc-btn-danger", type: "button", onclick: () => host.stop("用户点击停止") }, "停止执行")
          : h(
              "button",
              { class: "pc-btn pc-btn-primary", type: "button", disabled: !start.ok || busyButton, title: start.reason ?? "按服务端分配给本账号、已到采购日期的任务模拟执行" , onclick: () => void onStart() },
              "开始执行（模拟）",
            ),
        h("button", { class: "pc-btn", type: "button", disabled: busyButton, onclick: () => void onRefreshIdentity() }, "刷新身份"),
      ),
    );
    if (!start.ok && !runningHere && start.reason) body.append(h("div", { class: "pc-caption", style: "margin-top:6px" }, start.reason));

    body.append(h("div", { class: "section-title" }, "日志（北京时间）"));
    const logBox = h("div", { class: "pc-log", role: "log" }, ...renderLogLines(s.logs));
    if (s.logs.length === 0) logBox.append(h("div", {}, "暂无日志"));
    body.append(logBox);
    wrap.append(card);
    logBox.scrollTop = logBox.scrollHeight;
  }

  host.onChange(() => render());
  onStateChange(() => void refresh());
  render();
  void refresh();
  // 连接状态的「多久没心跳」是按时间变化的：每 30 秒重画一次。
  setInterval(() => render(), 30_000);
}
