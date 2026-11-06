/** 概览（契约 §8 GET /overview，形状见 §10）。 */
import { get } from './client'
import type { Overview } from './types'

/** 输入：无 → 输出：各状态计数、今日可采购、在线设备、最近异常、最近一次分配摘要 */
export const getOverview = () => get<Overview>('/overview')
