/** 「开始执行」那一步的几张对照表（纯函数；内容脚本里的执行宿主与后台的定时触发共用，Node 里可测）。
 *
 * 开始执行（规格 §3）：当前页重读身份 → 交后台对账、上报 → 申请批次。每一步不过，都落成一个 ScheduledReply：
 * 面板据此提示所有者，后台的定时触发据此决定「今天这个时间点算不算处理过」。
 */

import { codeLabel } from "./codes.js";
import type { StartNotice } from "./engine.js";
import type { IdentitySnapshot } from "./identity.js";
import type { PrepareReply, ScheduledReply } from "./messages.js";

/** 登录状态对不上的那几个码：过一会儿（刷新页面、关掉旧标签页、打开订单页）自然会变，定时触发时不算处理过。 */
export const IDENTITY_CODES: ReadonlySet<string> = new Set(["IDENTITY_MISMATCH", "IDENTITY_CONFLICT", "SIGNED_OUT", "NO_CUSTOMER_ID", "NO_IDENTITY"]);

/** 输入：这一页刚读到的身份（登录状态读不出来为 null）、定时所属的买家号 ID（手动为 null）
 *  → 输出：这一页为什么开始不了；可以开始为 null。
 *
 *  这一页自己必须读到买家号 ID：不拿别的标签页读到的 ID 在这一页上领任务
 *  （换登之后在一张读不出 ID 的页面上点开始，不能用旧买家号开批次）。 */
export function pageStartProblem(page: IdentitySnapshot | null | undefined, scheduledFor: string | null = null): { code: string; why: string } | null {
  if (!page) return { code: "NO_IDENTITY", why: "这一页没有读到亚马逊登录状态（页面上没有导航栏），请换一个亚马逊页面（例如订单页）再试" };
  if (!page.signed_in) return { code: "SIGNED_OUT", why: "浏览器没有登录亚马逊" };
  if (!page.customer_id) return { code: "NO_CUSTOMER_ID", why: "这一页没有读到买家号 ID。打开订单页后插件会自动读取" };
  if (scheduledFor !== null && page.customer_id !== scheduledFor) return { code: "IDENTITY_MISMATCH", why: "这一页登录的不是定时所属的买家号" };
  return null;
}

/** 输入：这一页开始不了的原因、是不是定时触发 → 输出：答复。定时触发时答 next_tab：请后台换下一个标签页。 */
export function replyForPage(problem: { code: string; why: string }, scheduled: boolean): ScheduledReply {
  return { result: scheduled ? "next_tab" : "identity", code: problem.code, message: `开始执行失败：${problem.why}` };
}

/** 输入：后台对 pc.prepareStart 的答复（后台没有响应为 null）、这一页读到的买家号 ID
 *  → 输出：不能开始时的答复；可以去申请批次了为 null。 */
export function replyFromPrepare(prep: PrepareReply | null | undefined, customerId: string): ScheduledReply | null {
  if (!prep) return { result: "not_ready", code: "SW_UNAVAILABLE", message: `开始执行失败：${codeLabel("SW_UNAVAILABLE")}` };
  if (prep.ok && prep.customer_id === customerId) return null;
  if (prep.ok) {
    // 后台对账出的买家号与这一页读到的不是同一个：不拿对不上的 ID 去领任务。
    return { result: "identity", code: "IDENTITY_MISMATCH", message: "开始执行失败：页面上的买家号与插件对账的结论不一致，请刷新亚马逊页面后重试" };
  }
  const code = prep.code ?? "IDENTITY_CONFLICT";
  const result: ScheduledReply["result"] = code === "DEVICE_BUSY" ? "busy" : IDENTITY_CODES.has(code) ? "identity" : "not_ready";
  return { result, code, message: prep.message ?? `开始执行失败：${codeLabel(code)}` };
}

/** 输入：引擎开始批次那一步的结论 → 输出：答复。 */
export function replyFromNotice(n: StartNotice): ScheduledReply {
  const code = n.error?.code ?? null;
  if (n.started) return { result: "started", code: null, message: n.message };
  if (n.status === "busy") return { result: "busy", code, message: n.message };
  if (n.error?.kind === "network") return { result: "network", code, message: n.message };
  if (code !== null && IDENTITY_CODES.has(code)) return { result: "identity", code, message: n.message };
  return { result: "rejected", code, message: n.message };
}

/** 定时触发拿到答复之后怎么办：
 *    done      今天这个时间点记为处理过；
 *    refresh   先刷新配置（心跳），再记为处理过 —— 服务端说参数不对（422），多半是账号的执行方式或时间点刚改过；
 *    later     不记，下一分钟再问；
 *    next_tab  这一页开始不了，换下一个标签页。 */
export type ScheduleVerdict = "done" | "refresh" | "later" | "next_tab";

/** 输入：内容脚本对定时触发的答复 → 输出：怎么办（规格 §3）。
 *  已开始 → 记；SLOT_DONE（服务端说这个时间点今天已有正常结束的批次）→ 记；422 → 刷新配置后记；账号已暂停 → 记
 *  （今天这次不执行）；ACCOUNT_BUSY（含同一个时间点的批次还在别处执行：它可能没跑完就结束，时间点会重新空出来）/
 *  DEVICE_BUSY、没说上话、登录状态对不上、别的拒绝 → 不记：去重的权威在服务端，本地记录只用来少发请求，
 *  拿不准就再问一次。 */
export function scheduleVerdict(r: Pick<ScheduledReply, "result" | "code">): ScheduleVerdict {
  if (r.result === "next_tab") return "next_tab";
  if (r.result === "started") return "done";
  if (r.result === "rejected") {
    if (r.code === "SLOT_DONE" || r.code === "ACCOUNT_PAUSED") return "done";
    if (r.code === "VALIDATION") return "refresh";
  }
  return "later";
}
