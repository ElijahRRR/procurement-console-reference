/** 插件本地设置与输入校验（纯函数）。
 *
 * 设置存在 chrome.storage.local，由设置页修改。设备令牌单独存（见 background/state.ts），
 * 只在后台使用，永不显示、永不写日志。
 */

import { normalizeOutcome, type SimOutcome } from "./simulate.js";
import type { Browser } from "./types.js";

export interface Settings {
  serverUrl: string;
  simulationOutcome: SimOutcome;
}

export const DEFAULT_SERVER_URL = "http://127.0.0.1:8790";

export const DEFAULT_SETTINGS: Settings = {
  serverUrl: DEFAULT_SERVER_URL,
  simulationOutcome: "pass",
};

export const BROWSER_OPTIONS: ReadonlyArray<{ value: Browser; label: string }> = [
  { value: "chrome", label: "Chrome" },
  { value: "adspower", label: "AdsPower" },
  { value: "other", label: "其他" },
];

/** 输入：存下来的任意值 → 输出：补全默认值后的设置。 */
export function normalizeSettings(v: unknown): Settings {
  const o = v && typeof v === "object" ? (v as Record<string, unknown>) : {};
  const url = typeof o.serverUrl === "string" && validateServerUrl(o.serverUrl).ok ? normalizeServerUrl(o.serverUrl) : DEFAULT_SERVER_URL;
  return { serverUrl: url, simulationOutcome: normalizeOutcome(o.simulationOutcome) };
}

/** 输入：服务端地址 → 输出：去掉首尾空白与末尾斜杠后的地址。 */
export function normalizeServerUrl(s: string): string {
  return s.trim().replace(/\/+$/, "");
}

/** 输入：服务端地址 → 输出：能不能用，不能用时给出原因。
 *
 *  只允许本机地址（http://127.0.0.1 或 http://localhost，端口任意）：
 *  插件的主机权限只申请了这两个（不申请 <all_urls>），别的地址请求会被浏览器拦下。 */
export function validateServerUrl(s: string): { ok: true } | { ok: false; reason: string } {
  let u: URL;
  try {
    u = new URL(normalizeServerUrl(s));
  } catch {
    return { ok: false, reason: "地址格式不正确，例如 http://127.0.0.1:8790" };
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") return { ok: false, reason: "只支持 http 或 https 地址" };
  if (u.hostname !== "127.0.0.1" && u.hostname !== "localhost") {
    return { ok: false, reason: "当前版本只支持本机服务端（127.0.0.1 或 localhost）" };
  }
  if (u.pathname !== "/" && u.pathname !== "") return { ok: false, reason: "只填协议、主机和端口，不要带路径" };
  return { ok: true };
}

/** 输入：用户输入的配对码 → 输出：去掉空白与短横后的配对码（不改大小写）。 */
export function normalizePairingCode(s: string): string {
  return s.replace(/[\s-]+/g, "");
}

/** 输入：配对码 → 输出：是不是 8 位字母或数字。 */
export function isValidPairingCode(s: string): boolean {
  return /^[A-Za-z0-9]{8}$/.test(normalizePairingCode(s));
}

/** 输入：任意值 → 输出：合法的浏览器类型（认不出按 other）。 */
export function normalizeBrowser(v: unknown): Browser {
  return v === "chrome" || v === "adspower" ? v : "other";
}

/** 输入：设备名 → 输出：去掉首尾空白、限制 60 字以内的设备名；空则返回 null。 */
export function normalizeDeviceName(s: string): string | null {
  const t = s.trim().slice(0, 60);
  return t === "" ? null : t;
}
