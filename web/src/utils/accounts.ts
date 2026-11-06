/**
 * 账号相关的展示规则（纯函数）。
 *
 * 账号输出带 pause_cause（result_unknown / account_error / manual，未暂停为 null）与 open_result_unknown
 * （这个账号当前处在「结果待核实」的任务数，services/accounts）。执行模块因「已登记提交后没有确认结果」暂停账号时，
 * 暂停原因写成「结果待核实：<平台订单号>」（services/execution）；旧服务端没有 pause_cause 时按这个前缀判断。
 */
import type { Account } from '@/api/types'

/** 暂停原因的前缀：结果待核实（铁律 9：已登记提交的非成功结局一律暂停账号，人工核实后才恢复） */
export const RESULT_UNKNOWN_PAUSE_PREFIX = '结果待核实'

/** 输入：账号的暂停原因 → 输出：是否因结果待核实而暂停 */
export function isResultUnknownPause(reason: string | null | undefined): boolean {
  return !!reason && reason.trim().startsWith(RESULT_UNKNOWN_PAUSE_PREFIX)
}

export type ResumeInfo = Pick<Account, 'name'> & Partial<Pick<Account, 'pause_reason' | 'pause_cause' | 'open_result_unknown'>>

/** 输入：账号 → 输出：该账号仍为「结果待核实」的任务数；服务端没给为 null */
function openUnknown(a: ResumeInfo): number | null {
  return typeof a.open_result_unknown === 'number' && Number.isFinite(a.open_result_unknown) ? a.open_result_unknown : null
}

/** 输入：账号 → 输出：是否因结果待核实暂停，或还有结果待核实的任务（恢复确认框要醒目提醒） */
export function resumeNeedsCaution(a: ResumeInfo): boolean {
  const cause = a.pause_cause === undefined ? (isResultUnknownPause(a.pause_reason) ? 'result_unknown' : null) : a.pause_cause
  return cause === 'result_unknown' || (openUnknown(a) ?? 0) > 0
}

/**
 * 输入：账号（名称、暂停原因、pause_cause、open_result_unknown）→ 输出：恢复执行的确认文案（「确定要……吗？」）。
 * 还有结果待核实的任务时写明数量，提醒先在亚马逊订单页核实并登记结果；因结果待核实暂停而任务都已核实时说明；
 * 账号级错误暂停时提醒先在浏览器里处理好。
 */
export function resumeConfirmText(a: ResumeInfo): string {
  const head = `确定要恢复「${a.name}」的执行吗？`
  const open = openUnknown(a)
  if (open !== null && open > 0) {
    return `${head}该账号还有 ${open} 笔结果待核实的任务：请先在亚马逊订单页核实并登记结果。恢复执行不会让这些任务被再次领取，但未核实前可能重复下单的风险仍在。`
  }
  const cause = a.pause_cause === undefined ? (isResultUnknownPause(a.pause_reason) ? 'result_unknown' : null) : a.pause_cause
  if (cause === 'result_unknown') {
    return open === null
      ? `${head}暂停原因是结果待核实，请先确认该账号的结果待核实任务都已核实。`
      : `${head}暂停时的结果待核实任务都已核实。`
  }
  if (cause === 'account_error') {
    const why = (a.pause_reason ?? '').trim()
    return `${head}暂停原因是账号级错误${why ? `（${why}）` : ''}，请先在浏览器里处理好（如重新登录、解除受限）。`
  }
  return head
}
