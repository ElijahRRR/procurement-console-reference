/** 全局设置（契约 §8、§10「设置」）。 */
import { get, patch } from './client'
import type { Settings } from './types'

/** 输入：无 → 输出：全局设置 */
export const getSettings = () => get<Settings>('/settings')

/** 输入：要改的字段 → 输出：更新后的设置 */
export const updateSettings = (body: Partial<Pick<Settings, 'default_max_delivery_days' | 'logistics_sync_time'>>) =>
  patch<Settings>('/settings', body)
