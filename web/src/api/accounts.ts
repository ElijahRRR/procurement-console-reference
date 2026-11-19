/** 买家账号与待登记的买家号（契约 §8；形状见 §10「账号」）。 */
import { asPage, get, patch, post } from './client'
import type { Account, AccountInput, DiscoveredBuyer, Page, PageQuery } from './types'

/** 输入：分页 → 输出：{total, rows}（含当天额度、所在浏览器、当前执行批次） */
export const listAccounts = async (query: PageQuery = { page: 1, page_size: 200 }): Promise<Page<Account>> =>
  asPage(await get<Page<Account>>('/accounts', { ...query }))

/** 输入：账号 id → 输出：账号（含 pause_cause、open_result_unknown；恢复执行前重新读一次） */
export const getAccount = (id: number) => get<Account>(`/accounts/${id}`)

/** 输入：账号配置（可带 amazon_customer_id，即「登记」一个买家号）→ 输出：新账号 */
export const createAccount = (body: AccountInput) => post<Account>('/accounts', body)

/** 输入：账号 id、要改的字段（只带 amazon_customer_id 即「填到已有账号」）→ 输出：更新后的账号 */
export const updateAccount = (id: number, body: AccountInput) => patch<Account>(`/accounts/${id}`, body)

/** 输入：账号 id、原因 → 输出：更新后的账号（暂停执行） */
export const pauseAccount = (id: number, reason: string) => post<Account>(`/accounts/${id}/pause`, { reason })

/** 输入：账号 id → 输出：更新后的账号（恢复执行） */
export const resumeAccount = (id: number) => post<Account>(`/accounts/${id}/resume`, {})

/**
 * 输入：是否带上已忽略的 → 输出：插件读到、还没登记到任何账号的买家号（按最近上报时间倒序）。
 * 接口返回 {items: [...]}；也认裸数组与 {rows: [...]}。
 */
export async function listDiscovered(includeIgnored = false): Promise<DiscoveredBuyer[]> {
  const res = await get<{ items?: DiscoveredBuyer[]; rows?: DiscoveredBuyer[] } | DiscoveredBuyer[] | null>('/accounts/discovered', {
    include_ignored: includeIgnored ? 1 : 0,
  })
  if (Array.isArray(res)) return res
  return res?.items ?? res?.rows ?? []
}

/** 输入：买家号 ID → 输出：{ok}（忽略：不再出现在待登记列表里） */
export const ignoreDiscovered = (customerId: string) =>
  post<{ ok: boolean }>('/accounts/discovered/ignore', { customer_id: customerId })

/** 输入：买家号 ID → 输出：{ok}（恢复：回到待登记列表） */
export const unignoreDiscovered = (customerId: string) =>
  post<{ ok: boolean }>('/accounts/discovered/unignore', { customer_id: customerId })
