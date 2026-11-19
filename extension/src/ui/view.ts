/** 三处界面（页面内面板、设置页、弹出页）共用的展示：状态 → DOM，以及一个只用 textContent 的 DOM 构造器
 *  （动态内容从不拼进 innerHTML）。「什么状态说什么话、给什么按钮」的判断在 core/status.ts（Node 里可测），
 *  这里只负责画。 */

import { codeLabel } from "../core/codes.js";
import type { StateSnapshot } from "../core/messages.js";
import { receiptText } from "../core/receipts.js";
import { normalizeSlots } from "../core/schedule.js";
import { runViewActive, startGate } from "../core/startgate.js";
import { ACTION_LABEL, loginLabel, panelStatus, type PanelStatus, type StatusAction, type Tone } from "../core/status.js";
import { bjClock, bjShort } from "../core/time.js";
import type { LogLine } from "../core/log.js";

export type { Tone } from "../core/status.js";

/** 心跳超过这么久没成功，就不再显示「已连接」。 */
const STALE_MS = 3 * 60_000;

/** 输入：状态快照、当前时刻 → 输出：「服务端」一行的文案与色调（细节与下一步在状态区）。 */
export function connectionText(s: StateSnapshot, now: number): { text: string; tone: Tone } {
  const c = s.connection;
  if (!c) return { text: "正在连接", tone: "muted" };
  if (!c.ok) return { text: `连接失败：${c.kind === "network" ? "连不上服务端" : c.message ?? codeLabel(c.code)}`, tone: "danger" };
  if (now - c.at > STALE_MS) return { text: `最近联系 ${bjShort(c.at) ?? "—"}，之后没有心跳`, tone: "warning" };
  return { text: `已连接 · ${bjClock(c.at)} 心跳`, tone: "success" };
}

/** 输入：状态快照 → 输出：今日额度文案（读不到就说读不到，不写 0）。 */
export function quotaText(s: StateSnapshot): string {
  const q = s.config?.heartbeat.account?.quota_today;
  if (!q) return "—";
  const n = (v: number | null): string => (v === null ? "—" : String(v));
  return `已用 ${n(q.used)} / 上限 ${n(q.limit)} 单，计划 ${n(q.planned)}，剩余 ${n(q.available)}`;
}

/** 输入：状态快照 → 输出：执行方式文案（定时则列出北京时间）。 */
export function scheduleText(s: StateSnapshot): string {
  const a = s.config?.heartbeat.account;
  if (!a) return "—";
  if (a.purchase_mode === "scheduled") {
    const slots = normalizeSlots(a.schedule_times);
    return slots.length ? `定时 ${slots.join("、")}（北京时间）` : "定时（未设置时间）";
  }
  if (a.purchase_mode === "manual") return "手动";
  return "—"; // 认不出的执行方式不原样显示
}

/** 输入：状态快照 → 输出：「账号」一行（与控制台「浏览器」页签的「当前登录」同一套说法）。 */
export function accountText(s: StateSnapshot): string {
  const hb = s.config?.heartbeat;
  if (!s.device.registered || !hb) return "—";
  const state = s.identity?.conflict ? "conflict" : hb.login.state;
  return loginLabel(state, hb.login.customer_id, hb.account?.name ?? null, hb.account?.execution_paused === true);
}

/** 输入：状态快照 → 输出：「开始执行」现在能不能点，不能点时给原因（规则见 core/startgate.ts）。 */
export const canStart = startGate;

/** 输入：状态快照 → 输出：本浏览器是不是正在执行（本地记录且没被更新的服务端心跳否定）。 */
export const runActive = runViewActive;

/** 输入：状态快照 → 输出：面板此刻的状态、文案与按钮（规格 §5，见 core/status.ts）。 */
export const statusOf = panelStatus;

/** 输入：状态快照 → 输出：待补交回执的提示行（没有为空数组）。 */
export function receiptLines(s: StateSnapshot): string[] {
  return (s.receipts ?? []).map(receiptText);
}

/** 输入：日志行 → 输出：`HH:MM:SS` 北京时间前缀。 */
export function logTime(l: LogLine): string {
  return bjClock(l.at);
}

type Child = Node | string | null | undefined | false;

/** 输入：标签名、属性、子节点 → 输出：元素。文本一律走 textContent。 */
export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Record<string, string | boolean | ((ev: Event) => void) | undefined> = {},
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === false) continue;
    if (typeof v === "function") el.addEventListener(k.replace(/^on/, ""), v);
    else if (v === true) el.setAttribute(k, "");
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v);
  }
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    el.append(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return el;
}

/** 输入：日志行列表 → 输出：日志窗口里的行节点。 */
export function renderLogLines(lines: readonly LogLine[]): HTMLElement[] {
  return lines.map((l) => h("div", { class: l.level }, h("span", { class: "t" }, logTime(l)), l.text));
}

export const TONE_DOT: Record<Tone, string> = {
  success: "pc-dot pc-dot-green",
  warning: "pc-dot pc-dot-yellow",
  danger: "pc-dot pc-dot-red",
  muted: "pc-dot",
  brand: "pc-dot pc-dot-green",
};

export const TONE_NOTICE: Record<Tone, string> = {
  success: "pc-notice pc-notice-success",
  warning: "pc-notice pc-notice-warning",
  danger: "pc-notice pc-notice-danger",
  muted: "pc-notice pc-notice-muted",
  brand: "pc-notice pc-notice-brand",
};

/** 输入：面板状态、各按钮的处理函数（没给处理函数的按钮不画）、是否禁用 → 输出：状态区的节点：
 *  一段正文、追加的说明、完整的买家号 ID（可选中）、这个状态带的按钮。 */
export function statusBlock(st: PanelStatus, on: Partial<Record<StatusAction, () => void>>, disabled = false): HTMLElement {
  const box = h("div", { class: `${TONE_NOTICE[st.tone]} pc-status` }, h("div", {}, st.text));
  for (const line of st.extra) box.append(h("div", { class: "pc-status-extra" }, line));
  if (st.customer_id) {
    box.append(h("div", { class: "pc-idline" }, h("span", { class: "pc-idline-k" }, "买家号 ID"), h("code", { class: "pc-code pc-id", title: "买家号 ID（亚马逊 customerId）" }, st.customer_id)));
  }
  const btns = st.actions.filter((a) => on[a]).map((a) => h("button", { class: "pc-btn pc-btn-sm", type: "button", disabled, onclick: () => on[a]!() }, ACTION_LABEL[a]));
  if (btns.length) box.append(h("div", { class: "pc-status-actions" }, ...btns));
  return box;
}
