/** 日志行（面板与设置页的日志窗口的数据源）。
 *
 * 早期实现的教训：参考插件的 catch 一律吞掉，失败在界面上完全不可见。这里反过来：
 * 每一条失败都进日志，面板实时显示最近 50 行。日志存在 chrome.storage.local，
 * 后台重启不丢；写入由后台在串行闸里做，避免两条消息互相覆盖。
 */

export type LogLevel = "info" | "ok" | "warn" | "err";

export interface LogLine {
  /** epoch 毫秒；显示时换算成北京时间。 */
  at: number;
  level: LogLevel;
  text: string;
}

export const LOG_CAP = 50;

const LEVELS: ReadonlySet<string> = new Set(["info", "ok", "warn", "err"]);

/** 输入：已有日志、新的一行、上限 → 输出：追加后只保留最近 cap 行的新数组（不修改入参）。 */
export function appendLog(lines: readonly LogLine[] | null | undefined, line: LogLine, cap = LOG_CAP): LogLine[] {
  const base = Array.isArray(lines) ? lines.filter(isLogLine) : [];
  const out = [...base, line];
  return out.length > cap ? out.slice(out.length - cap) : out;
}

/** 输入：任意值 → 输出：它是不是一条合法日志行（存储可能被手改坏）。 */
export function isLogLine(v: unknown): v is LogLine {
  if (!v || typeof v !== "object") return false;
  const o = v as Record<string, unknown>;
  return typeof o.at === "number" && typeof o.text === "string" && LEVELS.has(String(o.level));
}

/** 输入：任意级别文本 → 输出：合法级别（认不出按 info）。 */
export function normalizeLevel(v: unknown): LogLevel {
  return LEVELS.has(String(v)) ? (v as LogLevel) : "info";
}
