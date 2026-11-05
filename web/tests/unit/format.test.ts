import { describe, expect, it } from 'vitest'
import {
  blankToNull,
  cnSpacing,
  daysBetween,
  displayValue,
  EMPTY,
  failureText,
  formatAmountRange,
  formatCount,
  formatDate,
  formatDateTime,
  formatMoney,
  formatNumber,
  formatRelative,
  formatTime,
  greeting,
  hasAtMostTwoDecimals,
  normalizeMoneyInput,
  parseAmount,
} from '@/utils/format'
import { ApiError } from '@/api/client'

describe('金额', () => {
  it('两位小数与千分位，美元符号在前', () => {
    expect(formatMoney('1234.5')).toBe('$1,234.50')
    expect(formatMoney('52.00')).toBe('$52.00')
    expect(formatMoney(41.97)).toBe('$41.97')
    expect(formatMoney('1234567.891')).toBe('$1,234,567.89')
    expect(formatMoney('0')).toBe('$0.00')
  })
  it('负数符号在美元符号之前', () => {
    expect(formatMoney('-12')).toBe('-$12.00')
  })
  it('空值与解析不了的值显示「—」，不当作 0', () => {
    expect(formatMoney(null)).toBe(EMPTY)
    expect(formatMoney(undefined)).toBe(EMPTY)
    expect(formatMoney('')).toBe(EMPTY)
    expect(formatMoney('abc')).toBe(EMPTY)
    expect(formatMoney(Number.NaN)).toBe(EMPTY)
  })
  it('能解析带 $ 与千分位的输入', () => {
    expect(parseAmount('$1,234.50')).toBe(1234.5)
    expect(parseAmount(' 52 ')).toBe(52)
    expect(parseAmount('US$3.10')).toBe(3.1)
    expect(parseAmount('1.2.3')).toBeNull()
  })
  it('表单金额规范成两位小数字符串，空值为 null', () => {
    expect(normalizeMoneyInput('12.5')).toBe('12.50')
    expect(normalizeMoneyInput('$1,000')).toBe('1000.00')
    expect(normalizeMoneyInput('')).toBeNull()
    expect(normalizeMoneyInput('x')).toBeNull()
  })
  it('金额区间：上限为空表示不限', () => {
    expect(formatAmountRange('20.00', '50.00')).toBe('$20.00–$50.00')
    expect(formatAmountRange('20.00', null)).toBe('$20.00 起')
    expect(formatAmountRange(null, null)).toBe('不限')
  })
})

describe('数字', () => {
  it('千分位与单位', () => {
    expect(formatNumber(1284)).toBe('1,284')
    expect(formatNumber(0)).toBe('0')
    expect(formatNumber(2.456)).toBe('2.46')
    expect(formatNumber(null)).toBe(EMPTY)
    expect(formatCount(1284, '笔')).toBe('1,284 笔')
    expect(formatCount(null, '笔')).toBe(EMPTY)
  })
})

describe('日期与时刻（北京时间）', () => {
  it('日期字符串原样显示', () => {
    expect(formatDate('2026-10-31')).toBe('2026-10-31')
  })
  it('UTC 时刻换算成北京日期', () => {
    // 2026-10-31T17:00Z = 北京 11 月 1 日 01:00
    expect(formatDate('2026-10-31T17:00:00+00:00')).toBe('2026-11-01')
    expect(formatDateTime('2026-10-31T17:00:00+00:00')).toBe('2026-11-01 01:00')
    expect(formatDateTime('2026-10-27T13:31:05+00:00', true)).toBe('2026-10-27 21:31:05')
    expect(formatTime('2026-10-27T13:31:00Z')).toBe('21:31')
  })
  it('空值与非法时刻显示「—」', () => {
    expect(formatDate(null)).toBe(EMPTY)
    expect(formatDateTime('not a date')).toBe(EMPTY)
  })
  it('相对时间', () => {
    const now = new Date('2026-11-05T10:00:00Z')
    expect(formatRelative('2026-11-05T09:59:50Z', now)).toBe('刚刚')
    expect(formatRelative('2026-11-05T09:55:00Z', now)).toBe('5 分钟前')
    expect(formatRelative('2026-11-05T07:00:00Z', now)).toBe('3 小时前')
    expect(formatRelative('2026-11-03T10:00:00Z', now)).toBe('2 天前')
    expect(formatRelative('2026-11-05T10:10:00Z', now)).toBe('10 分钟后')
    expect(formatRelative('2026-10-01T10:00:00Z', now)).toBe('2026-10-01')
  })
  it('日期差', () => {
    expect(daysBetween('2026-10-23', '2026-10-25')).toBe(2)
    expect(daysBetween(null, '2026-10-25')).toBeNull()
  })
})

describe('中文排版', () => {
  it('中文与英文、数字之间补半角空格', () => {
    expect(cnSpacing('共3笔API订单')).toBe('共 3 笔 API 订单')
    expect(cnSpacing('Walmart美国站')).toBe('Walmart 美国站')
    expect(cnSpacing('增长12%')).toBe('增长 12%')
    expect(cnSpacing('金额$12.50')).toBe('金额 $12.50')
  })
  it('已有空格或中文标点两侧不重复补', () => {
    expect(cnSpacing('共 3 笔')).toBe('共 3 笔')
    expect(cnSpacing('失败：429')).toBe('失败：429')
    expect(cnSpacing('（ASIN）')).toBe('（ASIN）')
    expect(cnSpacing('')).toBe('')
    expect(cnSpacing(null)).toBe('')
  })
  it('失败提示为「动作失败：原因」', () => {
    expect(failureText('同步', new ApiError('FEISHU_ERROR', '飞书接口返回 429。', 502))).toBe('同步失败：飞书接口返回 429')
    expect(failureText('保存', '')).toBe('保存失败')
    expect(failureText('保存', null)).toBe('保存失败')
  })
  it('其他小工具', () => {
    expect(blankToNull('  ')).toBeNull()
    expect(blankToNull(' a ')).toBe('a')
    expect(displayValue(true)).toBe('是')
    expect(displayValue(['a', 'b'])).toBe('a、b')
    expect(displayValue(null)).toBe(EMPTY)
  })
})

describe('金额输入最多两位小数（限价是上限，不替用户四舍五入）', () => {
  it.each([
    ['40.99', true],
    ['40', true],
    ['40.5', true],
    ['$1,234.50', true],
    [' 78.69 ', true],
    ['', true],
    [null, true],
    [12.5, true],
    ['40.999', false],
    ['40.006', false],
    ['78.685', false],
    ['1.005', false],
    [40.999, false],
    ['abc', false],
  ] as const)('%s → %s', (v, ok) => {
    expect(hasAtMostTwoDecimals(v)).toBe(ok)
  })
  it('前端规整只补零，不再把三位小数进上去（三位小数先被校验挡住）', () => {
    expect(normalizeMoneyInput('40.9')).toBe('40.90')
    expect(hasAtMostTwoDecimals('40.999')).toBe(false)
  })
})

describe('问候语按北京时间', () => {
  it.each([
    ['2026-11-04T23:30:00Z', '早上好'], // 北京 07:30
    ['2026-11-05T03:30:00Z', '中午好'], // 北京 11:30
    ['2026-11-05T07:00:00Z', '下午好'], // 北京 15:00
    ['2026-11-05T12:00:00Z', '晚上好'], // 北京 20:00
    ['2026-11-04T19:00:00Z', '晚上好'], // 北京 03:00
  ])('%s → %s', (iso, want) => {
    expect(greeting(new Date(iso))).toBe(want)
  })
})
