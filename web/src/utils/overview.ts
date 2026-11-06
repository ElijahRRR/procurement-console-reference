/**
 * 概览与列表页共用的计数口径（纯函数）：统计卡上的数字必须等于它跳去的页面里各标签页计数之和。
 *
 * - 开始前待人工处理（待采购页）：待确认 + 资料待补齐 + 待核实（conflict）。
 * - 异常待处理（异常处理页）：异常 + 结果待核实 + 待决定重采。
 */
import type { AllocationSummary, OrderStatus, OrderView } from '@/api/types'
import { formatNumber } from './format'
import { ORDER_STATUS_LABEL } from './labels'

type Counts = Partial<Record<OrderStatus, number>>

export const PRE_START_MANUAL: readonly OrderStatus[] = ['pending_review', 'needs_info', 'conflict']
export const EXCEPTION_MANUAL: readonly OrderStatus[] = ['exception', 'result_unknown', 'repurchase_pending']

export interface CountGroup {
  total: number
  /** 「待确认 2 · 资料待补齐 1 · 待核实 0」 */
  hint: string
  /** 跳转时打开的视图：第一个有任务的状态（都为 0 时取第一个） */
  view: OrderView
}

function group(counts: Counts | null | undefined, statuses: readonly OrderStatus[]): CountGroup {
  const c = counts ?? {}
  const total = statuses.reduce((n, s) => n + (c[s] ?? 0), 0)
  const hint = statuses.map((s) => `${ORDER_STATUS_LABEL[s]} ${formatNumber(c[s] ?? 0)}`).join(' · ')
  const view = (statuses.find((s) => (c[s] ?? 0) > 0) ?? statuses[0]) as OrderView
  return { total, hint, view }
}

/** 输入：概览的状态计数 → 输出：开始前需要人工处理的任务（待采购页的待确认、资料待补齐、待核实） */
export function preStartManual(counts: Counts | null | undefined): CountGroup {
  return group(counts, PRE_START_MANUAL)
}

/** 输入：概览的状态计数 → 输出：异常处理页的任务（异常、结果待核实、待决定重采） */
export function exceptionManual(counts: Counts | null | undefined): CountGroup {
  return group(counts, EXCEPTION_MANUAL)
}

/** 输入：分配摘要里未覆盖的一项、摘要的计划日期 → 输出：账号名，不是计划当天时带上日期（「环境-B · 2026-11-12」） */
export function uncoveredTitle(u: AllocationSummary['uncovered'][number], planDate: string): string {
  return u.date && u.date !== planDate ? `${u.name} · ${u.date}` : u.name
}
