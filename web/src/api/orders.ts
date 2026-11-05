/** 采购任务：列表、详情与全部人工动作（契约 §8）。 */
import { asPage, get, post } from './client'
import type { OrderDetail, OrderQuery, OrderRow, Page, ResolveUnknownAmounts } from './types'

/** 输入：视图、搜索词、账号、来源、分页 → 输出：{total, rows} */
export const listOrders = async (query: OrderQuery = {}): Promise<Page<OrderRow>> =>
  asPage(await get<Page<OrderRow>>('/orders', { ...query }))

/** 输入：任务 id → 输出：任务详情（原值、覆盖值、来源、尝试、事件、allowed_actions） */
export const getOrder = (id: number) => get<OrderDetail>(`/orders/${id}`)

/** 输入：任务 id、通过 / 拒绝、备注 → 输出：更新后的详情 */
export const reviewOrder = (id: number, decision: 'approve' | 'reject', note?: string) =>
  post<OrderDetail>(`/orders/${id}/review`, { decision, note: note ?? null })

/** 输入：任务 id 列表、通过 / 拒绝 → 输出：后端摘要（如 {updated}） */
export const bulkReview = (ids: number[], decision: 'approve' | 'reject') =>
  post<Record<string, unknown>>('/orders/bulk-review', { ids, decision })

/** 输入：任务 id、目标账号 id → 输出：更新后的详情（人工改派） */
export const assignOrder = (id: number, accountId: number) =>
  post<OrderDetail>(`/orders/${id}/assign`, { account_id: accountId })

/** 输入：任务 id → 输出：更新后的详情（解除分配） */
export const unassignOrder = (id: number) => post<OrderDetail>(`/orders/${id}/unassign`, {})

/** 输入：任务 id、覆盖值（null 表示清除）、备注 → 输出：更新后的详情 */
export const overrideOrder = (
  id: number,
  body: { asin?: string | null; quantity?: number | null; price_cap?: string | null; note: string },
) => post<OrderDetail>(`/orders/${id}/override`, body)

/** 输入：任务 id、备注 → 输出：更新后的详情（异常恢复，重新校验） */
export const resumeOrder = (id: number, note?: string) => post<OrderDetail>(`/orders/${id}/resume`, { note: note ?? null })

/** 输入：任务 id、备注 → 输出：更新后的详情（人工取消） */
export const cancelOrder = (id: number, note?: string) => post<OrderDetail>(`/orders/${id}/cancel`, { note: note ?? null })

/** 输入：任务 id → 输出：更新后的详情（撤销取消） */
export const uncancelOrder = (id: number) => post<OrderDetail>(`/orders/${id}/uncancel`, {})

/** 输入：任务 id、核实结论（已下单需亚马逊单号与金额）→ 输出：更新后的详情 */
export const resolveUnknown = (
  id: number,
  body: { outcome: 'ordered' | 'not_ordered'; amazon_order_no?: string | null; amounts?: ResolveUnknownAmounts | null; note?: string | null },
) => post<OrderDetail>(`/orders/${id}/resolve-unknown`, body)

/** 输入：任务 id、备注 → 输出：更新后的详情（登记亚马逊取消 → 待决定重采） */
export const markAmazonCancelled = (id: number, note?: string) =>
  post<OrderDetail>(`/orders/${id}/mark-amazon-cancelled`, { note: note ?? null })

/** 输入：任务 id、同意 / 不重采、备注 → 输出：更新后的详情 */
export const decideRepurchase = (id: number, decision: 'approve' | 'decline', note?: string) =>
  post<OrderDetail>(`/orders/${id}/repurchase`, { decision, note: note ?? null })

/** 输入：任务 id、保留当前 / 接受来源新值、备注 → 输出：更新后的详情 */
export const resolveConflict = (id: number, choice: 'keep' | 'accept', note?: string) =>
  post<OrderDetail>(`/orders/${id}/resolve-conflict`, { choice, note: note ?? null })
