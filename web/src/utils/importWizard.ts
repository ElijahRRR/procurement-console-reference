/**
 * 文件导入向导的纯函数：字段对应签名（判断预览是否过期）、套用模板、从批次恢复向导、重复上传提醒。
 */
import type { ImportBatch, ImportTemplate, PreviousUpload } from '@/api/types'
import { formatDateTime } from './format'
import { BATCH_STATUS_LABEL, labelOf } from './labels'

export type ImportMapping = Record<string, string | null>

/** 输入：工作表名、{表头: 系统字段 | null} → 输出：与键顺序无关的签名（空值与未对应视为相同） */
export function mappingSignature(sheet: string, mapping: ImportMapping): string {
  const pairs = Object.keys(mapping)
    .sort()
    .map((h) => [h, mapping[h] || null] as const)
  return JSON.stringify([sheet, pairs])
}

/** 输入：当前表头、当前对应、模板 → 输出：套用后的对应（模板里有的表头用模板的，其余保持不变） */
export function applyTemplate(headers: readonly string[], current: ImportMapping, t: Pick<ImportTemplate, 'mapping'>): ImportMapping {
  const out: ImportMapping = {}
  for (const h of headers) out[h] = h in (t.mapping ?? {}) ? (t.mapping[h] ?? null) : (current[h] ?? null)
  return out
}

/** 输入：批次 → 输出：能否在控制台继续（已上传或已预览、还没提交或放弃） */
export function canResume(b: Pick<ImportBatch, 'status'>): boolean {
  return b.status === 'uploaded' || b.status === 'previewed'
}

export interface ResumeState {
  sheet: string
  headers: string[]
  mapping: ImportMapping
}

/**
 * 输入：GET /imports/{id} 的批次 → 输出：恢复向导用的工作表、表头与对应；批次没有记下对应时为 null。
 * 批次的 mapping 以该工作表的全部表头为键（上传时是建议对应，预览后是预览用的对应）。
 */
export function resumeFromBatch(b: Pick<ImportBatch, 'sheet_name' | 'mapping'>): ResumeState | null {
  const m = b.mapping
  if (!m || typeof m !== 'object' || !b.sheet_name) return null
  const headers = Object.keys(m)
  if (!headers.length) return null
  const mapping: ImportMapping = {}
  for (const h of headers) mapping[h] = typeof m[h] === 'string' && m[h] ? m[h] : null
  return { sheet: b.sheet_name, headers, mapping }
}

/** 输入：上次同一文件的上传 → 输出：提醒文案 */
export function previousUploadText(p: PreviousUpload): string {
  return (
    `这个文件在 ${formatDateTime(p.created_at)} 上传过（批次 ${p.id}，${labelOf(BATCH_STATUS_LABEL, p.status)}）。` +
    '再次导入时已有的任务按平台订单号去重，预览里会显示为「无变化」或「更新」。'
  )
}
