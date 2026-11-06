/** 模拟执行（契约 §5「模拟」）。 */
import { post } from './client'
import type { SimulationResetResult } from './types'

/** 输入：无 → 输出：清除摘要（受影响的尝试、任务、账号数） */
export const resetSimulation = () => post<SimulationResetResult>('/simulation/reset', {})
