/**
 * 飞书字段对应 {系统字段: 列名 | [候选列名]} 与向导里编辑用的 {系统字段: [列名…]} 互转（纯函数）。
 *
 * 服务端给多个候选列时按顺序回退：前一列为空才读下一列（如 ASIN 缺失时用「来源码」，services/feishu_fields）。
 * 向导用有序多选编辑，保存时一列写成字符串、多列写成列表、空列表去掉。
 */
import type { FeishuFieldMap, SystemField } from '@/api/types'

/** 输入：一项对应的值（字符串、列表或空）→ 输出：去空白、去重后的列名列表 */
export function toColumns(v: unknown): string[] {
  const raw = typeof v === 'string' ? [v] : Array.isArray(v) ? v : []
  const out: string[] = []
  for (const x of raw) {
    if (typeof x !== 'string') continue
    const s = x.trim()
    if (s && !out.includes(s)) out.push(s)
  }
  return out
}

/** 输入：列名列表 → 输出：一列为字符串，多列为列表，没有为 undefined */
export function fromColumns(cols: readonly string[]): string | string[] | undefined {
  const c = toColumns([...cols])
  if (!c.length) return undefined
  return c.length === 1 ? c[0] : c
}

/** 输入：服务端的字段对应 → 输出：编辑用的 {系统字段: [列名…]} */
export function toEditableMap(m: FeishuFieldMap | null | undefined): Record<string, string[]> {
  const out: Record<string, string[]> = {}
  for (const [k, v] of Object.entries(m ?? {})) {
    const cols = toColumns(v)
    if (cols.length) out[k] = cols
  }
  return out
}

/** 输入：编辑用的对应 → 输出：提交给服务端的字段对应（空项去掉，一列写成字符串） */
export function toFieldMap(m: Record<string, readonly string[] | undefined>): FeishuFieldMap {
  const out: FeishuFieldMap = {}
  for (const [k, cols] of Object.entries(m)) {
    const v = fromColumns(cols ?? [])
    if (v !== undefined) out[k] = v
  }
  return out
}

/** 输入：编辑中的对应、服务端建议的对应 → 输出：无（只给还没有对应的系统字段补上建议，已选的不动） */
export function fillSuggested(target: Record<string, string[]>, suggested: FeishuFieldMap | null | undefined): void {
  for (const [k, v] of Object.entries(suggested ?? {})) {
    if (toColumns(target[k]).length) continue
    const cols = toColumns(v)
    if (cols.length) target[k] = cols
  }
}

/** 输入：系统字段清单、编辑中的对应 → 输出：还没对应任何列的必填字段的中文名（空列表算没对应） */
export function missingRequired(fields: readonly SystemField[], m: Record<string, readonly string[] | undefined>): string[] {
  return fields.filter((f) => f.required && !toColumns(m[f.key]).length).map((f) => f.label)
}
