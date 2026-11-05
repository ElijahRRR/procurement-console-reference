/** 「开始执行（模拟）」能不能点（纯函数；面板、弹出页共用，Node 里可测）。
 *
 * 本地的「本浏览器正在执行」只是提示，不是权威：执行页面刷新或插件更新之后它可能是旧的。
 * 后台会在页面重新加载、执行页面不再答复时把它清掉；这里再多一道：比它更新的服务端心跳说
 * 「这台设备没有未结束的批次」（active_run 为 null）时，本地记录作废。
 * 反过来，服务端说有批次而本地没有（例如刷新前的批次租约还没到期）时**不**禁用按钮：
 * 点了之后服务端回 ACCOUNT_BUSY，面板说明是本设备上一次的批次、租约到期后自动释放。
 */

import type { StateSnapshot } from "./messages.js";

/** 输入：状态快照 → 输出：本地记录的「本浏览器正在执行」还算不算数。 */
export function runViewActive(s: Pick<StateSnapshot, "run" | "config">): boolean {
  const rv = s.run;
  if (!rv?.active) return false;
  const cfg = s.config;
  if (cfg && cfg.received_at > rv.updated_at && cfg.heartbeat.active_run === null) return false;
  return true;
}

/** 输入：状态快照 → 输出：「开始执行」现在能不能点，不能点时给原因。 */
export function startGate(s: StateSnapshot): { ok: boolean; reason: string | null } {
  if (!s.device.paired) return { ok: false, reason: "尚未配对，请先在设置页完成配对" };
  if (runViewActive(s)) return { ok: false, reason: "本浏览器正在执行" };
  const hb = s.config?.heartbeat;
  if (hb && !hb.account) return { ok: false, reason: "本设备还没有绑定买家账号（在控制台绑定后，这里几秒内自动更新）" };
  return { ok: true, reason: null };
}
