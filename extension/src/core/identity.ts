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
  /** 读到它的那一页的加载时刻（performance.timeOrigin，epoch 毫秒）。内容脚本总会带上；缺失按 at。 */
  loaded_at?: number;
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

// ── 多个标签页的身份读数：按页面加载先后对账 ─────────────────────────────────
//
// 身份只能从页面上读（不读 Cookie），而页面上的导航栏是**加载那一刻**服务端渲染的：一张几小时前打开、
// 一直没刷新的页面，重读一百遍也只是那一刻的样子。所以：
//   · 比「谁更新」只看页面的加载时刻（loaded_at），不看重读的时刻（read_at）；
//     较早加载的页面的读数永远不能覆盖较新加载的页面的读数；
//   · 标签页关掉或离开那一页之后，它的读数不再代表一张开着的页面，但如果它是本浏览器**最新加载**的一份，
//     仍然留作证据（例如：新标签页里登录了另一个买家号又关掉，旧标签页还显示绑定账号 —— 不能因此算一致）；
//   · 「已登录、但没读到买家号 ID」的读数是弱证据：它不制造分歧，也**不取代、不作废**读到 ID 或显示未登录的
//     读数（强证据）—— 同一个标签页跳到一张读不出 ID 的新页面、或开了一张这样的新标签页，之前那份强证据照样留着；
//   · 读数之间不一致时，**绝不报「是绑定账号」**：报加载最新、且不是绑定账号的那一份，
//     服务端据此判为「身份不一致」或「未登录」，拒绝领取；刷新或关掉较早打开的标签页后分歧消除。

/** 一个标签页里、一次页面加载读到的身份。 */
export interface IdentityObservation {
  tab_id: number;
  signed_in: boolean;
  customer_id: string | null;
  page_url: string | null;
  /** 页面的加载时刻：比先后只看它。 */
  loaded_at: number;
  /** 最近一次读到它的时刻：只用来判断上报是否新鲜。 */
  read_at: number;
}

/** 身份读数仓库（后台存在 chrome.storage.session，跨后台重启保留，浏览器重启清空）。 */
export interface IdentityStore {
  /** 开着、而且还停在读数那一页上的标签页：tab_id → 最近一份读数。 */
  live: Record<string, IdentityObservation>;
  /** 已关闭或已离开那一页的读数里留作证据的一份：有强证据时是加载最新的那份强证据，否则是加载最新的一份
   *  （只在比所有开着的强证据都新时才参与对账）。 */
  newest_gone: IdentityObservation | null;
}

/** 对账的结论：要上报的身份，外加从哪来、有没有分歧。 */
export interface ReconciledIdentity extends IdentitySnapshot {
  loaded_at: number;
  /** 取自哪个标签页（已关闭的页面也记它原来的标签页）。 */
  source_tab: number | null;
  /** 参与对账的读数之间不一致。 */
  conflict: boolean;
  /** 一共参考了几份读数。 */
  sources: number;
}

/** 输入：无 → 输出：空仓库。 */
export function emptyIdentityStore(): IdentityStore {
  return { live: {}, newest_gone: null };
}

/** 输入：存下来的任意值 → 输出：合法的仓库（存储被手改坏时退回空仓库）。 */
export function normalizeIdentityStore(v: unknown): IdentityStore {
  if (!v || typeof v !== "object") return emptyIdentityStore();
  const o = v as Partial<IdentityStore>;
  const live: Record<string, IdentityObservation> = {};
  for (const [k, x] of Object.entries(o.live && typeof o.live === "object" ? o.live : {})) {
    if (isObservation(x)) live[k] = x;
  }
  return { live, newest_gone: isObservation(o.newest_gone) ? o.newest_gone : null };
}

function isObservation(v: unknown): v is IdentityObservation {
  if (!v || typeof v !== "object") return false;
  const o = v as Record<string, unknown>;
  return typeof o.tab_id === "number" && typeof o.signed_in === "boolean" && typeof o.loaded_at === "number" && typeof o.read_at === "number";
}

/** 输入：一份读数 → 输出：是不是强证据（显示未登录，或已登录且读到了买家号 ID）。
 *  「已登录、但没读到 ID」是弱证据：不知道登着谁，不能拿来否定一份知道的。 */
function isStrong(o: { signed_in: boolean; customer_id: string | null }): boolean {
  return !o.signed_in || !!o.customer_id;
}

/** 输入：已留作证据的已关闭读数、又一份退出「开着的」读数 → 输出：留哪一份。
 *  弱证据绝不顶替强证据（不管谁加载得晚）；强弱相同时留加载时刻较新的。 */
function keepEvidence(g: IdentityObservation | null, prev: IdentityObservation): IdentityObservation {
  if (!g) return { ...prev };
  const gs = isStrong(g);
  if (gs !== isStrong(prev)) return gs ? g : { ...prev };
  return prev.loaded_at > g.loaded_at ? { ...prev } : g;
}

/** 输入：仓库 → 输出：去掉「已被某个开着的、不比它旧的页面取代」的已关闭读数后的仓库。
 *  强证据只能被强证据取代；弱证据被任何不比它旧的开着的读数取代。 */
function prune(store: IdentityStore): IdentityStore {
  const g = store.newest_gone;
  if (!g) return store;
  const gs = isStrong(g);
  const superseded = Object.values(store.live).some((o) => o.loaded_at >= g.loaded_at && (!gs || isStrong(o)));
  return superseded ? { ...store, newest_gone: null } : store;
}

/** 输入：仓库、一份新读数 → 输出：新仓库。
 *  · 同一页的重读（加载时刻相同）更新读数与 read_at；只是重读时没读到 ID 的话，之前读到的那份留作证据；
 *  · 同一标签页来了新加载的页面：上一页的读数先按「已离开那一页」留作证据（弱证据不顶替强证据），再记新的；
 *  · 较早加载的页面送来的读数（例如页面刷新后，旧页面迟到的一条消息）不覆盖这个标签页现在的读数，
 *    那一页已经离开了，按已离开的页面留作证据（被不比它旧的强证据取代时随即作废）。 */
export function observeIdentity(store: IdentityStore, obs: IdentityObservation): IdentityStore {
  const key = String(obs.tab_id);
  const prev = store.live[key];
  if (prev && obs.loaded_at < prev.loaded_at) {
    return prune({ live: store.live, newest_gone: keepEvidence(store.newest_gone, obs) });
  }
  const replaced = !!prev && (obs.loaded_at > prev.loaded_at || (isStrong(prev) && !isStrong(obs)));
  const newest_gone = replaced ? keepEvidence(store.newest_gone, prev) : store.newest_gone;
  return prune({ live: { ...store.live, [key]: { ...obs } }, newest_gone });
}

/** 输入：仓库、关闭或离开了页面的标签页 → 输出：新仓库。它的读数移出「开着的」，
 *  按 keepEvidence 决定是否顶替已有的已关闭读数留作证据。 */
export function retireIdentityTab(store: IdentityStore, tabId: number): IdentityStore {
  const key = String(tabId);
  const prev = store.live[key];
  if (!prev) return store;
  const live = { ...store.live };
  delete live[key];
  return prune({ live, newest_gone: keepEvidence(store.newest_gone, prev) });
}

type ReadingKind = "out" | "in?" | `in:${string}`;

function kindOf(o: { signed_in: boolean; customer_id: string | null }): ReadingKind {
  if (!o.signed_in) return "out";
  return o.customer_id ? `in:${o.customer_id}` : "in?";
}

/** 输入：仓库、绑定账号的买家号 ID（不知道为 null）、当前时刻 → 输出：要上报的身份；一份读数都没有为 null。
 *
 *  1. 候选 = 所有开着的页面的读数 + 留作证据的那一份已关闭页面的读数（比所有开着的强证据都新时），
 *     按加载时刻从新到旧；
 *  2. 「已登录、但没读到买家号 ID」的读数是弱证据：只要有读到 ID 的已登录读数，它不制造分歧；
 *     它也不取代、不作废读到 ID 或显示未登录的读数（见 observeIdentity / retireIdentityTab）；
 *  3. 候选一致 → 报加载最新的那一份（有 ID 的优先），新鲜度取候选里最近的一次读取；
 *  4. 不一致 → 报加载最新、且不是「已登录绑定账号」的那一份，conflict = true，新鲜度取当前时刻
 *     （这个结论只会让服务端拒绝，不会放行）。 */
export function reconcileIdentity(store: IdentityStore, boundCustomerId: string | null | undefined, now: number): ReconciledIdentity | null {
  const live = Object.values(store.live);
  // 只和开着的**强证据**比：一张读不出 ID 的新页面不能让已关闭页面上的另一个买家号（或未登录）出局。
  const newestStrongLive = live.reduce((m, o) => (isStrong(o) ? Math.max(m, o.loaded_at) : m), -Infinity);
  const cands = [...live];
  if (store.newest_gone && store.newest_gone.loaded_at > newestStrongLive) cands.push(store.newest_gone);
  if (cands.length === 0) return null;
  cands.sort((a, b) => b.loaded_at - a.loaded_at || b.read_at - a.read_at);

  const kinds = new Set(cands.map(kindOf));
  const hasId = [...kinds].some((k) => k.startsWith("in:"));
  if (hasId) kinds.delete("in?");
  const conflict = kinds.size > 1;

  const pick = conflict
    ? cands.find((o) => { const k = kindOf(o); return k !== "in?" && k !== `in:${boundCustomerId ?? ""}`; }) ?? cands[0]!
    : cands.find((o) => o.signed_in && !!o.customer_id) ?? cands[0]!;
  const at = conflict ? now : Math.max(...cands.map((o) => o.read_at));
  return {
    signed_in: pick.signed_in,
    customer_id: pick.signed_in ? pick.customer_id ?? null : null,
    site: "US",
    page_url: pick.page_url ?? null,
    at,
    loaded_at: pick.loaded_at,
    source_tab: pick.tab_id,
    conflict,
    sources: cands.length,
  };
}
