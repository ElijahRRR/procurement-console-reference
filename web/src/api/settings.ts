/** 全局设置（契约 §8、§10「设置」）。 */
import { get, patch } from './client'
import type { Settings, SettingsPatch } from './types'

/** 输入：无 → 输出：全局设置 */
export const getSettings = () => get<Settings>('/settings')

/** 输入：要改的字段（file_admission 给出的项覆盖缺省，null = 恢复缺省）→ 输出：更新后的设置 */
export const updateSettings = (body: SettingsPatch) => patch<Settings>('/settings', body)
