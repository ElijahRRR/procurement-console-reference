/** 插件设备与配对码（契约 §5、§8）。 */
import { asPage, get, patch, post } from './client'
import type { Device, Page, PageQuery, PairingCode } from './types'

/** 输入：分页 → 输出：{total, rows} */
export const listDevices = async (query: PageQuery = { page: 1, page_size: 200 }): Promise<Page<Device>> =>
  asPage(await get<Page<Device>>('/devices', { ...query }))

/** 输入：无 → 输出：8 位配对码与过期时间（10 分钟有效、一次性） */
export const createPairingCode = () => post<PairingCode>('/devices/pairing-codes', {})

/** 输入：设备 id、新名称 / 绑定账号 → 输出：更新后的设备 */
export const updateDevice = (id: number, body: { name?: string; bound_account_id?: number | null }) =>
  patch<Device>(`/devices/${id}`, body)

/** 输入：设备 id → 输出：更新后的设备（撤销授权，令牌失效） */
export const revokeDevice = (id: number) => post<Device>(`/devices/${id}/revoke`, {})
