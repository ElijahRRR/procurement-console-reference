/** 三处界面（页面内面板、设置页、弹出页）共用的展示逻辑：状态 → 中文文案与色调，
 *  以及一个只用 textContent 的 DOM 构造器（动态内容从不拼进 innerHTML）。 */

import { IDENTITY_STATUS_TEXT } from "../core/identity.js";
import type { StateSnapshot } from "../core/messages.js";
import { normalizeSlots } from "../core/schedule.js";
import { bjClock, bjShort } from "../core/time.js";
import type { LogLine } from "../core/log.js";

export type Tone = "success" | "warning" | "danger" | "muted" | "brand";

/** 心跳超过这么久没成功，就不再显示「已连接」。 */
const STALE_MS = 3 * 60_000;

/** 输入：状态快照、当前时刻 → 输出：服务端连接的文案与色调。 */
export function connectionText(s: StateSnapshot, now: number): { text: string; tone: Tone } {
  if (!s.device.paired) return { text: "未配对", tone: "muted" };
  const c = s.connection;
  if (!c) return { text: "正在连接", tone: "muted" };
  if (!c.ok) return { text: `连接失败：${c.message ?? c.code ?? "未知原因"}`, tone: "danger" };
  if (now - c.at > STALE_MS) return { text: `最近联系 ${bjShort(c.at) ?? "—"}，之后没有心跳`, tone: "warning" };
  return { text: `已连接 · ${bjClock(c.at)} 心跳`, tone: "success" };
}

/** 输入：状态快照 → 输出：身份状态的短文案、说明与色调。 */
export function identityText(s: StateSnapshot): { short: string; long: string; tone: Tone } {
  const t = IDENTITY_STATUS_TEXT[s.identity_status];
  return { short: t.short, long: t.long, tone: t.tone };
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
  return a.purchase_mode ?? "—";
}

/** 输入：状态快照 → 输出：绑定账号文案。 */
export function accountText(s: StateSnapshot): string {
  if (!s.device.paired) return "—";
  const hb = s.config?.heartbeat;
  if (!hb) return "等待心跳";
  if (!hb.account) return "未绑定（请在控制台「账号与设备」里绑定）";
  const paused = hb.account.execution_paused ? `，已暂停执行${hb.account.pause_reason ? `：${hb.account.pause_reason}` : ""}` : "";
  return `${hb.account.name}${paused}`;
}

/** 输入：状态快照 → 输出：「开始执行」现在能不能点，不能点时给原因。 */
export function canStart(s: StateSnapshot): { ok: boolean; reason: string | null } {
  if (!s.device.paired) return { ok: false, reason: "尚未配对，请先在设置页完成配对" };
  if (s.run?.active) return { ok: false, reason: "本浏览器正在执行" };
  const hb = s.config?.heartbeat;
  if (hb && !hb.account) return { ok: false, reason: "本设备还没有绑定买家账号" };
  return { ok: true, reason: null };
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
