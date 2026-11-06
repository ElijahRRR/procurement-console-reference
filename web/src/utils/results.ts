/**
 * 把动作接口的返回整理成给人看的提示文案（纯函数，tests/unit/results.test.ts 覆盖）。
 * 形状以服务端为准：services/feishu_intake.run_connection、feishu_connections.verify_writeback、imports.commit、
 * simulation.reset、orders.bulk_review。
 */
import type {
  AllocationSummary,
  BatchStats,
  BulkReviewResult,
  FeishuSyncResult,
  ImportCommitResult,
  OrderRow,
  SimulationResetResult,
  VerifyWritebackResult,
} from '@/api/types'
import { displayValue, formatNumber } from './format'
import { FEISHU_VERDICT_LABEL, SYNC_STAT_LABEL, labelOf } from './labels'

/** 同步统计里展示的键（顺序即展示顺序） */
const SYNC_KEYS = ['read', 'new', 'update', 'unchanged', 'conflict', 'historical', 'skipped', 'invalid'] as const
/** 导入统计里展示的键 */
const IMPORT_KEYS = ['new', 'update', 'unchanged', 'conflict', 'historical', 'invalid'] as const

/** 输入：统计 {键: 数字} → 输出：「读取 12 · 新单 3 · 更新 1」；只列大于 0 的项（read / total 总是列出） */
export function statsText(stats: Record<string, unknown> | null | undefined, keys: readonly string[] = SYNC_KEYS): string {
  if (!stats) return ''
  const always = new Set(['read', 'total'])
  return keys
    .filter((k) => typeof stats[k] === 'number' && ((stats[k] as number) > 0 || always.has(k)))
    .map((k) => `${SYNC_STAT_LABEL[k] ?? k} ${formatNumber(stats[k])}`)
    .join(' · ')
}

/** 输入：分配摘要 → 输出：「随后分配：分出 3 笔，未分出 1 笔」；没有分配为空串 */
export function allocationBrief(a: AllocationSummary | null | undefined): string {
  if (!a?.stats) return ''
  return `随后分配：分出 ${formatNumber(a.stats.assigned)} 笔，未分出 ${formatNumber(a.stats.unassigned)} 笔`
}

export interface Summary {
  title: string
  /** 正文（可为空） */
  message: string
  /** 需要人看一眼的问题（非空时用警告提示） */
  warnings: string[]
}

/** 同步结果里列出的问题记录条数上限 */
export const SYNC_PROBLEM_SHOWN = 5

/** 输入：连接名、立即同步的返回 → 输出：成功提示（有跳过 / 无效 / 待核实的记录时列出样本） */
export function syncSummary(name: string, res: FeishuSyncResult): Summary {
  const parts = [statsText(res as unknown as Record<string, unknown>), allocationBrief(res.allocation)].filter(Boolean)
  const problems = res.problems ?? []
  const warnings = problems.slice(0, SYNC_PROBLEM_SHOWN).map((p) => {
    const who = p.upstream_order_no || `记录 ${p.record_id}`
    return `${who}：${labelOf(FEISHU_VERDICT_LABEL, p.verdict)}${p.reason ? `，${p.reason}` : ''}`
  })
  const more = (res.skipped ?? 0) + (res.invalid ?? 0) + (res.conflict ?? 0) - warnings.length
  if (warnings.length && more > 0) warnings.push(`另有 ${formatNumber(more)} 条，见「读取记录」`)
  return { title: `已同步「${name}」`, message: parts.join('；'), warnings }
}

export interface VerifySummary extends Summary {
  ok: boolean
  /** 测试记录没能恢复原值时的提醒（记录里还留着测试值，要醒目提示）；没有为空串 */
  restoreProblem: string
}

/**
 * 输入：写回验收的返回、测试记录 id → 输出：通过 / 未通过的提示（列出不一致的列；恢复原值失败放在最前面并单独给出）。
 */
export function verifySummary(res: VerifyWritebackResult, recordId?: string | null): VerifySummary {
  const warnings: string[] = []
  const rec = recordId ? `测试记录 ${recordId} ` : '测试记录'
  // restore_error 是服务端的「恢复测试记录原值失败：原因（飞书错误码 X）」，原样附在后面
  const restoreProblem = res.restore_error
    ? `${rec}里还留着测试值，请到飞书里手动改回原值。${res.restore_error.trim().replace(/[。.]$/, '')}`
    : ''
  if (restoreProblem) warnings.push(restoreProblem)
  else if (!res.restored && Object.keys(res.written ?? {}).length && !res.error) {
    warnings.push(`${rec}的原值没有恢复，请到飞书里核对这条记录`)
  }
  if (res.error) warnings.push(res.error)
  for (const m of res.mismatches ?? []) {
    warnings.push(`「${m.column}」写入 ${displayValue(m.expected)}，读回 ${displayValue(m.actual)}`)
  }
  if (res.ok) {
    return {
      ok: true,
      title: restoreProblem ? '写回验收通过，但测试记录的原值没能恢复' : '写回验收通过',
      message: '之后的真实采购结果会写回飞书；模拟结果仍不写回。',
      warnings,
      restoreProblem,
    }
  }
  return {
    ok: false,
    title: '写回验收失败：' + (res.error ? '飞书接口报错' : res.mismatches?.length ? '读回的值与写入的不一致' : '未完成验收'),
    message: '写回仍为「回填待验证」，队列只积压不发送。',
    warnings,
    restoreProblem,
  }
}

/** 输入：提交导入的返回 → 输出：成功提示（实际统计 + 随后的分配） */
export function commitSummary(res: ImportCommitResult): Summary {
  const s = (res.stats ?? {}) as Partial<BatchStats>
  const parts = [statsText(s as Record<string, unknown>, IMPORT_KEYS)]
  const held: string[] = []
  if (s.pending_review) held.push(`待确认 ${formatNumber(s.pending_review)} 笔`)
  if (s.needs_info) held.push(`资料待补齐 ${formatNumber(s.needs_info)} 笔`)
  if (held.length) parts.push(`其中${held.join('、')}不会下发采购`)
  parts.push(allocationBrief(res.allocation))
  return { title: `已提交导入「${res.batch?.filename ?? ''}」`, message: parts.filter(Boolean).join('；'), warnings: [] }
}

/** 输入：清除模拟结果的返回 → 输出：成功提示（没能还原的任务列为警告） */
export function resetSummary(res: SimulationResetResult): Summary {
  const restored = res.orders_restored?.length ?? 0
  const parts = [
    `中止模拟尝试 ${formatNumber(res.attempts_reset ?? 0)} 次`,
    `还原任务 ${formatNumber(restored)} 笔`,
    `结束执行批次 ${formatNumber(res.runs_ended?.length ?? 0)} 个`,
    `恢复账号 ${formatNumber(res.accounts_resumed?.length ?? 0)} 个`,
  ]
  const warnings = (res.orders_skipped ?? []).map((x) => `任务 ${x.order_id} 没有还原：${x.reason}`)
  return { title: '已清除模拟结果', message: parts.join('，'), warnings }
}

/** 输入：批量人审的返回、通过 / 拒绝、选中的行 → 输出：成功提示与跳过的任务（「n 笔未处理：原因」） */
export function bulkReviewSummary(res: BulkReviewResult, decision: 'approve' | 'reject', rows: Pick<OrderRow, 'id' | 'upstream_order_no'>[]): Summary {
  const no = new Map(rows.map((r) => [r.id, r.upstream_order_no]))
  const n = res.updated?.length ?? 0
  const skipped = res.skipped ?? []
  const title = n
    ? decision === 'approve'
      ? `已通过 ${formatNumber(n)} 笔任务`
      : `已拒绝 ${formatNumber(n)} 笔任务`
    : decision === 'approve'
      ? '批量人审通过失败：选中的任务都没有处理'
      : '批量拒绝失败：选中的任务都没有处理'
  const warnings = skipped.map((x) => `${no.get(x.id) ?? `任务 ${x.id}`}：${x.reason}`)
  const message = skipped.length ? `${formatNumber(skipped.length)} 笔未处理` : ''
  return { title, message, warnings }
}
