/** 飞书写回队列（契约 §6、§8）。 */
import { asPage, get, post } from './client'
import type { Page, PageQuery, Writeback, WritebackStatus } from './types'

/** 输入：状态筛选、分页 → 输出：{total, rows} */
export const listWritebacks = async (query: PageQuery & { status?: WritebackStatus | '' } = {}): Promise<Page<Writeback>> =>
  asPage(await get<Page<Writeback>>('/writebacks', { ...query }))

/** 输入：写回 id → 输出：更新后的写回记录（立即重试） */
export const retryWriteback = (id: number) => post<Writeback>(`/writebacks/${id}/retry`, {})
