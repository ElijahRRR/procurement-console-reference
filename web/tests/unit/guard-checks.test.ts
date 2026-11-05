import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { GUARD_CHECK_KEYS, type GuardVerdict } from '@/api/types'
import { guardCheckDetail, normalizeGuardChecks } from '@/utils/guard'
import { GUARD_CHECK_LABEL } from '@/utils/labels'

/** 与 services/guard.evaluate 的输出同形：checks 以检查名为键 */
const blocked: GuardVerdict = {
  verdict: 'block',
  code: 'DATA_UNRELIABLE',
  reasons: ['结算数据不可靠：结算总额读取不到或格式不对'],
  checks: {
    item: { ok: true, expected: { asin: 'B0DEMO0001', quantity: 1 }, actual: { asin: 'B0DEMO0001', quantity: 1 } },
    zip: { ok: false, expected: '32960', actual: '32961' },
    subtotal: { ok: true, expected: { min: '20.00', max: '50.00' }, actual: '41.97' },
    price_cap: { ok: null, expected: '78.69', actual: null },
    delivery: { ok: true, expected: 10, actual: 4 },
    data: { ok: false, expected: 'USD；小计、总额可解析；配送天数可得', actual: ['结算总额读取不到或格式不对'] },
    card: { ok: true, expected: null, actual: '7860' },
  },
  delivery_days: 4,
  total_for_cap: null,
  fulfillment: 'amazon',
}

describe('护栏逐项结果（services/guard.evaluate 的对象写法）', () => {
  it('按契约 §4 的顺序列出全部检查，配送方式只记录、排在最后', () => {
    const rows = normalizeGuardChecks(blocked)
    expect(rows.map((r) => r.key)).toEqual([...GUARD_CHECK_KEYS, 'fulfillment'])
    expect(rows.map((r) => r.label)).toEqual(['商品', '邮编', '金额区间', '限价', '配送时长', '数据可靠', '卡尾号', '配送方式'])
  })

  it('ok 为 true / false / null 分别显示「通过」「不通过」「无法判定」', () => {
    const by = Object.fromEntries(normalizeGuardChecks(blocked).map((r) => [r.key, r]))
    expect(by.item).toMatchObject({ ok: true, state: 'pass', stateText: '通过' })
    expect(by.zip).toMatchObject({ ok: false, state: 'fail', stateText: '不通过' })
    expect(by.price_cap).toMatchObject({ ok: null, state: 'unknown', stateText: '无法判定' })
    expect(by.fulfillment).toMatchObject({ ok: null, state: 'record', stateText: '只记录', detail: '亚马逊配送（amazon）' })
  })

  it('要求与实际写成中文（NULL 不当 0）', () => {
    const by = Object.fromEntries(normalizeGuardChecks(blocked).map((r) => [r.key, r.detail]))
    expect(by.item).toBe('要求 B0DEMO0001 × 1，实际 B0DEMO0001 × 1')
    expect(by.zip).toBe('要求 32960，实际 32961')
    expect(by.subtotal).toBe('要求 $20.00–$50.00，实际 $41.97')
    expect(by.price_cap).toBe('要求 ≤ $78.69，实际 读取不到')
    expect(by.delivery).toBe('要求 ≤ 10 天，实际 4 天')
    expect(by.data).toBe('结算总额读取不到或格式不对')
    expect(by.card).toBe('账号未设置期望卡尾号，实际 7860')
  })

  it('商品检查：结算页不止一种商品时写明数量', () => {
    expect(
      guardCheckDetail('item', { ok: false, expected: { asin: 'B0DEMO0001', quantity: 1 }, actual: [{ asin: 'B0DEMO0001', quantity: 1 }, { asin: 'B0DEMO0002', quantity: 1 }] }),
    ).toBe('要求 B0DEMO0001 × 1，结算页有 2 种商品（B0DEMO0001 × 1；B0DEMO0002 × 1）')
    expect(guardCheckDetail('item', { ok: false, expected: { asin: 'B0DEMO0001', quantity: 1 }, actual: null })).toBe('要求 B0DEMO0001 × 1，结算页读取不到商品')
  })

  it('没有有效限价 / 配送上限时写明原因', () => {
    expect(guardCheckDetail('price_cap', { ok: false, expected: null, actual: '44.49' })).toBe('没有有效限价，实际 $44.49')
    expect(guardCheckDetail('delivery', { ok: false, expected: null, actual: 4 })).toBe('没有有效配送上限，实际 4 天')
    expect(guardCheckDetail('card', { ok: false, expected: '7860', actual: null })).toBe('要求 7860，实际 读取不到')
    expect(guardCheckDetail('data', { ok: true, expected: 'USD；小计、总额可解析；配送天数可得', actual: [] })).toBe('要求 USD；小计、总额可解析；配送天数可得')
  })

  it('未知的检查原样列出、不丢', () => {
    const rows = normalizeGuardChecks({ verdict: 'pass', code: null, reasons: [], checks: { new_check: { ok: true, expected: 'x', actual: 'x' } } })
    expect(rows).toEqual([{ key: 'new_check', label: 'new_check', ok: true, state: 'pass', stateText: '通过', detail: '要求 x，实际 x' }])
  })

  it('没有裁决或没有 checks 时为空', () => {
    expect(normalizeGuardChecks(null)).toEqual([])
    expect(normalizeGuardChecks({ verdict: 'pass', code: null, reasons: [] })).toEqual([])
  })
})

const guardPath = resolve(process.cwd(), '..', 'backend/services/guard.py')
describe.skipIf(!existsSync(guardPath))('检查名与 services/guard.CHECK_ORDER 一致', () => {
  it('GUARD_CHECK_KEYS 与后端判定顺序相同，每项都有中文名', () => {
    const src = readFileSync(guardPath, 'utf8')
    const block = /CHECK_ORDER[^=]*=\s*\(([\s\S]*?)\n\)/.exec(src)?.[1] ?? ''
    const keys = [...block.matchAll(/\("(\w+)", "(\w+)"\)/g)].map((m) => m[1])
    expect(keys.length).toBeGreaterThan(0)
    expect([...GUARD_CHECK_KEYS]).toEqual(keys)
    for (const k of keys) expect((GUARD_CHECK_LABEL as Record<string, string>)[k]).toBeTruthy()
  })
})
