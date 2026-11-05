/** 身份快照（契约 §5.3）与页面类型判断（纯函数）。
 *
 * 身份 = 这个浏览器环境此刻登着哪个亚马逊买家账号。只从页面上读（导航栏与内联脚本里的
 * customerId），**不读 Cookie**：插件不申请 cookies 权限，登录状态留在浏览器里。
 * 不上传页面 HTML，page_url 只留 origin + pathname（不带查询串，查询串里常有订单号等信息）。
 */

import type { Identity, IdentityStatus } from "./types.js";
import type { LoginState } from "../dom/parse.js";

/** 本地保存的身份快照：上报体 + 读取时刻。 */
export interface IdentitySnapshot extends Identity {
  at: number;
}

/** 身份快照多久内算「新鲜」：只把新鲜的快照捎进心跳。
 *  服务端要求开始批次前 10 分钟内上报过身份；一份几小时前读的快照不能每分钟被重新「上报」一次，
 *  那样服务端看到的「最近上报」就是假的。 */
export const IDENTITY_FRESH_MS = 5 * 60_000;

/** 同一份身份多久内不重复单独上报（心跳仍会每分钟捎带）。 */
export const IDENTITY_DEDUPE_MS = 60_000;

/** 输入：页面完整地址 → 输出：只留 origin + pathname 的地址；解析不了返回 null。 */
export function sanitizePageUrl(href: string | null | undefined): string | null {
  if (typeof href !== "string" || href.trim() === "") return null;
  try {
    const u = new URL(href);
    if (u.protocol !== "https:" && u.protocol !== "http:") return null;
    return u.origin + u.pathname;
  } catch {
    return null;
  }
}

/** 输入：登录状态、读到的买家号 ID、页面地址、当前时刻 → 输出：身份快照；
 *  登录状态读不出来（unknown）时返回 null —— 「这一轮没读到」不是一个结论，不上报。 */
export function identityFromPage(
  login: LoginState,
  customerId: string | null | undefined,
  href: string | null | undefined,
  now: number,
): IdentitySnapshot | null {
  if (login === "unknown") return null;
  const signedIn = login === "ok";
  return {
    signed_in: signedIn,
    // 登出的页面上即使残留了 customerId 也不带：那不是「这个浏览器登着谁」。
    customer_id: signedIn ? customerId ?? null : null,
    site: "US",
    page_url: sanitizePageUrl(href),
    at: now,
  };
}

/** 输入：两份快照 → 输出：登录状态与买家号 ID 是否相同（页面地址与时刻不算）。 */
export function sameIdentity(a: Identity | null | undefined, b: Identity | null | undefined): boolean {
  if (!a || !b) return false;
  return a.signed_in === b.signed_in && (a.customer_id ?? null) === (b.customer_id ?? null) && a.site === b.site;
}

/** 输入：快照、当前时刻、有效期 → 输出：快照是否还新鲜。 */
export function isFresh(s: IdentitySnapshot | null | undefined, now: number, maxAgeMs = IDENTITY_FRESH_MS): boolean {
  return !!s && typeof s.at === "number" && now - s.at <= maxAgeMs && now >= s.at - 60_000;
}

/** 输入：快照 → 输出：上报体（去掉本地的读取时刻）。 */
export function toIdentity(s: IdentitySnapshot): Identity {
  return { signed_in: s.signed_in, customer_id: s.customer_id ?? null, site: "US", page_url: s.page_url ?? null };
}

/** 输入：买家号 ID → 输出：日志与界面上显示的遮掩形式（前 4 位 + … + 后 2 位）；完整值只发给服务端。 */
export function maskCustomerId(id: string | null | undefined): string {
  if (!id) return "—";
  if (id.length <= 6) return id;
  return `${id.slice(0, 4)}…${id.slice(-2)}`;
}

/** 身份状态的中文说明（契约 §5.3：match / mismatch / signed_out / unknown）。 */
export const IDENTITY_STATUS_TEXT: Record<IdentityStatus, { short: string; long: string; tone: "success" | "danger" | "warning" | "muted" }> = {
  match: { short: "身份一致", long: "已登录绑定的买家账号", tone: "success" },
  mismatch: { short: "身份不一致", long: "当前浏览器登录的亚马逊账号不是绑定账号，服务端不会下发任务", tone: "danger" },
  signed_out: { short: "未登录亚马逊", long: "请在本浏览器登录绑定的买家账号", tone: "warning" },
  unknown: { short: "身份未知", long: "账号还没有登记买家号 ID，或还没有收到身份上报；可在控制台「采用插件识别的 ID」", tone: "muted" },
};

/** 输入：页面路径 → 输出：是不是订单历史页（这些页面上默认展开面板）。 */
export function isOrderHistoryPath(pathname: string | null | undefined): boolean {
  if (typeof pathname !== "string") return false;
  return (
    pathname === "/your-orders" ||
    pathname.startsWith("/your-orders/") ||
    pathname.startsWith("/gp/css/order-history") ||
    pathname.startsWith("/gp/your-account/order-history")
  );
}
