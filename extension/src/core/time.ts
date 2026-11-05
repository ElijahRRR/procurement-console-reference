/** 时间口径的唯一定义处（纯函数，不碰任何 chrome API）。
 *
 * 与服务端 `services/clock` 同一套口径：
 *   · 业务日期、执行时间、额度切日一律按**北京时间**（Asia/Shanghai，UTC+8，无夏令时）；
 *   · 配送天数按亚马逊站点日期（America/Los_Angeles，有夏令时，必须走 Intl）。
 *
 * 电脑本身在哪个时区都不改变这些含义（落地方案 §7.2「另一台电脑处于不同时区」）。
 * 所以这里**从不**用 `Date#getHours()` 这类按本机时区取值的方法。
 */

/** 北京时间相对 UTC 的偏移。中国自 1991 年起不再实行夏令时，固定 +8。 */
export const BJ_OFFSET_MS = 8 * 3600_000;
export const DAY_MS = 24 * 3600_000;

const pad = (n: number, w = 2): string => String(n).padStart(w, "0");

/** 输入：epoch 毫秒 → 输出：北京时间的各个分量（年、月、日、时、分、秒）。 */
export function bjParts(ms: number): { y: number; mo: number; d: number; h: number; mi: number; s: number } {
  const t = new Date(ms + BJ_OFFSET_MS);
  return {
    y: t.getUTCFullYear(),
    mo: t.getUTCMonth() + 1,
    d: t.getUTCDate(),
    h: t.getUTCHours(),
    mi: t.getUTCMinutes(),
    s: t.getUTCSeconds(),
  };
}

/** 输入：epoch 毫秒 → 输出：北京日期 `YYYY-MM-DD`。 */
export function bjDate(ms: number): string {
  const p = bjParts(ms);
  return `${p.y}-${pad(p.mo)}-${pad(p.d)}`;
}

/** 输入：epoch 毫秒 → 输出：北京日期 `YYYYMMDD`（模拟单号用）。 */
export function bjCompact(ms: number): string {
  return bjDate(ms).replace(/-/g, "");
}

/** 输入：epoch 毫秒 → 输出：北京时间 `HH:MM:SS`（日志行用）。 */
export function bjClock(ms: number): string {
  const p = bjParts(ms);
  return `${pad(p.h)}:${pad(p.mi)}:${pad(p.s)}`;
}

/** 输入：epoch 毫秒或 ISO 字符串 → 输出：北京时间 `MM-DD HH:MM`；解析不了返回 null。 */
export function bjShort(v: number | string | null | undefined): string | null {
  const ms = typeof v === "number" ? v : parseIsoMs(v);
  if (ms === null || !Number.isFinite(ms)) return null;
  const p = bjParts(ms);
  return `${pad(p.mo)}-${pad(p.d)} ${pad(p.h)}:${pad(p.mi)}`;
}

/** 输入：北京日期 `YYYY-MM-DD` → 输出：这一天北京时间 00:00 的 epoch 毫秒；格式不对返回 null。 */
export function bjMidnight(date: string): number | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  if (!m) return null;
  return Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])) - BJ_OFFSET_MS;
}

/** 输入：ISO 8601 字符串 → 输出：epoch 毫秒；空或解析失败返回 null（不当成 0）。 */
export function parseIsoMs(v: string | null | undefined): number | null {
  if (typeof v !== "string" || v.trim() === "") return null;
  const ms = Date.parse(v);
  return Number.isFinite(ms) ? ms : null;
}

let laFormatter: Intl.DateTimeFormat | null = null;

/** 输入：epoch 毫秒 → 输出：亚马逊美国站（洛杉矶）的日期 `YYYY-MM-DD`。
 *
 *  洛杉矶有夏令时，不能用固定偏移；交给 Intl（Chrome 与 Node 都带完整时区数据）。 */
export function laDate(ms: number): string {
  if (!laFormatter) {
    laFormatter = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/Los_Angeles",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
  }
  const parts = laFormatter.formatToParts(new Date(ms));
  const get = (t: string): string => parts.find((p) => p.type === t)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")}`;
}

/** 输入：日期 `YYYY-MM-DD` 与天数 → 输出：加上天数后的日期（纯日历运算，与时区无关）。 */
export function addDays(date: string, days: number): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  if (!m) throw new Error(`日期格式不对：${date}`);
  const t = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])) + days * DAY_MS);
  return `${t.getUTCFullYear()}-${pad(t.getUTCMonth() + 1)}-${pad(t.getUTCDate())}`;
}
