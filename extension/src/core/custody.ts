/** 后台对「当前执行批次」的几条裁决（纯函数，不碰 chrome API）。
 *
 * 引擎跑在亚马逊标签页的内容脚本里：页面一刷新、一跳转、一关闭，引擎就没了，而且来不及说一声。
 * 后台不能只信本地记录的「正在执行」：
 *   · 同一个标签页里来了一张**新加载**的页面（pc.hello，加载时刻晚于批次开始）→ 原来的引擎已经没了；
 *   · 执行页面对 pc.ping 没有答复、或答复说自己没在跑这个批次 → 引擎已经没了；
 *   · 引擎没了之后，批次如果还有结果回执没送到，后台接着续租、补交，送达后再结束批次；
 *     租约预算用完还送不到就放手（服务端清扫；回执留着，连上后作为迟到回执补交）；
 *     没有待送的回执就立刻结束批次（服务端按租约过期同样处理在途尝试，只是更快）。
 */

import type { ActiveRun, PingReply } from "./messages.js";
import { DEFAULT_LEASE_TTL_S } from "./engine.js";

/** 放手之前给租约留的余量。 */
export const CUSTODY_MARGIN_MS = 10_000;

/** 输入：当前批次、发来 pc.hello 的标签页、那一页的加载时刻 → 输出：原来跑批次的引擎是不是已经没了。
 *  hello 每次页面加载只发一次；加载时刻早于批次开始的 hello 来自开始批次的那一页本身（消息迟到），不算。 */
export function helloEndsRun(ar: ActiveRun | null | undefined, tabId: number | undefined, loadedAt: number | null | undefined): boolean {
  if (!ar || tabId === undefined || ar.tab_id !== tabId) return false;
  if (typeof loadedAt !== "number" || !Number.isFinite(loadedAt)) return true;
  return loadedAt > ar.started_at;
}

/** 输入：执行页面对 pc.ping 的答复、当前批次 → 输出：引擎还在不在跑这个批次。
 *  running 而 run_id 为 null：正在申请批次（还没拿到批次号），算在跑。 */
export function pingSaysAlive(reply: unknown, runId: number): boolean {
  if (!reply || typeof reply !== "object") return false;
  const r = reply as Partial<PingReply>;
  return r.running === true && (r.run_id === null || r.run_id === undefined || r.run_id === runId);
}

/** 接管的批次下一步做什么：finish = 结束批次；beat = 续租、等回执送达；release = 放手交给服务端清扫。 */
export type CustodyStep = "finish" | "beat" | "release";

/** 输入：接管的批次、当前时刻、它还有几条回执没送到 → 输出：下一步。 */
export function custodyStep(ar: ActiveRun, now: number, pendingReceipts: number): CustodyStep {
  if (pendingReceipts <= 0) return "finish";
  const ttlMs = (ar.lease_ttl_s ?? DEFAULT_LEASE_TTL_S) * 1000;
  if (now > ar.last_beat_ok_at + ttlMs - CUSTODY_MARGIN_MS) return "release";
  return "beat";
}
