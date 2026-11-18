/** 工具栏弹出页：一眼看状态（服务端、本浏览器、当前买家号与账号、执行），外加这个状态带的按钮
 *  （规格 §5：重试、打开订单页、重新读取、复制买家号 ID、打开控制台）与「打开设置」「立即同步」。
 *  「开始执行（模拟）」不在这里：它要在亚马逊页面的面板上点（开始时要读那一页的买家号 ID）。 */

import type { RefreshIdentityReply, StateSnapshot } from "../core/messages.js";
import type { StatusAction } from "../core/status.js";
import { copyText, fetchState, keepFresh, onStateChange, toSw } from "../ui/bridge.js";
import { LOGO_SVG, themeCss } from "../ui/theme.js";
import { TONE_DOT, accountText, connectionText, h, receiptLines, runActive, scheduleText, statusBlock, statusOf } from "../ui/view.js";

const POPUP_CSS = `
body { margin: 0; width: 340px; background: var(--pc-bg-page); color: var(--pc-text-body); font: 400 14px/20px var(--pc-font-sans); -webkit-font-smoothing: antialiased; }
.wrap { padding: 12px; }
.grow { flex: 1; min-width: 0; }
.btns { display: flex; gap: 8px; margin-top: 14px; }
.btns .pc-btn { flex: 1; }
.pc-kv { grid-template-columns: 64px 1fr; }
.gap { margin-top: 12px; }
`;

const app = document.getElementById("app")!;
const style = document.createElement("style");
style.textContent = themeCss(":root") + POPUP_CSS;
document.head.append(style);

let state: StateSnapshot | null = null;
let busy = false;
let note: string | null = null;

function logo(): HTMLElement {
  const el = h("span", { class: "pc-logo" });
  el.innerHTML = LOGO_SVG; // 静态图标
  return el;
}

async function refresh(): Promise<void> {
  state = await fetchState();
  render();
}

/** 发一条消息给后台、等它做完、再取一次状态。done：连上了服务端时显示的一句（没连上时状态区自己会说原因）。 */
async function act(msg: Parameters<typeof toSw>[0], done: string | null = null): Promise<void> {
  busy = true;
  note = null;
  render();
  await toSw(msg, 60_000).catch(() => undefined);
  busy = false;
  state = await fetchState();
  note = done !== null && state?.connection?.ok ? done : null;
  render();
}

async function reread(): Promise<void> {
  busy = true;
  render();
  const r = await toSw<RefreshIdentityReply>({ type: "pc.refreshIdentity" }, 30_000).catch(() => null);
  if (!r?.reported) await toSw({ type: "pc.sync", reason: "重新读取", minAgeMs: 0 }, 60_000).catch(() => undefined);
  busy = false;
  note = r ? "已重新读取" : "重新读取失败：插件后台没有响应";
  await refresh();
}

async function copyId(): Promise<void> {
  const id = state ? statusOf(state).customer_id : null;
  if (!id) return;
  note = (await copyText(id, document.body)) ? "已复制买家号 ID" : "复制失败：请选中上面的买家号 ID 手动复制";
  render();
}

const actions: Record<StatusAction, () => void> = {
  retry: () => void act({ type: "pc.retry" }),
  open_options: () => void chrome.runtime.openOptionsPage(),
  open_orders: () => void toSw({ type: "pc.openOrders" }).catch(() => undefined),
  reread: () => void reread(),
  copy_id: () => void copyId(),
  open_console: () => void toSw({ type: "pc.openConsole" }).catch(() => undefined),
};

function render(): void {
  app.replaceChildren();
  const s = state;
  const card = h("section", { class: "pc-card" });
  card.append(
    h(
      "div",
      { class: "pc-card-hd" },
      logo(),
      h("div", { class: "grow" }, h("div", { class: "pc-title" }, "采购助手"), h("div", { class: "pc-sub" }, s?.device.device_name ?? (s?.device.registered ? "本浏览器" : "还没有连上服务端"))),
      h("span", { class: "pc-tag pc-tag-brand" }, "模拟模式"),
    ),
  );
  const body = h("div", { class: "pc-card-bd" });
  card.append(body);
  if (!s) {
    body.append(h("div", { class: "pc-notice pc-notice-danger" }, "插件后台没有响应"));
  } else {
    const conn = connectionText(s, Date.now());
    const run = runActive(s) ? s.run?.view ?? null : null;
    const receipts = receiptLines(s).length;
    body.append(
      h(
        "dl",
        { class: "pc-kv" },
        h("dt", {}, "服务端"), h("dd", {}, h("span", { class: "pc-row" }, h("span", { class: TONE_DOT[conn.tone] }), conn.text)),
        h("dt", {}, "浏览器"), h("dd", {}, s.device.device_name ?? "—"),
        h("dt", {}, "账号"), h("dd", {}, accountText(s)),
        h("dt", {}, "执行"), h("dd", {}, run ? `执行中：批次 #${run.run_id ?? "—"} · ${run.stage_label ?? run.message ?? "—"}` : scheduleText(s)),
        receipts ? h("dt", {}, "回执") : null,
        receipts ? h("dd", {}, `${receipts} 条结果回执待补交（服务端恢复后自动补交）`) : null,
      ),
    );
    // 「打开设置」在下面常驻，状态区里不重复画。
    const { open_options: _oo, ...blockActions } = actions;
    void _oo;
    const block = statusBlock(statusOf(s), blockActions, busy);
    block.classList.add("gap");
    body.append(block);
    if (note) body.append(h("div", { class: "pc-caption gap" }, note));
  }
  body.append(
    h(
      "div",
      { class: "btns" },
      h("button", { class: "pc-btn pc-btn-primary", type: "button", onclick: () => void chrome.runtime.openOptionsPage() }, "打开设置"),
      h("button", { class: "pc-btn", type: "button", disabled: busy, onclick: () => void act({ type: "pc.heartbeatNow" }, "已同步") }, busy ? "正在同步" : "立即同步"),
    ),
  );
  app.append(h("div", { class: "wrap" }, card));
}

onStateChange(() => void refresh());
void refresh();
keepFresh("打开弹出页", () => state);
