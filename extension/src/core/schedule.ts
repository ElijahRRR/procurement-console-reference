/** 定时执行的时间算术（纯函数）。
 *
 * 规则（方案 v2 §1 默认值 4，落地方案 §7.2）：
 *   · 控制台给的执行时间（如 `["14:00"]`）一律按**北京时间**理解，与电脑所在时区无关；
 *   · 到点只代表「该执行了」，真正能不能执行要看有没有打开的亚马逊页面；
 *   · 错过的时间点在**同一个北京日期**内、插件上线时补执行一次；跨天不补。
 *
 * 闹钟（chrome.alarms）只负责把后台叫醒；「哪些时间点该执行」完全由这里的
 * `dueSlots` 根据当前时刻与当天已执行记录判定。这样闹钟丢了、晚了、电脑睡过了，
 * 结论都一样 —— 不依赖闹钟准时。
 *
 * 「一个时间点一天只执行一次」的权威在服务端：定时批次带着时间点（`POST /runs {trigger: "scheduled", slot}`），
 * 服务端按（账号、北京日期、时间点）去重，重复时回 SLOT_DONE。本地的当天记录只用来少发请求，
 * 而且**按买家号 ID 分键**（ScheduleBook）：同一个浏览器当天先后登录两个买家号时，一个账号处理过的
 * 时间点不能算到另一个账号头上。
 */

import { DAY_MS, bjDate, bjMidnight } from "./time.js";

/** 输入：一个执行时间文本 → 输出：{时, 分}；不是合法的 `H:MM` / `HH:MM`（可带秒）返回 null。 */
export function parseSlot(s: unknown): { h: number; m: number } | null {
  if (typeof s !== "string") return null;
  const m = /^\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*$/.exec(s);
  if (!m) return null;
  const h = Number(m[1]);
  const mi = Number(m[2]);
  if (h > 23 || mi > 59) return null;
  return { h, m: mi };
}

/** 输入：服务端下发的执行时间列表（可能残缺）→ 输出：去重、规范成 `HH:MM` 并按时间排序的列表。 */
export function normalizeSlots(times: unknown): string[] {
  if (!Array.isArray(times)) return [];
  const out = new Set<string>();
  for (const t of times) {
    const p = parseSlot(t);
    if (p) out.add(`${String(p.h).padStart(2, "0")}:${String(p.m).padStart(2, "0")}`);
  }
  return [...out].sort();
}

/** 输入：北京日期 `YYYY-MM-DD` 与执行时间 `HH:MM` → 输出：该时刻的 epoch 毫秒；任一不合法返回 null。 */
export function slotEpoch(date: string, slot: string): number | null {
  const base = bjMidnight(date);
  const p = parseSlot(slot);
  if (base === null || p === null) return null;
  return base + p.h * 3600_000 + p.m * 60_000;
}

/** 输入：执行时间列表与当前时刻 → 输出：每个时间点下一次触发的 epoch 毫秒（升序、去重）。
 *
 *  「下一次」严格晚于 now：恰好在 14:00:00.000 调用时给的是明天 14:00 ——
 *  今天这一次由 `dueSlots` 判为到期，不会因此漏掉。
 *  北京时间没有夏令时，所以「明天同一时刻」恒等于 +24 小时。 */
export function nextFireTimes(scheduleTimes: unknown, now: number): number[] {
  return nextFirePlan(scheduleTimes, now).map((p) => p.at);
}

/** 输入：执行时间列表与当前时刻 → 输出：`[{slot, at}]`，at 为下一次触发时刻，按 at 升序。 */
export function nextFirePlan(scheduleTimes: unknown, now: number): Array<{ slot: string; at: number }> {
  const today = bjDate(now);
  const plan: Array<{ slot: string; at: number }> = [];
  for (const slot of normalizeSlots(scheduleTimes)) {
    let at = slotEpoch(today, slot);
    if (at === null) continue;
    if (at <= now) at += DAY_MS;
    plan.push({ slot, at });
  }
  return plan.sort((a, b) => a.at - b.at);
}

/** 当天定时执行的记录（存 chrome.storage.local，跨后台重启保留）。 */
export interface ScheduleDayState {
  /** 记录所属的北京日期。与今天不同 = 过期记录，整份作废（跨天不补）。 */
  bj_date: string;
  /** 今天已经处理过的时间点（已开始执行，或已明确不执行）。 */
  done: string[];
  /** 今天已经记过「等待环境」的时间点：同一个时间点只记一次日志，不刷屏。 */
  waiting_logged: string[];
}

/** 输入：已有记录与当前时刻 → 输出：属于今天的记录（跨天则换成空的新记录）。 */
export function dayState(state: ScheduleDayState | null | undefined, now: number): ScheduleDayState {
  const today = bjDate(now);
  if (!state || state.bj_date !== today) return { bj_date: today, done: [], waiting_logged: [] };
  return {
    bj_date: today,
    done: Array.isArray(state.done) ? [...state.done] : [],
    waiting_logged: Array.isArray(state.waiting_logged) ? [...state.waiting_logged] : [],
  };
}

/** 输入：执行时间列表、当前时刻、当天记录 → 输出：今天已经到点、还没处理过的时间点（升序）。
 *
 *  这一个函数同时覆盖「准点触发」和「错过后当天补执行」：
 *  今天 14:00 的时间点，在 14:00 之后、今天之内的任何一次检查里都算到期，
 *  直到被标记处理过；过了北京时间零点，记录换新，昨天没跑的不再补。 */
export function dueSlots(
  scheduleTimes: unknown,
  now: number,
  state: ScheduleDayState | null | undefined,
): string[] {
  const day = dayState(state, now);
  const done = new Set(day.done);
  const out: string[] = [];
  for (const slot of normalizeSlots(scheduleTimes)) {
    const at = slotEpoch(day.bj_date, slot);
    if (at !== null && at <= now && !done.has(slot)) out.push(slot);
  }
  return out;
}

/** 输入：当天记录、要标记的时间点、当前时刻 → 输出：新的当天记录（不修改入参）。 */
export function markSlots(
  state: ScheduleDayState | null | undefined,
  slots: readonly string[],
  now: number,
  field: "done" | "waiting_logged" = "done",
): ScheduleDayState {
  const day = dayState(state, now);
  const set = new Set(day[field]);
  for (const s of slots) set.add(s);
  return { ...day, [field]: [...set].sort() };
}

/** 各买家号的当天记录：买家号 ID → 当天记录（存 chrome.storage.local）。 */
export type ScheduleBook = Record<string, ScheduleDayState>;

const isDayState = (v: unknown): v is ScheduleDayState =>
  !!v && typeof v === "object" && typeof (v as ScheduleDayState).bj_date === "string";

/** 输入：存下来的任意值 → 输出：合法的记录本。旧版（不分买家号的单份记录，顶层就有 bj_date）整份作废：
 *  不知道它是哪个买家号的，宁可多问服务端一次（它会回 SLOT_DONE），也不把一个账号的记录算给另一个。 */
export function normalizeBook(v: unknown): ScheduleBook {
  if (!v || typeof v !== "object" || Array.isArray(v) || isDayState(v)) return {};
  const out: ScheduleBook = {};
  for (const [k, x] of Object.entries(v as Record<string, unknown>)) if (k && isDayState(x)) out[k] = x;
  return out;
}

/** 输入：记录本、买家号 ID、当前时刻 → 输出：这个买家号今天的记录（没有或跨天了就是空的新记录）。 */
export function bookDay(book: ScheduleBook | null | undefined, customerId: string, now: number): ScheduleDayState {
  return dayState(normalizeBook(book)[customerId], now);
}

/** 输入：记录本、买家号 ID、它的新记录、当前时刻 → 输出：新的记录本（不修改入参）。
 *  顺手丢掉别的买家号不是今天的记录，记录本不会越积越多。 */
export function bookPut(book: ScheduleBook | null | undefined, customerId: string, state: ScheduleDayState, now: number): ScheduleBook {
  const today = bjDate(now);
  const out: ScheduleBook = {};
  for (const [k, v] of Object.entries(normalizeBook(book))) if (v.bj_date === today) out[k] = v;
  out[customerId] = state;
  return out;
}

/** 输入：今天到期的时间点（升序）→ 输出：向服务端申请定时批次时带哪一个时间点：最晚的那个。
 *  一个批次会把今天能执行的任务都跑掉，错过的几个时间点合成一次；各浏览器都报最晚的那个，
 *  服务端对它去重（SLOT_DONE），不会因为各自报了不同的时间点而各跑一次。没有到期的返回 null。 */
export function slotToClaim(due: readonly string[]): string | null {
  return due.length ? due[due.length - 1]! : null;
}

/** 输入：本机时刻与服务端时刻的偏差（毫秒，服务端减本机）→ 输出：用于闹钟的本机 epoch。
 *
 *  闹钟按本机时钟触发，而「北京时间 14:00」按服务端时钟算更可信（本机时间可能被改过）。
 *  偏差只用来换算闹钟时刻；判定到期仍以 `now + 偏差` 为准。 */
export function toLocalEpoch(serverEpoch: number, offsetMs: number): number {
  return serverEpoch - offsetMs;
}

