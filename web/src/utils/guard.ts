/**
 * 护栏裁决展示：契约 §4 只规定了 {verdict, code, reasons, checks}，没有规定 checks 的内部形状，
 * 这里把常见的数组 / 对象两种写法统一成一列 {key, label, ok, detail}。
 */
import type { GuardCheck, GuardVerdict } from '@/api/types'
import { GUARD_CHECK_LABEL } from './labels'

export interface GuardCheckRow {
  key: string
  label: string
  /** true 通过；false 拦截；null 只记录 */
  ok: boolean | null
  detail: string
}

function okOf(c: GuardCheck | boolean): boolean | null {
  if (typeof c === 'boolean') return c
  for (const k of ['ok', 'passed', 'pass'] as const) {
    if (typeof c[k] === 'boolean') return c[k] as boolean
  }
  return null
}

function detailOf(c: GuardCheck | boolean): string {
  if (typeof c === 'boolean') return ''
  const text = c.detail ?? c.reason ?? c.message ?? ''
  if (text) return String(text)
  const parts: string[] = []
  if (c.expected !== undefined && c.expected !== null) parts.push(`要求 ${String(c.expected)}`)
  if (c.actual !== undefined && c.actual !== null) parts.push(`实际 ${String(c.actual)}`)
  return parts.join('，')
}

/** 输入：尝试的 guard_verdict → 输出：逐项检查行；没有裁决时为空数组 */
export function normalizeGuardChecks(verdict: GuardVerdict | null | undefined): GuardCheckRow[] {
  if (!verdict || !verdict.checks) return []
  const rows: GuardCheckRow[] = []
  if (Array.isArray(verdict.checks)) {
    verdict.checks.forEach((c, i) => {
      const key = c.key ?? c.name ?? c.check ?? c.code ?? `check_${i + 1}`
      rows.push({ key, label: GUARD_CHECK_LABEL[key] ?? key, ok: okOf(c), detail: detailOf(c) })
    })
  } else {
    for (const [key, c] of Object.entries(verdict.checks)) {
      rows.push({ key, label: GUARD_CHECK_LABEL[key] ?? key, ok: okOf(c), detail: detailOf(c) })
    }
  }
  return rows
}
