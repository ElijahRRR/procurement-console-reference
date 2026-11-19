/** 页面内面板（Shadow DOM，closed）：订单历史页默认展开，其余亚马逊页面是收起的悬浮按钮。
 *
 * 显示：服务端连接、本浏览器名称、当前买家号与账号、「模拟模式」标签、执行方式、模拟结局、
 * **状态区**（规格 §5 的状态文案表：这个状态说什么话、给什么按钮，见 core/status.ts）、
 * 当前尝试进度、待补交的结果回执、最近 50 行日志。
 * 按钮：「开始执行（模拟）」（只在买家号已登记时出现）、「重新读取」，以及状态带的按钮
 * （重试、打开设置、打开订单页、复制买家号 ID、打开控制台）。
 * 数据来源：后台的状态快照（chrome.storage.local 变化即刷新）+ 本页引擎的实时状态。
 * 面板挂上、页面回到前台都会让后台马上同步一次（还没连上服务端时顺带再试一次登记）；
 * 买家号还没有登记时，页面看得见就每 10 秒同步一次 —— 控制台里一登记，这里几秒内显示账号。
 * 买家号 ID 在面板上完整显示、可以复制（所有者要拿它去控制台登记）；日志里用遮掩形式。
 * 「打开订单页」「打开控制台」只在所有者点击时由后台新开标签页，面板自己不导航、不点击亚马逊页面。
 * 所有动态文本走 textContent，不拼 innerHTML。
 */

import type { EngineView } from "../core/engine.js";
import { isOrderHistoryPath } from "../core/identity.js";
import type { RefreshIdentityReply, StateSnapshot } from "../core/messages.js";
import { SIM_OUTCOMES } from "../core/simulate.js";
import type { StatusAction } from "../core/status.js";
import { LOGO_SVG, themeCss } from "../ui/theme.js";
import { copyText, fetchState, keepFresh, onStateChange, syncNow, toSw } from "../ui/bridge.js";
import {
  TONE_DOT,
  accountText,
  canStart,
  connectionText,
  h,
  receiptLines,
  renderLogLines,
  runActive,
  scheduleText,
  statusBlock,
  statusOf,
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

function logo(): HTMLElement {
  const el = h("span", { class: "pc-logo" });
  // LOGO_SVG 是本文件之外写死的静态图标，不含任何动态内容。
  el.innerHTML = LOGO_SVG;
  return el;
}

interface Notice {
  tone: "warning" | "danger" | "brand";
  text: string;
  /** 这条提示带的一个按钮（例如这一页没读到买家号 ID →「打开订单页」）。 */
  action?: StatusAction;
}

/** 输入：执行宿主 → 输出：无。把面板挂到页面上并保持刷新。 */
export function mountPanel(host: RunHost): void {
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
  let notice: Notice | null = null;
  let busyButton = false;

  const refresh = async (): Promise<void> => {
    state = await fetchState();
    render();
  };

  const setNotice = (n: Notice | null): void => {
    notice = n;
    render();
  };

  const onStart = async (): Promise<void> => {
    busyButton = true;
    setNotice(null);
    const r = await host.start("manual");
    busyButton = false;
    if (r.result === "started") return render();
    setNotice({
      tone: r.result === "busy" ? "warning" : "danger",
      text: r.message,
      action: r.code === "NO_CUSTOMER_ID" || r.code === "NO_IDENTITY" ? "open_orders" : undefined,
    });
  };

  /** 「重新读取」：请后台让每个亚马逊标签页重读身份、对账、上报；没报上去就再同步一次心跳。 */
  const onReread = async (): Promise<void> => {
    busyButton = true;
    render();
    const r = await toSw<RefreshIdentityReply>({ type: "pc.refreshIdentity" }, 30_000).catch(() => null);
    if (!r?.reported) await toSw({ type: "pc.sync", reason: "重新读取", minAgeMs: 0 }, 60_000).catch(() => undefined);
    busyButton = false;
    await refresh();
    if (!r) setNotice({ tone: "danger", text: "重新读取失败：插件后台没有响应，请刷新页面后再试" });
    else setNotice({ tone: "brand", text: `已重新读取：${state ? statusOf(state).short : "—"}` });
  };

  const onRetry = async (): Promise<void> => {
    busyButton = true;
    render();
    await toSw({ type: "pc.retry" }, 60_000).catch(() => undefined);
    busyButton = false;
    await refresh();
  };

  const onCopy = async (): Promise<void> => {
    const id = state ? statusOf(state).customer_id : null;
    if (!id) return;
    const ok = await copyText(id, root);
    setNotice(ok ? { tone: "brand", text: "已复制买家号 ID" } : { tone: "warning", text: "复制失败：浏览器不允许写入剪贴板，请选中上面的买家号 ID 手动复制" });
  };

  const actions: Record<StatusAction, () => void> = {
    retry: () => void onRetry(),
    open_options: () => void toSw({ type: "pc.openOptions" }).catch(() => undefined),
    open_orders: () => void toSw({ type: "pc.openOrders" }).catch(() => undefined),
    reread: () => void onReread(),
    copy_id: () => void onCopy(),
    open_console: () => void toSw({ type: "pc.openConsole" }).catch(() => undefined),
  };

  function runBlock(s: StateSnapshot): HTMLElement | null {
    const local = host.view();
    const remote = runActive(s) ? s.run?.view ?? null : null;
    const v: EngineView | null = local ?? remote;
    if (!v) {
      const last = host.last();
      return last ? h("div", { class: "run" }, h("div", { class: "pc-caption" }, last.message)) : null;
    }
    const elsewhere = !local && runActive(s);
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
    const s = state;
    const st = s ? statusOf(s) : null;
    if (!expanded) {
      // 要所有者动手处理的状态：悬浮按钮上带一句短说明与状态点，不让人只看到一个小圆点。
      const tone: Tone = st ? st.tone : "muted";
      wrap.append(
        h(
          "button",
          // 展开面板也同步一次：还没连上服务端时，这一次会让后台再试一次登记（受退避约束）。
          { class: "fab", type: "button", title: st ? `展开采购助手：${st.short}` : "展开采购助手", onclick: () => { expanded = true; syncNow("展开面板"); void refresh(); } },
          logo(),
          h("span", {}, st?.needs_owner ? `采购助手 · ${st.short}` : "采购助手"),
          h("span", { class: TONE_DOT[tone] }),
        ),
      );
      return;
    }

    const card = h("section", { class: "pc-card panel", "aria-label": "采购助手" });
    const header = h(
      "div",
      { class: "pc-card-hd" },
      logo(),
      h("div", { class: "grow" }, h("div", { class: "pc-title" }, "采购助手"), h("div", { class: "pc-sub" }, s?.device.device_name ?? (s?.device.registered ? "本浏览器" : "还没有连上服务端"))),
      h("span", { class: "pc-tag pc-tag-brand" }, "模拟模式"),
      h("button", { class: "pc-btn pc-btn-sm pc-btn-ghost", type: "button", onclick: () => { expanded = false; render(); } }, "收起"),
    );
    const body = h("div", { class: "pc-card-bd" });
    card.append(header, body);

    if (!s || !st) {
      body.append(h("div", { class: "pc-notice pc-notice-danger" }, "插件后台没有响应，请刷新页面或在扩展管理里重新加载插件"));
      wrap.append(card);
      return;
    }

    const conn = connectionText(s, Date.now());
    const outcome = SIM_OUTCOMES.find((o) => o.value === s.settings.simulationOutcome)?.label ?? "—";
    body.append(
      h(
        "dl",
        { class: "pc-kv" },
        h("dt", {}, "服务端"),
        h("dd", {}, h("span", { class: "status-cell" }, h("span", { class: TONE_DOT[conn.tone] }), conn.text)),
        h("dt", {}, "浏览器"),
        h("dd", {}, s.device.device_name ?? "—"),
        h("dt", {}, "账号"),
        h("dd", {}, accountText(s)),
        h("dt", {}, "执行方式"),
        h("dd", {}, scheduleText(s)),
        h("dt", {}, "模拟结局"),
        h("dd", {}, outcome),
      ),
    );

    // 状态区：「重新读取」在下面的按钮栏里常驻，这里不重复画。
    const { reread: _reread, ...blockActions } = actions;
    void _reread;
    const block = statusBlock(st, blockActions, busyButton);
    block.classList.add("notice-gap");
    body.append(block);

    const receipts = receiptLines(s);
    if (receipts.length) {
      body.append(
        h(
          "div",
          { class: "pc-notice pc-notice-warning notice-gap" },
          h("div", {}, `有 ${receipts.length} 条结果回执还没送到服务端，服务端恢复后自动补交：`),
          ...receipts.map((t) => h("div", { class: "pc-caption" }, t)),
        ),
      );
    }
    if (notice) {
      const n = notice;
      const el = h("div", { class: `pc-notice pc-notice-${n.tone} notice-gap` }, h("div", {}, n.text));
      if (n.action) {
        const act = n.action;
        el.append(h("div", { class: "pc-status-actions" }, h("button", { class: "pc-btn pc-btn-sm", type: "button", onclick: () => actions[act]() }, act === "open_orders" ? "打开订单页" : "重试")));
      }
      body.append(el);
    }

    const rb = runBlock(s);
    if (rb) body.append(rb);

    const start = canStart(s);
    const runningHere = host.running;
    // 「开始执行（模拟）」只在买家号已登记（ready）时出现；账号已暂停时出现但禁用。别的状态先按状态区的提示处理。
    const primary = runningHere
      ? h("button", { class: "pc-btn pc-btn-danger", type: "button", onclick: () => host.stop("用户点击停止") }, "停止执行")
      : st.kind === "ready"
        ? h(
            "button",
            { class: "pc-btn pc-btn-primary", type: "button", disabled: !start.ok || busyButton, title: start.reason ?? "按服务端分给这个买家号、已到采购日期的任务模拟执行", onclick: () => void onStart() },
            "开始执行（模拟）",
          )
        : null;
    body.append(
      h(
        "div",
        { class: "actions" },
        primary,
        h("button", { class: "pc-btn", type: "button", disabled: busyButton, title: "让这个浏览器的每个亚马逊标签页重读登录状态与买家号 ID", onclick: () => void onReread() }, "重新读取"),
      ),
    );
    if (primary && !runningHere && !start.ok && start.reason) body.append(h("div", { class: "pc-caption", style: "margin-top:6px" }, start.reason));

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
  keepFresh("打开亚马逊页面", () => state);
  // 连接状态的「多久没心跳」是按时间变化的：每 30 秒重画一次。
  setInterval(() => render(), 30_000);
}
