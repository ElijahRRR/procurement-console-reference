/** 分配：重新分配（契约 §3.2、§8；最近一次摘要随概览返回）。 */
import { post } from './client'
import type { AllocationSummary } from './types'

/** 输入：无 → 输出：本次分配摘要 */
export const runAllocation = () => post<AllocationSummary>('/allocation/run', {})
