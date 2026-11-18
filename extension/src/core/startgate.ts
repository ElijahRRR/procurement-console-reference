/** 「开始执行（模拟）」能不能点（纯函数；面板用，Node 里可测）。
 *
 * 本地的「本浏览器正在执行」只是提示，不是权威：执行页面刷新或插件更新之后它可能是旧的。
 * 后台会在页面重新加载、执行页面不再答复时把它清掉；这里再多一道：比它更新的服务端心跳说
 * 「这个浏览器没有未结束的批次」（device_run 为 null）时，本地记录作废。
 * 看的是 device_run（这个浏览器自己的批次，不论账号），不是 account.active_run：换登另一个买家号之后
 * account 跟着变，而上一个买家号的批次可能还没结束。
 * 反过来，服务端说有批次而本地没有（例如刷新前的批次租约还没到期）时**不**禁用按钮：
 * 点了之后服务端回 DEVICE_BUSY，面板说明这个浏览器上一个批次还没有结束。
 */

import type { StateSnapshot } from "./messages.js";
import { panelStatus } from "./status.js";

/** 输入：状态快照 → 输出：本地记录的「本浏览器正在执行」还算不算数。 */
export function runViewActive(s: Pick<StateSnapshot, "run" | "config">): boolean {
  const rv = s.run;
  if (!rv?.active) return false;
  const cfg = s.config;
  if (cfg && cfg.received_at > rv.updated_at && cfg.heartbeat.device_run === null) return false;
  return true;
}

/** 输入：状态快照 → 输出：「开始执行」现在能不能点，不能点时给原因。
 *
 *  只有 ready（买家号已登记、账号没暂停）才能点。「今天可执行 0 笔」不禁用：数字可能是一分钟前的，
 *  点了以服务端的答复为准。后台还在替上一个批次收尾（有结果回执没送到）时不能点：这时不发请求。 */
export function startGate(s: StateSnapshot): { ok: boolean; reason: string | null } {
  if (runViewActive(s)) return { ok: false, reason: "本浏览器正在执行" };
  const st = panelStatus(s);
  if (st.kind !== "ready") return { ok: false, reason: st.short };
  if (st.account?.execution_paused) return { ok: false, reason: "账号已暂停执行" };
  if (s.active_run?.orphaned) return { ok: false, reason: "这个浏览器上一个批次还在收尾（有结果回执没送到），送达后自动结束" };
  return { ok: true, reason: null };
}
