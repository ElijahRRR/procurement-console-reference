/** 事件流水（契约 §8 GET /events）。 */
import { asPage, get } from './client'
import type { EventEntity, EventRecord, Page, PageQuery } from './types'

/** 输入：对象种类与 id、任务 id、分页 → 输出：{total, rows}（新的在前，total 为全部匹配条数） */
export const listEvents = async (
  query: PageQuery & { entity?: EventEntity; entity_id?: number; order_id?: number } = {},
): Promise<Page<EventRecord>> => asPage(await get<Page<EventRecord>>('/events', { ...query }))
