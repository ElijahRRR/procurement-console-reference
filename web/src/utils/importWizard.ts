/**
 * 文件导入向导的纯函数：字段对应签名（判断预览是否过期）、套用模板、同名模板覆盖确认、从批次恢复向导、重复上传提醒。
 * 导入模板是工作区共用的（v2 §4、契约 §7）：同名保存会覆盖别人存的对应关系，所以保存前要先确认。
 */
import type { ImportBatch, ImportSheet, ImportTemplate, PreviousUpload } from '@/api/types'
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

/**
 * 输入：工作区模板列表、要保存的名称 → 输出：同名的已有模板（名称去掉首尾空白后完全一致，与服务端唯一键相同），没有为 undefined
 */
export function findTemplateByName<T extends Pick<ImportTemplate, 'name'>>(templates: readonly T[], name: string): T | undefined {
  const key = name.trim()
  return key ? templates.find((t) => t.name === key) : undefined
}

/** 输入：将被覆盖的模板 → 输出：覆盖确认文案（写明创建人；改动对整个工作区生效） */
export function templateOverwriteMessage(t: Pick<ImportTemplate, 'name' | 'created_by_name'>): string {
  const by = t.created_by_name ? `它由 ${t.created_by_name} 创建，` : ''
  return `确定要覆盖模板「${t.name}」吗？${by}工作区里的人以后套用它都会用新的字段对应。`
}

/** 输入：批次 → 输出：能否在控制台继续（已上传或已预览、还没提交或放弃） */
export function canResume(b: Pick<ImportBatch, 'status'>): boolean {
  return b.status === 'uploaded' || b.status === 'previewed'
}

export interface ResumeState {
  sheet: string
  headers: string[]
  mapping: ImportMapping
  /** 原文件的全部工作表（服务端读不到原文件时只有批次所选的那一张） */
  sheets: ImportSheet[]
}

/** 输入：任意值 → 输出：字符串列表（不是列表为空列表，非字符串项去掉） */
function strings(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
}

/**
 * 输入：GET /imports/{id} 的批次 → 输出：恢复向导用的工作表、表头、对应与工作表列表；凑不出工作表与表头时为 null。
 *
 * 表头优先用服务端从原文件读出的 headers（原文件顺序）；读不到原文件（headers 为空）时退回 mapping 的键
 * （jsonb 不保证顺序）。mapping 以该工作表的全部表头为键（上传时是建议对应，预览后是预览用的对应）。
 * 工作表列表用 sheets，空时只给批次所选的那一张（行数取批次统计）。
 */
export function resumeFromBatch(
  b: Pick<ImportBatch, 'sheet_name' | 'mapping'> & Partial<Pick<ImportBatch, 'sheets' | 'headers' | 'stats'>>,
): ResumeState | null {
  const m = b.mapping && typeof b.mapping === 'object' ? b.mapping : {}
  const sheets = (Array.isArray(b.sheets) ? b.sheets : [])
    .filter((s) => s && typeof s.name === 'string')
    .map((s) => ({ name: s.name, row_count: typeof s.row_count === 'number' ? s.row_count : null, headers: strings(s.headers) }))
  const sheet = b.sheet_name || sheets.find((s) => s.headers.length)?.name || ''
  if (!sheet) return null
  const fromFile = strings(b.headers).length ? strings(b.headers) : (sheets.find((s) => s.name === sheet)?.headers ?? [])
  const headers = fromFile.length ? fromFile : Object.keys(m)
  if (!headers.length) return null
  const mapping: ImportMapping = {}
  for (const h of headers) mapping[h] = typeof m[h] === 'string' && m[h] ? m[h] : null
  const listed = sheets.some((s) => s.name === sheet)
  const all = listed
    ? sheets.map((s) => (s.name === sheet && !s.headers.length ? { ...s, headers } : s))
    : [{ name: sheet, row_count: b.stats?.total ?? null, headers }, ...sheets]
  return { sheet, headers, mapping, sheets: all }
}

/** 输入：上次同一文件的上传 → 输出：提醒文案 */
export function previousUploadText(p: PreviousUpload): string {
  return (
    `这个文件在 ${formatDateTime(p.created_at)} 上传过（批次 ${p.id}，${labelOf(BATCH_STATUS_LABEL, p.status)}）。` +
    '再次导入时已有的任务按平台订单号去重，预览里会显示为「无变化」或「更新」。'
  )
}
