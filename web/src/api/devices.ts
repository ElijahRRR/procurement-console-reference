/** 装了插件的浏览器（契约 §5、§8；接口路径沿用 /devices）。浏览器由插件自动登记，这里只有查看、改名与移除。 */
import { asPage, get, patch, post } from './client'
import type { Device, Page, PageQuery } from './types'

export interface DeviceQuery extends PageQuery {
  /** true 时带上已从列表移除的浏览器 */
  include_hidden?: boolean
}

/** 输入：分页、是否带上已移除的 → 输出：{total, rows} */
export const listDevices = async (query: DeviceQuery = {}): Promise<Page<Device>> => {
  const { include_hidden: hidden, ...paging } = query
  return asPage(await get<Page<Device>>('/devices', { page: 1, page_size: 200, ...paging, include_hidden: hidden ? 1 : 0 }))
}

/** 输入：浏览器 id、新名称 → 输出：更新后的浏览器（只能改名） */
export const renameDevice = (id: number, name: string) => patch<Device>(`/devices/${id}`, { name })

/**
 * 输入：浏览器 id → 输出：服务端的答复（更新后的浏览器或 {ok}，调用方之后重新读列表）。
 * 移除只是把它从列表里清掉；这个浏览器的插件再次联系服务端时自动回到列表。正在执行的浏览器不能移除（409）。
 */
export const removeDevice = (id: number) => post<Device | { ok: boolean } | null>(`/devices/${id}/remove`, {})
