/**
 * 账号相关的展示规则（纯函数）。
 *
 * 执行模块因「已登记提交后没有确认结果」暂停账号时，暂停原因写成「结果待核实：<平台订单号>」（services/execution）。
 */

/** 暂停原因的前缀：结果待核实（铁律 9：已登记提交的非成功结局一律暂停账号，人工核实后才恢复） */
export const RESULT_UNKNOWN_PAUSE_PREFIX = '结果待核实'

/** 输入：账号的暂停原因 → 输出：是否因结果待核实而暂停 */
export function isResultUnknownPause(reason: string | null | undefined): boolean {
  return !!reason && reason.trim().startsWith(RESULT_UNKNOWN_PAUSE_PREFIX)
}

/**
 * 输入：账号名、暂停原因、该账号仍为「结果待核实」的任务数（读不到为 null）→ 输出：恢复执行的确认文案（「确定要……吗？」）。
 * 有待核实的任务时写明数量，提醒先在亚马逊订单页核实并登记结果。
 */
export function resumeConfirmText(name: string, reason: string | null | undefined, openUnknown: number | null): string {
  const head = `确定要恢复「${name}」的执行吗？`
  if (openUnknown !== null && openUnknown > 0) {
    return `${head}该账号还有 ${openUnknown} 笔结果待核实的任务：请先在亚马逊订单页核实并登记结果。恢复执行不会让这些任务被再次领取，但未核实前可能重复下单的风险仍在。`
  }
  if (isResultUnknownPause(reason)) {
    return openUnknown === null
      ? `${head}暂停原因是结果待核实，请先确认该账号的结果待核实任务都已核实。`
      : `${head}暂停时的结果待核实任务都已核实。`
  }
  return head
}
