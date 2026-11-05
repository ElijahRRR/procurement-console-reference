/** 页面展示规则：概览计数口径、未覆盖账号的日期、恢复执行的确认文案、任务详情的提示颜色与去重。 */
import { describe, expect, it } from 'vitest'
import { ORDER_STATUSES } from '@/api/types'
import { exceptionManual, preStartManual, uncoveredTitle } from '@/utils/overview'
import type { Attempt, EventRecord, OrderConflict } from '@/api/types'
import { isResultUnknownPause, resumeConfirmText, resumeNeedsCaution } from '@/utils/accounts'
import {
  STATUS_ALERT_TYPE,
  amazonOrderNoProblem,
  conflictView,
  eventSummary,
  latestLateResult,
  missingFieldsText,
  pendingUnknownAttempt,
  showStatusReason,
  statusAlertType,
  unknownPrefill,
} from '@/utils/orderDetail'

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
  it('还有待核实的任务时写明数量（open_result_unknown），不论暂停起因', () => {
    const t = resumeConfirmText({ name: 'DEMO-环境-A', pause_reason: '需要人工登录', pause_cause: 'manual', open_result_unknown: 2 })
    expect(t.startsWith('确定要恢复「DEMO-环境-A」的执行吗？')).toBe(true)
    expect(t).toContain('还有 2 笔结果待核实的任务')
    expect(resumeNeedsCaution({ name: 'A', pause_cause: 'manual', open_result_unknown: 2 })).toBe(true)
  })
  it('pause_cause 为 result_unknown：任务都已核实时说明；没给数量时提醒先确认', () => {
    expect(resumeConfirmText({ name: 'A', pause_reason: '结果待核实：PO-DEMO-0006', pause_cause: 'result_unknown', open_result_unknown: 0 })).toContain('都已核实')
    expect(resumeConfirmText({ name: 'A', pause_reason: '人工写的原因', pause_cause: 'result_unknown' })).toContain('暂停原因是结果待核实')
    expect(resumeNeedsCaution({ name: 'A', pause_cause: 'result_unknown', open_result_unknown: 0 })).toBe(true)
  })
  it('账号级错误暂停时提醒先在浏览器里处理；人工暂停只问一句', () => {
    expect(resumeConfirmText({ name: 'A', pause_reason: '买家账号登录失效', pause_cause: 'account_error', open_result_unknown: 0 })).toBe(
      '确定要恢复「A」的执行吗？暂停原因是账号级错误（买家账号登录失效），请先在浏览器里处理好（如重新登录、解除受限）。',
    )
    expect(resumeConfirmText({ name: 'A', pause_reason: '需要人工登录', pause_cause: 'manual', open_result_unknown: 0 })).toBe('确定要恢复「A」的执行吗？')
    expect(resumeNeedsCaution({ name: 'A', pause_cause: 'manual', open_result_unknown: 0 })).toBe(false)
  })
  it('旧服务端没有 pause_cause 时按暂停原因的前缀判断', () => {
    expect(resumeConfirmText({ name: 'A', pause_reason: '结果待核实：PO-DEMO-0006' })).toContain('暂停原因是结果待核实')
    expect(resumeNeedsCaution({ name: 'A', pause_reason: '结果待核实：PO-DEMO-0006' })).toBe(true)
    expect(resumeNeedsCaution({ name: 'A', pause_reason: '需要人工登录' })).toBe(false)
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

function attempt(extra: Partial<Attempt> = {}): Attempt {
  return {
    id: 27, attempt_no: 1, kind: 'auto', account_id: 7, device_id: 7, run_id: 9, simulated: false, status: 'unknown',
    bj_date: '2026-11-05', stage: null, claimed_at: '2026-11-05T01:00:00+00:00', submitted_at: '2026-11-05T01:01:00+00:00',
    finished_at: '2026-11-05T01:05:00+00:00', guard_verdict: null, amazon_order_no: null, subtotal: null, shipping: null,
    tax: null, total_before_tax: null, total: null, card_last4: null, error_code: null, error_detail: null,
    ...extra,
  }
}
function event(kind: string, detail: Record<string, unknown>, extra: Partial<EventRecord> = {}): EventRecord {
  return { id: 1, entity: 'attempt', entity_id: 27, order_id: 7, at: '2026-11-05T02:00:00+00:00', actor_kind: 'device', actor_id: 7, actor_name: 'DEMO 设备', kind, detail, ...extra }
}

describe('事件摘要', () => {
  it('进度被服务端叫停时写出 cancel_code 的中文与原因', () => {
    const e = event('attempt.progress', { stage: '结算页核对', continue: false, cancel_reason: '领取后来源修改了限价，未提交前中止', cancel_code: 'SOURCE_CHANGED' })
    expect(eventSummary(e)).toBe('阶段 结算页核对 · 服务端叫停：领取后来源有变化 · 领取后来源修改了限价，未提交前中止')
    expect(eventSummary(event('attempt.progress', { stage: '加入购物车', continue: true, cancel_reason: null, cancel_code: null }))).toBe('阶段 加入购物车')
    expect(eventSummary(event('attempt.progress', { stage: 'x', continue: false, cancel_code: 'NEW_CODE' }))).toBe('阶段 x · 服务端叫停：NEW_CODE')
  })
  it('错误码、护栏拦截码显示中文；重复的文字只出现一次', () => {
    expect(eventSummary(event('attempt.aborted', { error_code: 'ABORTED_BY_SERVER', reason: '账号已暂停执行' }))).toBe(
      '服务端要求中止（账号暂停或归档、身份不符或过期、来源有变化），未提交前中止 · 账号已暂停执行',
    )
    expect(eventSummary(event('attempt.guard_blocked', { code: 'PRICE_OVER_CAP', reasons: ['结算总额 $52.00 超过限价 $50.00'] }))).toBe('结算总额超过限价 · 结算总额 $52.00 超过限价 $50.00')
    expect(eventSummary(event('attempt.late_result', { amazon_order_no: 'SIM-20261105-27', reason: '插件迟到上报的结果：成功，亚马逊订单号 SIM-20261105-27，请据此核实' }))).toBe(
      '插件迟到上报的结果：成功，亚马逊订单号 SIM-20261105-27，请据此核实',
    )
  })
  it('状态变化与没有文字时的细节', () => {
    expect(eventSummary(event('order.status_changed', { from: 'executing', to: 'exception', reason: '结算总额超过限价' }))).toBe('采购中 → 异常 · 结算总额超过限价')
    expect(eventSummary(event('order.override_set', { quantity: 2 }))).toBe('数量：2')
    // 对象不原样打印 JSON
    expect(eventSummary(event('order.something_new', { extra: { a: 1 }, count: 3 }))).toBe('count：3')
  })
  it('来源、预览结论、状态与字段变化写成中文', () => {
    expect(eventSummary(event('order.created', { source: { kind: 'file', batch_id: 1, row_no: 6 }, verdict: 'new', status: 'pending_review' }))).toBe(
      '文件批次 1 第 6 行 · 新单 · 状态：待确认',
    )
    expect(eventSummary(event('order.source_updated', { source: { kind: 'feishu', connection_id: 1, record_id: 'recDEMO1' }, changes: { quantity: [1, 2], price_cap: ['35.00', '30.00'] }, overrides_kept: [] }))).toBe(
      '飞书记录 recDEMO1 · 数量 1 → 2；限价 $35.00 → $30.00',
    )
    expect(eventSummary(event('order.attention', { source: { kind: 'file', batch_id: 2, row_no: 3 }, notes: ['上游在采购开始后修改了 ASIN'] }))).toBe(
      '文件批次 2 第 3 行 · 上游在采购开始后修改了 ASIN',
    )
  })
})

describe('核实结果对话框', () => {
  const late = event('attempt.late_result', { reason: '插件迟到上报的结果：成功，亚马逊订单号 111-2223333-4445555，请据此核实' })
  it('取最新的待核实尝试与它最近一条迟到回执', () => {
    const d = { attempts: [attempt({ id: 30, status: 'failed' }), attempt(), attempt({ id: 20 })], events: [event('attempt.late_result', { reason: '别的尝试' }, { entity_id: 99 }), late] }
    expect(pendingUnknownAttempt(d)?.id).toBe(27)
    expect(latestLateResult(d.events, 27)).toBe(late)
    expect(latestLateResult(d.events, 5)).toBeNull()
    expect(pendingUnknownAttempt({ attempts: [] })).toBeNull()
  })
  it('迟到回执补进了单号与金额：预填并给出回执说明；空着的金额保持空（不当作 0）', () => {
    const p = unknownPrefill({ attempts: [attempt({ amazon_order_no: '111-2223333-4445555', total: '50.37', subtotal: '47.50' })], events: [late] })
    expect(p).toEqual({
      amazon_order_no: '111-2223333-4445555', subtotal: '47.50', shipping: '', tax: '', total_before_tax: '', total: '50.37',
      simulated: false, prefilled: true, hint: '插件迟到上报的结果：成功，亚马逊订单号 111-2223333-4445555，请据此核实',
    })
  })
  it('没有迟到回执事件但尝试带着单号时给通用提示；什么都没有时全部留空', () => {
    expect(unknownPrefill({ attempts: [attempt({ amazon_order_no: '111-2223333-4445555' })], events: [] }).hint).toContain('已带着亚马逊订单号 111-2223333-4445555')
    const empty = unknownPrefill({ attempts: [attempt()], events: [] })
    expect(empty.amazon_order_no).toBe('')
    expect(empty.total).toBe('')
    expect(empty.prefilled).toBe(false)
    expect(empty.hint).toBe('')
    expect(unknownPrefill(null).simulated).toBe(false)
  })
  it('单号校验：真实尝试只认 123-1234567-1234567，模拟尝试只认 SIM- 开头', () => {
    expect(amazonOrderNoProblem('111-2223333-4445555', false)).toBe('')
    expect(amazonOrderNoProblem(' 111-2223333-4445555 ', false)).toBe('')
    expect(amazonOrderNoProblem('', false)).toBe('请填写亚马逊订单号')
    expect(amazonOrderNoProblem('111-222', false)).toBe('格式应为 123-1234567-1234567')
    expect(amazonOrderNoProblem('SIM-20261105-27', false)).toBe('真实尝试不能登记模拟单号')
    expect(amazonOrderNoProblem('SIM-20261105-27', true)).toBe('')
    expect(amazonOrderNoProblem('111-2223333-4445555', true)).toContain('SIM- 开头')
  })
})

describe('冲突按种类给出处理按钮', () => {
  const base: OrderConflict = { kind: 'fields', source_kind: 'file', reason: '文件来源的数量与飞书记录不同', fields: { quantity: [1, 2] }, incoming: { quantity: 2 }, at: null }
  it('fields：保留当前 / 接受来源新值；说明用服务端原因', () => {
    const v = conflictView(base, 'conflict')
    expect([v.keep.label, v.accept?.label]).toEqual(['保留当前', '接受来源新值'])
    expect(v.description).toBe('文件来源的数量与飞书记录不同。任务暂停在「待核实」，处理前不会分配或执行。')
    expect(conflictView({ ...base, kind: undefined }).accept?.label).toBe('接受来源新值')
  })
  it('review：保留通过 / 接受拒绝（回待确认）', () => {
    const v = conflictView({ ...base, kind: 'review', reason: null })
    expect([v.keep.label, v.accept?.label]).toEqual(['保留通过', '接受拒绝（回待确认）'])
    expect(v.description).toContain('文件来源送来的人审结论是拒绝')
  })
  it('admission：不再采购（取消任务）；source_key_changed：按新订单号接收（取消原任务）', () => {
    expect(conflictView({ ...base, kind: 'admission' }).accept?.label).toBe('不再采购（取消任务）')
    const k = conflictView({ ...base, kind: 'source_key_changed', incoming: { upstream_order_no: 'PO-DEMO-NEW' } })
    expect(k.accept?.label).toBe('按新订单号接收（取消原任务）')
    expect(k.accept?.message).toContain('PO-DEMO-NEW')
  })
  it('external_order_no：单号合格可登记为已采购；格式不对只能保留', () => {
    const okNo = conflictView({ ...base, kind: 'external_order_no', incoming: { external_amz_order_no: '111-2223333-4445555' } })
    expect(okNo.accept?.label).toBe('登记为已采购')
    expect(okNo.acceptBlocked).toBe('')
    const bad = conflictView({ ...base, kind: 'external_order_no', incoming: { external_amz_order_no: '114-DEMO' } })
    expect(bad.accept).toBeNull()
    expect(bad.acceptBlocked).toContain('只能保留当前')
  })
  it('执行中、异常的任务上的冲突说明不同', () => {
    expect(conflictView(base, 'executing').description).toContain('插件下一次上报进度时会在提交前中止')
    expect(conflictView(base, 'exception').description).toContain('任务保持异常')
  })
  it('确认文案都是「确定要……吗？」', () => {
    for (const kind of ['fields', 'review', 'external_order_no', 'admission', 'source_key_changed']) {
      const v = conflictView({ ...base, kind, incoming: { external_amz_order_no: '111-2223333-4445555', upstream_order_no: 'PO-DEMO-NEW' } })
      for (const c of [v.keep, v.accept]) expect(c?.message).toMatch(/^确定要.*吗？/)
    }
  })
})
