/** 设置页：服务端地址、配对 / 解除配对、模拟结局、服务端下发的账号配置、日志。 */

import { maskCustomerId } from "../core/identity.js";
import type { StateSnapshot } from "../core/messages.js";
import { normalizeSlots } from "../core/schedule.js";
import { BROWSER_OPTIONS, DEFAULT_SERVER_URL, isValidPairingCode, validateServerUrl } from "../core/settings.js";
import { SIM_OUTCOMES, type SimOutcome } from "../core/simulate.js";
import { bjShort } from "../core/time.js";
import type { Browser } from "../core/types.js";
import { fetchState, keepFresh, onStateChange, toSw } from "../ui/bridge.js";
import { LOGO_SVG, themeCss } from "../ui/theme.js";
import {
  accountText,
  connectionText,
  h,
  identityConflictText,
  identityText,
  quotaText,
  receiptLines,
  renderLogLines,
  scheduleText,
} from "../ui/view.js";

const PAGE_CSS = `
body { margin: 0; background: var(--pc-bg-page); color: var(--pc-text-body); font: 400 14px/20px var(--pc-font-sans); -webkit-font-smoothing: antialiased; }
.page { max-width: 760px; margin: 0 auto; padding: 32px 24px 48px; }
.page-hd { display: flex; align-items: center; gap: 12px; margin-bottom: 24px; }
.page-hd .pc-logo { width: 40px; height: 40px; }
.page-title { font: 700 24px/32px var(--pc-font-sans); color: var(--pc-text-strong); }
.stack { display: flex; flex-direction: column; gap: 24px; }
.card-title { font: 600 18px/28px var(--pc-font-sans); color: var(--pc-text-strong); }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 24px; }
.grid .full { grid-column: 1 / -1; }
.btns { display: flex; gap: 8px; flex-wrap: wrap; }
.pc-kv { grid-template-columns: 120px 1fr; font-size: 14px; }
.pc-log { max-height: 320px; }
.desc { margin: 0 0 16px; color: var(--pc-text-muted); font-size: 13px; line-height: 20px; }
.hint { color: var(--pc-text-muted); font-size: 12px; line-height: 16px; margin-top: 6px; }
.grow { flex: 1; min-width: 0; }
@media (max-width: 640px) { .grid { grid-template-columns: 1fr; } }
`;

const app = document.getElementById("app")!;
const style = document.createElement("style");
style.textContent = themeCss(":root") + PAGE_CSS;
document.head.append(style);

let state: StateSnapshot | null = null;
let message: { tone: "danger" | "brand" | "warning"; text: string } | null = null;
let pending = false;
// 配对表单的输入在重画之间保留。
const form = { serverUrl: "", code: "", deviceName: "", browser: "chrome" as Browser };
let formInit = false;

function logo(): HTMLElement {
  const el = h("span", { class: "pc-logo" });
  el.innerHTML = LOGO_SVG; // 静态图标
  return el;
}

function input(attrs: Record<string, string | boolean | undefined>, value: string, onInput: (v: string) => void): HTMLInputElement {
  const el = h("input", { class: "pc-input", ...attrs });
  el.value = value;
  el.addEventListener("input", () => onInput(el.value));
  return el;
}

function card(title: string, ...children: Array<Node | null>): HTMLElement {
  return h(
    "section",
    { class: "pc-card" },
    h("div", { class: "pc-card-hd" }, h("div", { class: "card-title" }, title)),
    h("div", { class: "pc-card-bd" }, ...children),
  );
}

async function refresh(): Promise<void> {
  state = await fetchState();
  if (state && !formInit) {
    form.serverUrl = state.device.server_url ?? state.settings.serverUrl ?? DEFAULT_SERVER_URL;
    formInit = true;
  }
  render();
}

async function doPair(): Promise<void> {
  const url = validateServerUrl(form.serverUrl);
  if (!url.ok) return show("danger", `配对失败：${url.reason}`);
  if (!isValidPairingCode(form.code)) return show("danger", "配对失败：配对码应为 8 位字母或数字");
  if (!form.deviceName.trim()) return show("danger", "配对失败：请填写设备名称");
  pending = true;
  render();
  try {
    const r = await toSw<{ ok: boolean; message: string }>(
      { type: "pc.pair", serverUrl: form.serverUrl, code: form.code, deviceName: form.deviceName, browser: form.browser },
      60_000,
    );
    if (r.ok) form.code = "";
    show(r.ok ? "brand" : "danger", r.message);
  } catch (e) {
    show("danger", `配对失败：${e instanceof Error ? e.message : String(e)}`);
  } finally {
    pending = false;
    await refresh();
  }
}

async function doUnpair(): Promise<void> {
  if (!window.confirm("确定要解除配对吗？解除后本浏览器不再领取任务；如需让服务端也作废这个设备，请在控制台撤销该设备。")) return;
  const r = await toSw<{ ok: boolean; message: string }>({ type: "pc.unpair" }).catch((e: unknown) => ({ ok: false, message: `解除配对失败：${String(e)}` }));
  show(r.ok ? "brand" : "danger", r.message);
  await refresh();
}

async function doSync(): Promise<void> {
  pending = true;
  render();
  await toSw({ type: "pc.heartbeatNow" }, 60_000).catch(() => undefined);
  pending = false;
  await refresh();
  if (state?.connection?.ok) show("brand", "已同步");
  else show("danger", `同步失败：${state?.connection?.message ?? "未知原因"}`);
}

async function saveOutcome(v: SimOutcome): Promise<void> {
  const r = await toSw<{ ok: boolean; message: string }>({ type: "pc.saveSettings", patch: { simulationOutcome: v } }).catch(() => ({ ok: false, message: "保存失败：插件后台没有响应" }));
  show(r.ok ? "brand" : "danger", r.ok ? "模拟结局已保存" : r.message);
  await refresh();
}

function show(tone: "danger" | "brand" | "warning", text: string): void {
  message = { tone, text };
  render();
}

function pairingCard(s: StateSnapshot): HTMLElement {
  if (!s.device.paired) {
    const browserSel = h("select", { class: "pc-select", id: "browser" });
    for (const o of BROWSER_OPTIONS) {
      const opt = h("option", { value: o.value }, o.label);
      if (o.value === form.browser) opt.selected = true;
      browserSel.append(opt);
    }
    browserSel.addEventListener("change", () => { form.browser = browserSel.value as Browser; });
    return card(
      "连接与配对",
      h("p", { class: "desc" }, "在控制台「账号与设备」里生成配对码（10 分钟内有效，只能用一次），填到这里完成配对。一个浏览器环境（一个 Chrome 用户或一个 AdsPower 环境）配对一次。"),
      h(
        "div",
        { class: "grid" },
        h("div", { class: "pc-field full" },
          h("label", { class: "pc-label", for: "server" }, "服务端地址"),
          input({ id: "server", placeholder: DEFAULT_SERVER_URL, autocomplete: "off" }, form.serverUrl, (v) => { form.serverUrl = v; }),
          h("div", { class: "hint" }, `控制台所在的地址，默认 ${DEFAULT_SERVER_URL}。当前版本只支持本机（127.0.0.1 或 localhost）。`)),
        h("div", { class: "pc-field" },
          h("label", { class: "pc-label", for: "code" }, "配对码"),
          input({ id: "code", placeholder: "8 位配对码", maxlength: "12", autocomplete: "off", spellcheck: "false" }, form.code, (v) => { form.code = v; })),
        h("div", { class: "pc-field" },
          h("label", { class: "pc-label", for: "browser" }, "浏览器"),
          browserSel),
        h("div", { class: "pc-field full" },
          h("label", { class: "pc-label", for: "name" }, "设备名称"),
          input({ id: "name", placeholder: "例如 Mac-AdsPower-环境1", maxlength: "60", autocomplete: "off" }, form.deviceName, (v) => { form.deviceName = v; }),
          h("div", { class: "hint" }, "在控制台里区分不同电脑与浏览器环境用。")),
      ),
      h("div", { class: "btns" }, h("button", { class: "pc-btn pc-btn-primary", type: "button", disabled: pending, onclick: () => void doPair() }, pending ? "正在配对" : "配对")),
    );
  }
  const conn = connectionText(s, Date.now());
  const ident = identityText(s);
  const dotClass = conn.tone === "success" ? "pc-dot pc-dot-green" : conn.tone === "danger" ? "pc-dot pc-dot-red" : conn.tone === "warning" ? "pc-dot pc-dot-yellow" : "pc-dot";
  const tagClass = `pc-tag pc-tag-${ident.tone}`;
  return card(
    "连接与配对",
    h(
      "dl",
      { class: "pc-kv" },
      h("dt", {}, "服务端"), h("dd", {}, h("span", { class: "pc-code" }, s.device.server_url ?? "—")),
      h("dt", {}, "连接状态"), h("dd", {}, h("span", { class: "pc-row" }, h("span", { class: dotClass }), conn.text)),
      h("dt", {}, "设备"), h("dd", {}, `${s.device.device_name ?? "—"}（ID ${s.device.device_id ?? "—"}，${BROWSER_OPTIONS.find((b) => b.value === s.device.browser)?.label ?? "—"}）`),
      h("dt", {}, "工作区"), h("dd", {}, s.device.workspace_name ?? "—"),
      h("dt", {}, "配对时间"), h("dd", {}, s.device.paired_at ? `${bjShort(s.device.paired_at)}（北京时间）` : "—"),
      h("dt", {}, "绑定账号"), h("dd", {}, accountText(s)),
      h("dt", {}, "身份状态"), h("dd", {}, h("span", { class: tagClass }, ident.short), h("div", { class: "hint" }, ident.long)),
      h("dt", {}, "页面识别"), h("dd", {}, s.identity
        ? `${s.identity.signed_in ? `已登录，买家号 ID ${maskCustomerId(s.identity.customer_id)}` : "未登录"} · ${bjShort(s.identity.at)} · ${s.identity.page_url ?? "—"}`
        : "还没有读到（打开任意亚马逊页面后自动读取）",
        identityConflictText(s) ? h("div", { class: "hint" }, identityConflictText(s)!) : null),
    ),
    h("div", { class: "pc-sep" }),
    h(
      "div",
      { class: "btns" },
      h("button", { class: "pc-btn", type: "button", disabled: pending, onclick: () => void doSync() }, "立即同步"),
      h("button", { class: "pc-btn pc-btn-danger", type: "button", onclick: () => void doUnpair() }, "解除配对"),
    ),
  );
}

function receiptsCard(s: StateSnapshot): HTMLElement | null {
  const lines = receiptLines(s);
  if (!s.device.paired || lines.length === 0) return null;
  return card(
    "待补交的结果回执",
    h("p", { class: "desc" }, "这些结果在上报时没有送到服务端（断网、服务端重启或插件后台被回收）。插件保存了回执，服务端恢复后自动补交；补交的是同一份结果，服务端不会登记两次。"),
    ...lines.map((t) => h("div", { class: "hint" }, t)),
  );
}

function simulationCard(s: StateSnapshot): HTMLElement {
  const sel = h("select", { class: "pc-select", id: "outcome" });
  for (const o of SIM_OUTCOMES) {
    const opt = h("option", { value: o.value }, o.label);
    if (o.value === s.settings.simulationOutcome) opt.selected = true;
    sel.append(opt);
  }
  sel.addEventListener("change", () => void saveOutcome(sel.value as SimOutcome));
  const hint = SIM_OUTCOMES.find((o) => o.value === s.settings.simulationOutcome)?.hint ?? "";
  return card(
    "模拟执行",
    h("p", { class: "desc" }, "本版本只做模拟执行：插件领取服务端分配给本账号、已到采购日期的任务，按任务数据推出一份结算信息交给服务端护栏裁决，再走提交登记与结果上报。不打开、不点击、不修改任何亚马逊页面，不会产生真实订单；模拟单号形如 SIM-日期-尝试号，不写回飞书，可在控制台一键清除。"),
    h("div", { class: "pc-field" }, h("label", { class: "pc-label", for: "outcome" }, "模拟结局"), sel, h("div", { class: "hint" }, hint)),
  );
}

function configCard(s: StateSnapshot): HTMLElement | null {
  const hb = s.config?.heartbeat;
  if (!s.device.paired || !hb) return null;
  const a = hb.account;
  const n = (v: number | null | undefined, unit = ""): string => (v === null || v === undefined ? "—" : `${v}${unit}`);
  const money = (v: string | null | undefined): string => (v === null || v === undefined ? "不限" : `$${v}`);
  return card(
    "账号配置（服务端下发）",
    h("p", { class: "desc" }, "以下是服务端已确认的有效配置，只读。修改请到控制台「账号与设备」。"),
    h(
      "dl",
      { class: "pc-kv" },
      h("dt", {}, "执行方式"), h("dd", {}, scheduleText(s)),
      h("dt", {}, "今日额度"), h("dd", {}, quotaText(s)),
      h("dt", {}, "金额区间"), h("dd", {}, a ? `${money(a.min_amount)} – ${money(a.max_amount)}（商品单价 × 数量）` : "—"),
      h("dt", {}, "配送上限"), h("dd", {}, a ? n(a.effective_max_delivery_days, " 天") : "—"),
      h("dt", {}, "期望卡尾号"), h("dd", {}, a?.expected_card_last4 ?? "未设置"),
      h("dt", {}, "采购开关"), h("dd", {}, a ? (a.purchase_enabled === false ? "已关闭（不接新分配）" : "开启") : "—"),
      h("dt", {}, "模拟限定"), h("dd", {}, hb.simulation_only ? "服务端只允许模拟执行" : "服务端允许真实执行（本插件版本仍只做模拟）"),
      h("dt", {}, "服务端日期"), h("dd", {}, hb.bj_date ? `${hb.bj_date}（北京）` : "—"),
      h("dt", {}, "今天的定时"), h("dd", {}, todaySchedule(s)),
    ),
  );
}

function todaySchedule(s: StateSnapshot): string {
  const a = s.config?.heartbeat.account;
  if (!a || a.purchase_mode !== "scheduled") return "—";
  const slots = normalizeSlots(a.schedule_times);
  if (!slots.length) return "未设置时间";
  const st = s.schedule && s.schedule.bj_date === s.config?.heartbeat.bj_date ? s.schedule : null;
  return slots.map((t) => `${t} ${st?.done.includes(t) ? "已处理" : st?.waiting_logged.includes(t) ? "等待环境" : "未到或待执行"}`).join("；");
}

function render(): void {
  app.replaceChildren();
  const page = h("div", { class: "page" });
  page.append(
    h(
      "div",
      { class: "page-hd" },
      logo(),
      h("div", { class: "grow" }, h("div", { class: "page-title" }, "采购助手 设置"), h("div", { class: "pc-sub" }, `版本 ${state?.ext_version ?? "—"}`)),
      h("span", { class: "pc-tag pc-tag-brand" }, "模拟模式"),
    ),
  );
  const stack = h("div", { class: "stack" });
  page.append(stack);
  if (message) stack.append(h("div", { class: `pc-notice pc-notice-${message.tone}` }, message.text));
  if (!state) {
    stack.append(h("div", { class: "pc-notice pc-notice-danger" }, "插件后台没有响应，请在扩展管理里重新加载插件"));
    app.append(page);
    return;
  }
  stack.append(pairingCard(state));
  const rc = receiptsCard(state);
  if (rc) stack.append(rc);
  stack.append(simulationCard(state));
  const cfg = configCard(state);
  if (cfg) stack.append(cfg);
  const logBox = h("div", { class: "pc-log", role: "log" }, ...renderLogLines(state.logs));
  if (state.logs.length === 0) logBox.append(h("div", {}, "暂无日志"));
  stack.append(card("日志（最近 50 行，北京时间）", logBox));
  app.append(page);
  logBox.scrollTop = logBox.scrollHeight;
}

/** 正在输入时不重画（重画会让输入框失去焦点），离开输入框后补画一次。 */
let dirty = false;
function safeRender(): void {
  const ae = document.activeElement;
  if (ae && (ae.tagName === "INPUT" || ae.tagName === "SELECT") && app.contains(ae)) {
    dirty = true;
    return;
  }
  dirty = false;
  render();
}
app.addEventListener("focusout", () => {
  setTimeout(() => {
    if (dirty) safeRender();
  }, 0);
});

onStateChange(() => {
  void fetchState().then((s) => {
    state = s;
    safeRender();
  });
});
void refresh();
keepFresh("打开设置页", () => state);
setInterval(() => safeRender(), 30_000);
