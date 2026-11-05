/**
 * 任务详情抽屉的展示规则（纯函数）：状态说明的提示颜色、缺失字段提示是否与状态说明重复。
 * 颜色表达含义（设计系统）：成功为绿、进行中为蓝灰、需要人处理为黄、出错与待核实为红。
 */
import type { OrderRow, OrderStatus } from '@/api/types'
import { fieldLabel } from './labels'

export type AlertType = 'success' | 'info' | 'warning' | 'error'

export const STATUS_ALERT_TYPE: Record<OrderStatus, AlertType> = {
  purchased: 'success',
  queued: 'info',
  assigned: 'info',
  executing: 'info',
  cancelled: 'info',
  closed: 'info',
  pending_review: 'warning',
  needs_info: 'warning',
  repurchase_pending: 'warning',
  conflict: 'error',
  exception: 'error',
  result_unknown: 'error',
}

/** 输入：任务状态 → 输出：状态说明（status_reason）用的 el-alert 类型 */
export function statusAlertType(status: OrderStatus | string): AlertType {
  return (STATUS_ALERT_TYPE as Record<string, AlertType>)[status] ?? 'info'
}

/** 输入：任务 → 输出：缺失字段的中文提示「缺少执行所需字段：采购单价、限价」，没有缺失为空串 */
export function missingFieldsText(o: Pick<OrderRow, 'missing_fields'>): string {
  return o.missing_fields?.length ? `缺少执行所需字段：${o.missing_fields.map(fieldLabel).join('、')}` : ''
}

/**
 * 输入：任务 → 输出：是否单独显示状态说明。资料待补齐时状态说明就是「缺少：…」，与结构化的缺失字段提示重复，
 * 这时只显示后者；状态说明里还有别的内容（如来源变化）时照常显示。
 */
export function showStatusReason(o: Pick<OrderRow, 'status' | 'status_reason' | 'missing_fields'>): boolean {
  const reason = (o.status_reason ?? '').trim()
  if (!reason) return false
  if (!o.missing_fields?.length) return true
  const labels = o.missing_fields.map(fieldLabel)
  // 去掉「缺少」前缀与全部缺失字段名、分隔符后，没有剩下别的内容 → 重复
  let rest = reason.replace(/^缺少(执行所需字段)?[:：]?/, '')
  for (const l of labels) rest = rest.split(l).join('')
  return rest.replace(/[、，,；;。\s]/g, '') !== ''
}
