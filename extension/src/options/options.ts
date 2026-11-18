/** 设置页。只有这几样（规格 §3、§5）：
 *    · 服务端地址（缺省 http://127.0.0.1:8790，装好不用改就能用）；
 *    · 状态区：连接、本浏览器名称、当前买家号、账号，以及「立即同步」「重新登记」；
 *    · 模拟结局；
 *    · 高级：工作区 ID（只有服务端里有多个工作区时才需要填）。
 *  没有配对码、设备名称、浏览器类型：登记是自动的，浏览器的名字由服务端起、在控制台改。
 *  保存设置、重新登记是特权消息，后台只接受从这一页（扩展自己的页面）发来的。 */

import type { StateSnapshot } from "../core/messages.js";
import { DEFAULT_SERVER_URL, validateServerUrl, validateWorkspaceIdText } from "../core/settings.js";
import { SIM_OUTCOMES, type SimOutcome } from "../core/simulate.js";
import type { StatusAction } from "../core/status.js";
import { bjShort } from "../core/time.js";
import { copyText, fetchState, keepFresh, onStateChange, toSw } from "../ui/bridge.js";
import { LOGO_SVG, themeCss } from "../ui/theme.js";
import { TONE_DOT, accountText, connectionText, h, receiptLines, statusBlock, statusOf } from "../ui/view.js";

const PAGE_CSS = `
body { margin: 0; background: var(--pc-bg-page); color: var(--pc-text-body); font: 400 14px/20px var(--pc-font-sans); -webkit-font-smoothing: antialiased; }
.page { max-width: 760px; margin: 0 auto; padding: 32px 24px 48px; }
.page-hd { display: flex; align-items: center; gap: 12px; margin-bottom: 24px; }
.page-hd .pc-logo { width: 40px; height: 40px; }
.page-title { font: 700 24px/32px var(--pc-font-sans); color: var(--pc-text-strong); }
.stack { display: flex; flex-direction: column; gap: 24px; }
.card-title { font: 600 18px/28px var(--pc-font-sans); color: var(--pc-text-strong); }
.btns { display: flex; gap: 8px; flex-wrap: wrap; }
.pc-kv { grid-template-columns: 120px 1fr; font-size: 14px; }
.desc { margin: 0 0 16px; color: var(--pc-text-muted); font-size: 13px; line-height: 20px; }
.hint { color: var(--pc-text-muted); font-size: 12px; line-height: 16px; margin-top: 6px; }
.grow { flex: 1; min-width: 0; }
.inline { display: flex; gap: 8px; align-items: flex-start; }
.inline .pc-input { flex: 1; }
.gap { margin-top: 12px; }
`;

const app = document.getElementById("app")!;
const style = document.createElement("style");
style.textContent = themeCss(":root") + PAGE_CSS;
document.head.append(style);

type Reply = { ok: boolean; message: string };

let state: StateSnapshot | null = null;
let message: { tone: "danger" | "brand" | "warning"; text: string } | null = null;
let pending = false;
// 两个输入框的内容在重画之间保留。
const form = { serverUrl: "", workspaceId: "" };
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

function show(tone: "danger" | "brand" | "warning", text: string): void {
  message = { tone, text };
  render();
}

async function refresh(): Promise<void> {
  state = await fetchState();
  if (state && !formInit) {
    form.serverUrl = state.settings.serverUrl ?? DEFAULT_SERVER_URL;
    form.workspaceId = state.settings.workspaceId === null ? "" : String(state.settings.workspaceId);
    formInit = true;
  }
  render();
}

/** 发一条要等结果的消息；后台没有响应时给一句「动作 + 失败：原因」。 */
async function ask(msg: Parameters<typeof toSw>[0], what: string): Promise<Reply> {
  pending = true;
  render();
  try {
    const r = await toSw<Reply>(msg, 90_000);
    return r && typeof r.ok === "boolean" ? r : { ok: false, message: `${what}失败：插件后台没有给出答复` };
  } catch {
    return { ok: false, message: `${what}失败：插件后台没有响应，请重新加载插件后再试` };
  } finally {
    pending = false;
  }
}

async function saveServerUrl(): Promise<void> {
  const v = validateServerUrl(form.serverUrl);
  if (!v.ok) return show("danger", `保存失败：${v.reason}`);
  const changed = !!state && form.serverUrl.trim().replace(/\/+$/, "") !== state.settings.serverUrl;
  if (changed && state?.device.registered && !window.confirm("确定要修改服务端地址吗？修改后这个浏览器会在新地址重新登记。")) return;
  const r = await ask({ type: "pc.saveSettings", patch: { serverUrl: form.serverUrl } }, "保存");
  formInit = !r.ok; // 保存成功后按后台规范化过的值重填
  show(r.ok ? "brand" : "danger", r.message);
  await refresh();
}

async function saveWorkspaceId(): Promise<void> {
  const v = validateWorkspaceIdText(form.workspaceId);
  if (!v.ok) return show("danger", `保存失败：${v.reason}`);
  const moving = !!state && state.device.registered && v.value !== null && state.device.workspace_id !== null && state.device.workspace_id !== v.value;
  if (moving && !window.confirm("确定要更换工作区吗？更换后这个浏览器会在新的工作区重新登记。")) return;
  const r = await ask({ type: "pc.saveSettings", patch: { workspaceId: v.value } }, "保存");
  formInit = !r.ok;
  show(r.ok ? "brand" : "danger", r.message);
  await refresh();
}

async function saveOutcome(v: SimOutcome): Promise<void> {
  const r = await ask({ type: "pc.saveSettings", patch: { simulationOutcome: v } }, "保存");
  show(r.ok ? "brand" : "danger", r.ok ? "模拟结局已保存" : r.message);
  await refresh();
}

async function doSync(): Promise<void> {
  pending = true;
  render();
  await toSw({ type: "pc.heartbeatNow" }, 60_000).catch(() => undefined);
  pending = false;
  await refresh();
  if (state?.connection?.ok) show("brand", "已同步");
  else show("danger", `同步失败：${state?.connection?.message ?? "插件后台没有响应"}`);
}

async function doReregister(): Promise<void> {
  const r = await ask({ type: "pc.reregister" }, "重新登记");
  show(r.ok ? "brand" : "danger", r.message);
  await refresh();
}

async function copyId(): Promise<void> {
  const id = state ? statusOf(state).customer_id : null;
  if (!id) return;
  const ok = await copyText(id, document.body);
  show(ok ? "brand" : "warning", ok ? "已复制买家号 ID" : "复制失败：请选中买家号 ID 手动复制");
}

/** 状态区里画哪些状态按钮：「重试」「打开设置」在这一页用「立即同步」与上面的表单代替。 */
const statusActions: Partial<Record<StatusAction, () => void>> = {
  open_orders: () => void toSw({ type: "pc.openOrders" }).catch(() => undefined),
  reread: () => void toSw({ type: "pc.refreshIdentity" }, 30_000).catch(() => undefined).then(() => refresh()),
  copy_id: () => void copyId(),
  open_console: () => void toSw({ type: "pc.openConsole" }).catch(() => undefined),
};

function serverCard(): HTMLElement {
  return card(
    "服务端地址",
    h("p", { class: "desc" }, "控制台所在的地址。服务端在本机、端口没改过时不用动：插件装好就会自动连上，并把这个浏览器登记到服务端。"),
    h(
      "div",
      { class: "pc-field" },
      h("label", { class: "pc-label", for: "server" }, "服务端地址"),
      h(
        "div",
        { class: "inline" },
        input({ id: "server", placeholder: DEFAULT_SERVER_URL, autocomplete: "off", spellcheck: "false" }, form.serverUrl, (v) => { form.serverUrl = v; }),
        h("button", { class: "pc-btn pc-btn-primary", type: "button", disabled: pending, onclick: () => void saveServerUrl() }, "保存"),
      ),
      h("div", { class: "hint" }, `默认 ${DEFAULT_SERVER_URL}。当前版本只支持本机（127.0.0.1 或 localhost）。正在执行、或有结果回执还没送到服务端时不能修改。`),
    ),
  );
}

function statusCard(s: StateSnapshot): HTMLElement {
  const conn = connectionText(s, Date.now());
  const st = statusOf(s);
  const receipts = receiptLines(s);
  const browser = s.device.registered
    ? `${s.device.device_name ?? "—"}（ID ${s.device.device_id ?? "—"}${s.device.workspace_name ? `，工作区 ${s.device.workspace_name}` : ""}${s.device.registered_at ? `，${bjShort(s.device.registered_at)} 登记` : ""}）`
    : "还没有登记";
  const block = statusBlock(st, statusActions, pending);
  block.classList.add("gap");
  return card(
    "状态",
    h(
      "dl",
      { class: "pc-kv" },
      h("dt", {}, "连接"), h("dd", {}, h("span", { class: "pc-row" }, h("span", { class: TONE_DOT[conn.tone] }), conn.text)),
      h("dt", {}, "本浏览器"), h("dd", {}, browser, s.device.registered ? h("div", { class: "hint" }, "名称由服务端起，可以在控制台的「浏览器」页签里改。") : null),
      h("dt", {}, "当前买家号"), h("dd", {}, st.customer_id ? h("code", { class: "pc-code pc-id" }, st.customer_id) : "—"),
      h("dt", {}, "账号"), h("dd", {}, accountText(s)),
      receipts.length ? h("dt", {}, "待补交回执") : null,
      receipts.length ? h("dd", {}, `${receipts.length} 条结果回执还没送到服务端，服务端恢复后自动补交（同一份结果，服务端不会登记两次）`, ...receipts.map((t) => h("div", { class: "hint" }, t))) : null,
    ),
    block,
    h("div", { class: "pc-sep" }),
    h(
      "div",
      { class: "btns" },
      h("button", { class: "pc-btn", type: "button", disabled: pending, onclick: () => void doSync() }, "立即同步"),
      h("button", { class: "pc-btn", type: "button", disabled: pending, title: "带着这个浏览器原来的令牌向服务端再登记一次", onclick: () => void doReregister() }, "重新登记"),
    ),
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
    h("p", { class: "desc" }, "本版本只做模拟执行：插件领取服务端分给当前买家号、已到采购日期的任务，按任务数据推出一份结算信息交给服务端护栏裁决，再走提交登记与结果上报。不打开、不点击、不修改任何亚马逊页面，不会产生真实订单；模拟单号形如 SIM-日期-尝试 ID，不写回飞书，可在控制台一键清除。"),
    h("div", { class: "pc-field" }, h("label", { class: "pc-label", for: "outcome" }, "模拟结局"), sel, h("div", { class: "hint" }, hint)),
  );
}

function advancedCard(): HTMLElement {
  return card(
    "高级",
    h(
      "div",
      { class: "pc-field" },
      h("label", { class: "pc-label", for: "workspace" }, "工作区 ID"),
      h(
        "div",
        { class: "inline" },
        input({ id: "workspace", placeholder: "留空", inputmode: "numeric", autocomplete: "off" }, form.workspaceId, (v) => { form.workspaceId = v; }),
        h("button", { class: "pc-btn", type: "button", disabled: pending, onclick: () => void saveWorkspaceId() }, "保存"),
      ),
      h("div", { class: "hint" }, "只有系统里有多个工作区时才需要填写（控制台「设置」页可以查到）。只有一个工作区时留空。已经登记过的浏览器改填另一个工作区，会在新的工作区重新登记。"),
    ),
  );
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
  stack.append(serverCard(), statusCard(state), simulationCard(state), advancedCard());
  app.append(page);
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
