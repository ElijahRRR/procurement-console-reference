/** 标签页级执行租约的裁决规则（纯函数，移植自早期实现 core/lease.ts）。
 *
 * 一个浏览器环境里可能同时开着好几个 amazon.com 标签页。服务端保证「一个账号同一时刻
 * 只有一个执行批次」（全局权威）；这一层只在本浏览器内部再挡一道：同一时刻只有一个
 * 标签页去向服务端申请批次，免得两个标签页前后脚去抢、一个拿到批次一个拿到 409。
 *
 * 早期实现在这里踩过的三个坑，合起来才是一个洞：
 *   1. 租约放在 service worker 的模块级变量里 —— MV3 后台空闲约 30 秒就被回收，
 *      唤醒后变量归零，「没有租约」对任何标签页都成立，谁先问谁拿走。
 *      所以租约存在 chrome.storage.session 里（跨后台重启保留、不落盘），
 *      由后台在串行闸里「读 → 裁决 → 写」。
 *   2. 有效期短于后台标签页的定时器节流周期（约 60 秒）：正在执行的标签页一被切到
 *      后台，续租就赶不上。所以有效期取 5 分钟。
 *   3. 过期就换手，不问持有者是不是正在执行。这里：持有者报告忙、标签页还在，
 *      在宽限期内不换手；宽限期也有界，一个死掉的内容脚本不能永远占着。
 */

export interface Lease {
  tabId: number;
  /** 到期时刻（epoch 毫秒）。 */
  until: number;
  /** 持有者上一次续租时说的：我正在执行。 */
  busy: boolean;
}

export interface LeaseAsk {
  tabId: number;
  /** 来要租约的这个标签页此刻是否正在执行。 */
  busy: boolean;
  now: number;
  /** 持有者那个标签页还在不在（chrome.tabs.get 查得到就是 true）。
   *  查不了的时候传 true —— 「不知道」不该变成「可以抢」。 */
  holderAlive: boolean;
}

export interface LeaseVerdict {
  granted: boolean;
  /** 该写回存储的那一份；granted 为 false 时是 null（不动存储）。 */
  next: Lease | null;
  /** 为什么。只用来写日志，但要写得出人话。 */
  reason: "fresh" | "renew" | "taken-over" | "holder-gone" | "held" | "held-busy";
}

export interface LeaseRules {
  /** 租约有效期：比后台标签页的定时器节流周期（约 60 秒）宽一个量级。 */
  ttlMs: number;
  /** 持有者报告忙、却再也没来续租时，还给它多少宽限。必须有界。 */
  busyGraceMs: number;
}

export const LEASE_DEFAULTS: LeaseRules = {
  ttlMs: 5 * 60_000,
  busyGraceMs: 10 * 60_000,
};

/** 输入：当前租约（没有为 null）、谁在要、两个时间预算 → 输出：给不给，以及该存回什么。 */
export function decideLease(
  cur: Lease | null,
  ask: LeaseAsk,
  rules: LeaseRules = LEASE_DEFAULTS,
): LeaseVerdict {
  const grant = (reason: LeaseVerdict["reason"]): LeaseVerdict => ({
    granted: true,
    next: { tabId: ask.tabId, until: ask.now + rules.ttlMs, busy: ask.busy },
    reason,
  });

  if (cur === null) return grant("fresh");
  if (cur.tabId === ask.tabId) return grant("renew");
  if (ask.now <= cur.until) return { granted: false, next: null, reason: "held" };

  // 过期了。持有者说它在执行的话，宽限期内不换手 —— 后台节流会让续租迟到，
  // 而把租约从一个正在执行的标签页手里抢走，代价是两个标签页同时申请批次。
  if (cur.busy && ask.holderAlive && ask.now <= cur.until + rules.busyGraceMs) {
    return { granted: false, next: null, reason: "held-busy" };
  }
  return grant(cur.busy && !ask.holderAlive ? "holder-gone" : "taken-over");
}

/** 输入：当前租约、要释放的标签页 → 输出：释放后应存的租约（不是持有者则原样返回）。 */
export function releaseLease(cur: Lease | null, tabId: number): Lease | null {
  if (cur && cur.tabId === tabId) return null;
  return cur;
}
