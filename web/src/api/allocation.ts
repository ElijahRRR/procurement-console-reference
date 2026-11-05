/** 分配：重新分配与最近一次摘要（契约 §3.2、§8）。 */
import { get, post } from './client'
import type { AllocationSummary } from './types'

/** 输入：无 → 输出：本次分配摘要 */
export const runAllocation = () => post<AllocationSummary>('/allocation/run', {})

/** 输入：无 → 输出：最近一次分配摘要，没有时为 null */
export const getLastAllocation = () => get<AllocationSummary | null>('/allocation/last')
