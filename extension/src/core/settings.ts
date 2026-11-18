/** 插件本地设置与输入校验（纯函数）。
 *
 * 设置存在 chrome.storage.local，由设置页修改（后台只接受扩展自己的页面发来的修改，见 core/messages.isPrivilegedSender）。
 * 设备令牌单独存在扩展源下的 IndexedDB 里（见 background/secrets.ts，内容脚本打不开），
 * 只在后台使用，永不显示、永不写日志。
 *
 * 零设置可用：服务端地址缺省 http://127.0.0.1:8790，装好就自动登记，没有任何必填项。
 */

import { normalizeOutcome, type SimOutcome } from "./simulate.js";

export interface Settings {
  serverUrl: string;
  simulationOutcome: SimOutcome;
  /** 高级：工作区 ID。只有服务端里有多个工作区时才需要填（控制台「设置」页可以查到）；不填为 null。 */
  workspaceId: number | null;
}

export const DEFAULT_SERVER_URL = "http://127.0.0.1:8790";

export const DEFAULT_SETTINGS: Settings = {
  serverUrl: DEFAULT_SERVER_URL,
  simulationOutcome: "pass",
  workspaceId: null,
};

/** 「打开订单页」打开的地址：订单历史页的内联脚本里有 customerId。只在所有者点击时打开。 */
export const ORDER_HISTORY_URL = "https://www.amazon.com/gp/css/order-history";

/** 输入：存下来的任意值 → 输出：补全默认值后的设置。 */
export function normalizeSettings(v: unknown): Settings {
  const o = v && typeof v === "object" ? (v as Record<string, unknown>) : {};
  const url = typeof o.serverUrl === "string" && validateServerUrl(o.serverUrl).ok ? normalizeServerUrl(o.serverUrl) : DEFAULT_SERVER_URL;
  return { serverUrl: url, simulationOutcome: normalizeOutcome(o.simulationOutcome), workspaceId: normalizeWorkspaceId(o.workspaceId) };
}

/** 输入：服务端地址 → 输出：去掉首尾空白与末尾斜杠后的地址。 */
export function normalizeServerUrl(s: string): string {
  return s.trim().replace(/\/+$/, "");
}

/** 输入：服务端地址 → 输出：能不能用，不能用时给出原因。
 *
 *  只允许本机地址（http://127.0.0.1 或 http://localhost，端口任意）：
 *  插件的主机权限只申请了这两个（不申请 <all_urls>），别的地址请求会被浏览器拦下；
 *  服务端的插件接口也只接受本机对端。 */
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

/** 输入：任意值（数字、数字文本、空）→ 输出：正整数的工作区 ID；空、0、负数、小数、别的文本一律为 null。 */
export function normalizeWorkspaceId(v: unknown): number | null {
  if (typeof v === "number") return Number.isSafeInteger(v) && v > 0 ? v : null;
  if (typeof v === "string" && /^\s*\d{1,15}\s*$/.test(v)) {
    const n = Number(v.trim());
    return n > 0 ? n : null;
  }
  return null;
}

/** 输入：设置页里填的工作区 ID 文本 → 输出：能不能用（留空可以）。 */
export function validateWorkspaceIdText(s: string): { ok: true; value: number | null } | { ok: false; reason: string } {
  if (s.trim() === "") return { ok: true, value: null };
  const n = normalizeWorkspaceId(s);
  return n === null ? { ok: false, reason: "工作区 ID 应为正整数（控制台「设置」页可以查到）；不需要时留空" } : { ok: true, value: n };
}

/** 输入：服务端地址 → 输出：「打开控制台」打开的地址（控制台的「账号」页）。 */
export function consoleAccountsUrl(serverUrl: string): string {
  return `${normalizeServerUrl(serverUrl)}/accounts`;
}
