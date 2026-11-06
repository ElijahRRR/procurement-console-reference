/** 工具栏弹出页：一眼看状态（服务端、设备、账号、身份、执行），按钮「打开设置」「立即同步」。 */

import type { StateSnapshot } from "../core/messages.js";
import { fetchState, keepFresh, onStateChange, toSw } from "../ui/bridge.js";
import { LOGO_SVG, themeCss } from "../ui/theme.js";
import { accountText, connectionText, h, identityText, quotaText, receiptLines, runActive, scheduleText } from "../ui/view.js";

const POPUP_CSS = `
body { margin: 0; width: 340px; background: var(--pc-bg-page); color: var(--pc-text-body); font: 400 14px/20px var(--pc-font-sans); -webkit-font-smoothing: antialiased; }
.wrap { padding: 12px; }
.grow { flex: 1; min-width: 0; }
.btns { display: flex; gap: 8px; margin-top: 14px; }
.btns .pc-btn { flex: 1; }
.pc-kv { grid-template-columns: 64px 1fr; }
`;

const app = document.getElementById("app")!;
const style = document.createElement("style");
style.textContent = themeCss(":root") + POPUP_CSS;
document.head.append(style);

let state: StateSnapshot | null = null;
let syncing = false;

function logo(): HTMLElement {
  const el = h("span", { class: "pc-logo" });
  el.innerHTML = LOGO_SVG; // 静态图标
  return el;
}

function render(): void {
  app.replaceChildren();
  const s = state;
  const card = h("section", { class: "pc-card" });
  card.append(
    h(
      "div",
      { class: "pc-card-hd" },
      logo(),
      h("div", { class: "grow" }, h("div", { class: "pc-title" }, "采购助手"), h("div", { class: "pc-sub" }, s?.device.device_name ?? "未配对")),
      h("span", { class: "pc-tag pc-tag-brand" }, "模拟模式"),
    ),
  );
  const body = h("div", { class: "pc-card-bd" });
  card.append(body);
  if (!s) {
    body.append(h("div", { class: "pc-notice pc-notice-danger" }, "插件后台没有响应"));
  } else if (!s.device.paired) {
    body.append(h("div", { class: "pc-notice pc-notice-brand" }, "尚未配对。打开设置页，填写控制台生成的 8 位配对码。"));
  } else {
    const conn = connectionText(s, Date.now());
    const ident = identityText(s);
    const dot = conn.tone === "success" ? "pc-dot pc-dot-green" : conn.tone === "danger" ? "pc-dot pc-dot-red" : conn.tone === "warning" ? "pc-dot pc-dot-yellow" : "pc-dot";
    const run = runActive(s) ? s.run?.view ?? null : null;
    const receipts = receiptLines(s).length;
    body.append(
      h(
        "dl",
        { class: "pc-kv" },
        h("dt", {}, "服务端"), h("dd", {}, h("span", { class: "pc-row" }, h("span", { class: dot }), conn.text)),
        h("dt", {}, "账号"), h("dd", {}, accountText(s)),
        h("dt", {}, "身份"), h("dd", {}, h("span", { class: `pc-tag pc-tag-${ident.tone}` }, ident.short)),
        h("dt", {}, "额度"), h("dd", {}, quotaText(s)),
        h("dt", {}, "执行"), h("dd", {}, run ? `执行中：批次 #${run.run_id ?? "—"} · ${run.stage_label ?? run.message ?? "—"}` : scheduleText(s)),
        receipts ? h("dt", {}, "回执") : null,
        receipts ? h("dd", {}, `${receipts} 条结果回执待补交（服务端恢复后自动补交）`) : null,
      ),
    );
  }
  body.append(
    h(
      "div",
      { class: "btns" },
      h("button", { class: "pc-btn pc-btn-primary", type: "button", onclick: () => void chrome.runtime.openOptionsPage() }, "打开设置"),
      s?.device.paired
        ? h("button", { class: "pc-btn", type: "button", disabled: syncing, onclick: () => void sync() }, syncing ? "正在同步" : "立即同步")
        : null,
    ),
  );
  app.append(h("div", { class: "wrap" }, card));
}

async function sync(): Promise<void> {
  syncing = true;
  render();
  await toSw({ type: "pc.heartbeatNow" }, 60_000).catch(() => undefined);
  syncing = false;
  state = await fetchState();
  render();
}

async function refresh(): Promise<void> {
  state = await fetchState();
  render();
}

onStateChange(() => void refresh());
void refresh();
keepFresh("打开弹出页", () => state);
