/** 页面展示规则：概览计数口径、未覆盖账号的日期、恢复执行的确认文案、任务详情的提示颜色与去重。 */
import { describe, expect, it } from 'vitest'
import { ORDER_STATUSES } from '@/api/types'
import { exceptionManual, preStartManual, uncoveredTitle } from '@/utils/overview'
import { isResultUnknownPause, resumeConfirmText } from '@/utils/accounts'
import { STATUS_ALERT_TYPE, missingFieldsText, showStatusReason, statusAlertType } from '@/utils/orderDetail'

describe('概览计数与跳转页面一致', () => {
  const counts = { pending_review: 2, needs_info: 1, conflict: 3, exception: 2, result_unknown: 1, repurchase_pending: 0, queued: 9 }
  it('开始前待人工处理 = 待确认 + 资料待补齐 + 待核实（待采购页三个标签页）', () => {
    expect(preStartManual(counts)).toEqual({ total: 6, hint: '待确认 2 · 资料待补齐 1 · 待核实 3', view: 'pending_review' })
    expect(preStartManual({ conflict: 1 }).view).toBe('conflict')
    expect(preStartManual(null)).toEqual({ total: 0, hint: '待确认 0 · 资料待补齐 0 · 待核实 0', view: 'pending_review' })
  })
  it('异常待处理 = 异常 + 结果待核实 + 待决定重采（异常处理页的计数）', () => {
    expect(exceptionManual(counts)).toEqual({ total: 3, hint: '异常 2 · 结果待核实 1 · 待决定重采 0', view: 'exception' })
    expect(exceptionManual({ result_unknown: 4 }).view).toBe('result_unknown')
  })
  it('未覆盖账号不是计划当天时带上日期', () => {
    expect(uncoveredTitle({ account_id: 2, name: 'DEMO-环境-B', date: '2026-11-12', reason: '没有金额匹配的订单' }, '2026-11-05')).toBe('DEMO-环境-B · 2026-11-12')
    expect(uncoveredTitle({ account_id: 2, name: 'DEMO-环境-B', date: '2026-11-05', reason: '' }, '2026-11-05')).toBe('DEMO-环境-B')
  })
})

describe('恢复执行的确认文案', () => {
  it('执行模块写的暂停原因是「结果待核实：<平台订单号>」', () => {
    expect(isResultUnknownPause('结果待核实：PO-DEMO-0006')).toBe(true)
    expect(isResultUnknownPause('结果待核实')).toBe(true)
    expect(isResultUnknownPause('需要人工登录')).toBe(false)
    expect(isResultUnknownPause(null)).toBe(false)
  })
  it('还有待核实的任务时写明数量', () => {
    const t = resumeConfirmText('DEMO-环境-A', '结果待核实：PO-DEMO-0006', 2)
    expect(t.startsWith('确定要恢复「DEMO-环境-A」的执行吗？')).toBe(true)
    expect(t).toContain('还有 2 笔结果待核实的任务')
  })
  it('读不到数量时按暂停原因提醒；已全部核实时说明', () => {
    expect(resumeConfirmText('A', '结果待核实：PO-DEMO-0006', null)).toContain('暂停原因是结果待核实')
    expect(resumeConfirmText('A', '结果待核实：PO-DEMO-0006', 0)).toContain('都已核实')
    expect(resumeConfirmText('A', '需要人工登录', 0)).toBe('确定要恢复「A」的执行吗？')
  })
})

describe('任务详情的状态说明', () => {
  it('每个状态都有提示颜色：已采购为成功，不再一律是警告', () => {
    expect(Object.keys(STATUS_ALERT_TYPE).sort()).toEqual([...ORDER_STATUSES].sort())
    expect(statusAlertType('purchased')).toBe('success')
    expect(statusAlertType('assigned')).toBe('info')
    expect(statusAlertType('needs_info')).toBe('warning')
    expect(statusAlertType('exception')).toBe('error')
    expect(statusAlertType('result_unknown')).toBe('error')
    expect(statusAlertType('conflict')).toBe('error')
    expect(statusAlertType('unknown_status')).toBe('info')
  })
  it('资料待补齐：状态说明只是「缺少：…」时不再重复显示', () => {
    const o = { status: 'needs_info' as const, status_reason: '缺少：采购单价、限价', missing_fields: ['unit_price', 'price_cap'] }
    expect(showStatusReason(o)).toBe(false)
    expect(missingFieldsText(o)).toBe('缺少执行所需字段：采购单价、限价')
  })
  it('状态说明有别的内容时照常显示', () => {
    expect(showStatusReason({ status: 'needs_info', status_reason: '缺少：限价；飞书来源修改了收货人', missing_fields: ['price_cap'] })).toBe(true)
    expect(showStatusReason({ status: 'purchased', status_reason: '模拟采购成功：SIM-20261105-2', missing_fields: [] })).toBe(true)
    expect(showStatusReason({ status: 'queued', status_reason: null, missing_fields: [] })).toBe(false)
  })
})
