import { describe, expect, it } from 'vitest'
import { normalizeGuardChecks } from '@/utils/guard'

describe('护栏逐项结果的统一展示', () => {
  it('数组写法', () => {
    const rows = normalizeGuardChecks({
      verdict: 'block',
      code: 'PRICE_OVER_CAP',
      reasons: ['结算总额 $52.00 超过限价 $50.00'],
      checks: [
        { name: 'item', ok: true },
        { name: 'price_cap', ok: false, detail: '$52.00 > $50.00' },
        { name: 'fulfillment', detail: 'amazon' },
      ],
    })
    expect(rows).toEqual([
      { key: 'item', label: '商品', ok: true, detail: '' },
      { key: 'price_cap', label: '限价', ok: false, detail: '$52.00 > $50.00' },
      { key: 'fulfillment', label: '配送方式', ok: null, detail: 'amazon' },
    ])
  })
  it('对象写法与布尔值', () => {
    const rows = normalizeGuardChecks({
      verdict: 'pass',
      code: null,
      reasons: [],
      checks: { zip: true, DELIVERY_TOO_LONG: { passed: true, expected: '≤ 10 天', actual: '4 天' } },
    })
    expect(rows).toEqual([
      { key: 'zip', label: '邮编', ok: true, detail: '' },
      { key: 'DELIVERY_TOO_LONG', label: '预计送达超过配送上限', ok: true, detail: '要求 ≤ 10 天，实际 4 天' },
    ])
  })
  it('没有裁决或没有 checks 时为空', () => {
    expect(normalizeGuardChecks(null)).toEqual([])
    expect(normalizeGuardChecks({ verdict: 'pass', code: null, reasons: [] })).toEqual([])
  })
})
