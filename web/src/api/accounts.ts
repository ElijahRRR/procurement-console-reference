/** 买家账号（契约 §8；形状见 §10「账号」）。 */
import { asPage, get, patch, post } from './client'
import type { Account, AccountInput, Page, PageQuery } from './types'

/** 输入：分页 → 输出：{total, rows}（含当天额度、绑定设备、当前执行批次） */
export const listAccounts = async (query: PageQuery = { page: 1, page_size: 200 }): Promise<Page<Account>> =>
  asPage(await get<Page<Account>>('/accounts', { ...query }))

/** 输入：账号 id → 输出：账号 */
export const getAccount = (id: number) => get<Account>(`/accounts/${id}`)

/** 输入：账号配置 → 输出：新账号 */
export const createAccount = (body: AccountInput) => post<Account>('/accounts', body)

/** 输入：账号 id、要改的字段 → 输出：更新后的账号 */
export const updateAccount = (id: number, body: AccountInput) => patch<Account>(`/accounts/${id}`, body)

/** 输入：账号 id、原因 → 输出：更新后的账号（暂停执行） */
export const pauseAccount = (id: number, reason: string) => post<Account>(`/accounts/${id}/pause`, { reason })

/** 输入：账号 id → 输出：更新后的账号（恢复执行） */
export const resumeAccount = (id: number) => post<Account>(`/accounts/${id}/resume`, {})

/** 输入：账号 id、设备 id → 输出：更新后的账号（采用插件识别的买家号 ID） */
export const adoptCustomerId = (id: number, deviceId: number) =>
  post<Account>(`/accounts/${id}/adopt-customer-id`, { device_id: deviceId })
