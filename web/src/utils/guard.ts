/**
 * 护栏裁决展示：把尝试的 guard_verdict（services/guard.evaluate 的输出）整理成逐项检查行。
 *
 * checks 是以检查名为键的对象 {item, zip, subtotal, price_cap, delivery, data, card}，每项 {ok, expected, actual}：
 * ok 为 true 通过、false 不通过、null 缺数据无法判定（由 data 检查统一拦截）。配送方式 fulfillment 在裁决顶层，只记录不拦截。
 */
import { GUARD_CHECK_KEYS, type GuardCheck, type GuardVerdict } from '@/api/types'
import { EMPTY, formatAmountRange, formatMoney, isBlank } from './format'
import { GUARD_CHECK_LABEL } from './labels'

export type GuardCheckState = 'pass' | 'fail' | 'unknown' | 'record'

export interface GuardCheckRow {
  key: string
  label: string
  /** true 通过；false 不通过；null 无法判定或只记录 */
  ok: boolean | null
  state: GuardCheckState
  /** 「通过」「不通过」「无法判定」「只记录」 */
  stateText: string
  /** 要求与实际的中文说明 */
  detail: string
}

export const GUARD_STATE_TEXT: Record<GuardCheckState, string> = {
  pass: '通过',
  fail: '不通过',
  unknown: '无法判定',
  record: '只记录',
}

function rec(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

function text(v: unknown): string {
  if (isBlank(v)) return EMPTY
  if (Array.isArray(v)) return v.length ? v.map(text).join('、') : EMPTY
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

function itemText(v: unknown): string {
  const r = rec(v)
  if (!r) return text(v)
  return `${text(r.asin)} × ${text(r.quantity)}`
}

/** 输入：检查名、{ok, expected, actual} → 输出：「要求 …，实际 …」 */
export function guardCheckDetail(key: string, c: GuardCheck): string {
  const { expected: exp, actual: act } = c
  switch (key) {
    case 'item': {
      const want = `要求 ${itemText(exp)}`
      if (act === null || act === undefined) return `${want}，结算页读取不到商品`
      if (Array.isArray(act)) {
        if (act.length === 1) return `${want}，实际 ${itemText(act[0])}`
        return `${want}，结算页有 ${act.length} 种商品${act.length ? `（${act.map(itemText).join('；')}）` : ''}`
      }
      return `${want}，实际 ${itemText(act)}`
    }
    case 'zip':
      return `要求 ${text(exp)}，实际 ${isBlank(act) ? '读取不到' : text(act)}`
    case 'subtotal': {
      const r = rec(exp)
      const range = r ? formatAmountRange(r.min, r.max) : text(exp)
      return `要求 ${range}，实际 ${isBlank(act) ? '读取不到' : formatMoney(act)}`
    }
    case 'price_cap': {
      const want = isBlank(exp) ? '没有有效限价' : `要求 ≤ ${formatMoney(exp)}`
      return `${want}，实际 ${isBlank(act) ? '读取不到' : formatMoney(act)}`
    }
    case 'delivery': {
      const want = isBlank(exp) ? '没有有效配送上限' : `要求 ≤ ${text(exp)} 天`
      return `${want}，实际 ${isBlank(act) ? '读取不到' : `${text(act)} 天`}`
    }
    case 'data': {
      const problems = Array.isArray(act) ? act.map(text).filter((x) => x !== EMPTY) : isBlank(act) ? [] : [text(act)]
      return problems.length ? problems.join('；') : `要求 ${text(exp)}`
    }
    case 'card':
      if (isBlank(exp)) return `账号未设置期望卡尾号，实际 ${isBlank(act) ? '读取不到' : text(act)}`
      return `要求 ${text(exp)}，实际 ${isBlank(act) ? '读取不到' : text(act)}`
    default: {
      const parts: string[] = []
      if (!isBlank(exp)) parts.push(`要求 ${text(exp)}`)
      if (!isBlank(act)) parts.push(`实际 ${text(act)}`)
      return parts.join('，')
    }
  }
}

function stateOf(ok: unknown): GuardCheckState {
  return ok === true ? 'pass' : ok === false ? 'fail' : 'unknown'
}

/** 输入：尝试的 guard_verdict → 输出：逐项检查行（按契约 §4 的顺序，未知检查排在后面，配送方式最后）；没有裁决为空数组 */
export function normalizeGuardChecks(verdict: GuardVerdict | null | undefined): GuardCheckRow[] {
  if (!verdict) return []
  const checks = (rec(verdict.checks) ?? {}) as Record<string, GuardCheck>
  const known = (GUARD_CHECK_KEYS as readonly string[]).filter((k) => k in checks)
  const others = Object.keys(checks).filter((k) => !(GUARD_CHECK_KEYS as readonly string[]).includes(k) && k !== 'fulfillment')
  const rows: GuardCheckRow[] = []
  for (const key of [...known, ...others]) {
    const c = rec(checks[key]) ? checks[key] : ({ ok: null, expected: null, actual: null } as GuardCheck)
    const ok = c.ok === true || c.ok === false ? c.ok : null
    const state = stateOf(ok)
    rows.push({
      key,
      label: (GUARD_CHECK_LABEL as Record<string, string>)[key] ?? key,
      ok,
      state,
      stateText: GUARD_STATE_TEXT[state],
      detail: guardCheckDetail(key, c),
    })
  }
  const fulfillment = verdict.fulfillment ?? (rec(checks.fulfillment) ? checks.fulfillment.actual : null)
  if (!isBlank(fulfillment)) {
    rows.push({
      key: 'fulfillment',
      label: GUARD_CHECK_LABEL.fulfillment,
      ok: null,
      state: 'record',
      stateText: GUARD_STATE_TEXT.record,
      detail: String(fulfillment) === 'amazon' ? '亚马逊配送（amazon）' : text(fulfillment),
    })
  }
  return rows
}
