/**
 * 组件接线（@vue/test-utils + Element Plus，接口与提示框打桩）：
 * - 「需要留意」标签页用服务端筛选 needs_attention=true，正常分页，不在前端过滤；
 * - 文件导入：预览后改字段对应，预览过期、「提交导入」不可点；批次列表可以继续已预览的批次；
 * - 任务详情：飞书来源显示回填目标与「设为回填目标」；「标记已处理」清除需要留意；
 * - 飞书向导：多列回退的字段对应（ASIN → 来源码）显示为有序多选，保存时保留列表。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus, { ElSelect } from 'element-plus'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h, nextTick, type Component, type DefineComponent } from 'vue'
import type { ImportBatch, ImportRow, ImportTemplate, OrderDetail, OrderRow } from '@/api/types'

const ordersApi = vi.hoisted(() => ({
  listOrders: vi.fn(),
  getOrder: vi.fn(),
  bulkReview: vi.fn(),
  setWritebackTarget: vi.fn(),
  clearAttention: vi.fn(),
}))
vi.mock('@/api/orders', () => ordersApi)

const importsApi = vi.hoisted(() => ({
  listImports: vi.fn(),
  listTemplates: vi.fn(),
  getImport: vi.fn(),
  listImportRows: vi.fn(),
  commitImport: vi.fn(),
  discardImport: vi.fn(),
  previewImport: vi.fn(),
  uploadImport: vi.fn(),
  createTemplate: vi.fn(),
  deleteTemplate: vi.fn(),
  downloadTemplate: vi.fn(),
  downloadProblems: vi.fn(),
  downloadOrdersExport: vi.fn(),
}))
vi.mock('@/api/imports', () => importsApi)

const feishuApi = vi.hoisted(() => ({
  getSystemFields: vi.fn(),
  resolveLink: vi.fn(),
  getFields: vi.fn(),
  previewFeishu: vi.fn(),
  createConnection: vi.fn(),
  updateConnection: vi.fn(),
}))
vi.mock('@/api/feishu', () => feishuApi)

const feedback = vi.hoisted(() => ({
  confirmAction: vi.fn(async () => true),
  promptText: vi.fn(async () => '已在亚马逊取消对应订单'),
  notifyFailure: vi.fn(),
  notifySuccess: vi.fn(),
  notifySummary: vi.fn(),
  runDownload: vi.fn(async (_a: string, fn: () => Promise<unknown>) => void (await fn())),
  toast: vi.fn(),
}))
vi.mock('@/utils/feedback', () => feedback)

import OrderListCard from '@/components/orders/OrderListCard.vue'
import ImportPanel from '@/components/sources/ImportPanel.vue'
import OrderDetailDrawer from '@/components/orders/OrderDetailDrawer.vue'
import FeishuWizard from '@/components/sources/FeishuWizard.vue'
import type { FeishuConnection } from '@/api/types'

function makeRouter() {
  const Blank = defineComponent({ render: () => h('div') })
  return createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: Blank }] })
}

const mounted: VueWrapper[] = []
async function mountWith(comp: Component, props: Record<string, unknown> = {}) {
  const router = makeRouter()
  await router.push('/')
  await router.isReady()
  const w = mount(comp as DefineComponent, { props, attachTo: document.body, global: { plugins: [ElementPlus, router] } })
  mounted.push(w)
  await flushPromises()
  return w
}

/** 输入：按钮文字 → 输出：页面里（含传送到 body 的抽屉、弹窗）文字完全相同的按钮 */
function buttons(text: string): HTMLButtonElement[] {
  return [...document.body.querySelectorAll('button')].filter((b) => (b.textContent ?? '').trim() === text) as HTMLButtonElement[]
}

beforeEach(() => {
  vi.clearAllMocks()
  feedback.confirmAction.mockResolvedValue(true)
  feedback.promptText.mockResolvedValue('已在亚马逊取消对应订单')
})
afterEach(() => {
  while (mounted.length) mounted.pop()!.unmount()
  document.body.innerHTML = ''
})

function orderRow(id: number, extra: Partial<OrderRow> = {}): OrderRow {
  return {
    id, upstream_order_no: `PO-DEMO-${id}`, asin: 'B0DEMO0001', asin_eff: 'B0DEMO0001', quantity: 1, quantity_eff: 1,
    unit_price: '41.97', subtotal_eff: '41.97', price_cap: '78.69', price_cap_eff: '78.69', has_override: false,
    suggested_purchase_date: '2026-11-05', order_time: null, recipient_name: 'Demo Buyer', state: 'FL', postal_code: '32960',
    status: 'purchased', status_reason: null, missing_fields: [], review_status: 'approved', needs_attention: true,
    attention_note: '上游在采购开始后修改了 ASIN', assigned_account: null, assignment_kind: null, assignment_note: null,
    primary_source_kind: 'file', source_kinds: ['file', 'feishu'], latest_attempt: null, updated_at: '2026-11-05T02:00:00+00:00',
    ...extra,
  }
}

describe('任务列表：需要留意交给服务端筛选', () => {
  it('needs_attention=true 与视图一起发给 GET /orders，total 用服务端的全部条数', async () => {
    ordersApi.listOrders.mockResolvedValue({ total: 57, rows: [orderRow(1), orderRow(2)] })
    const w = await mountWith(OrderListCard, {
      tabs: [{ value: 'needs_attention', label: '需要留意', view: 'all', query: { needs_attention: true } }],
      mode: 'exceptions',
    })
    expect(ordersApi.listOrders).toHaveBeenCalledWith({ needs_attention: true, view: 'all', q: undefined, page: 1, page_size: 20 })
    expect(w.text()).toContain('共 57 笔')
  })
})

describe('文件导入：预览之后改字段对应', () => {
  const batch: ImportBatch = {
    id: 42, filename: 'orders-demo.xlsx', sheet_name: '采购订单', status: 'previewed',
    stats: { total: 2, new: 2, update: 0, unchanged: 0, conflict: 0, historical: 0, invalid: 0, pending_review: 0, needs_info: 0 },
    mapping: { 平台订单号: 'upstream_order_no', 采购单价: 'unit_price', 限价: 'price_cap' },
    created_at: '2026-11-05T02:00:00+00:00', committed_at: null, uploaded_by_name: 'DEMO 所有者',
  }
  const row: ImportRow = { row_no: 2, verdict: 'new', issues: [], reason: null, status: 'queued', upstream_order_no: 'PO-DEMO-1', normalized: {}, raw: {} }

  beforeEach(() => {
    importsApi.listImports.mockResolvedValue({ total: 1, rows: [batch] })
    importsApi.listTemplates.mockResolvedValue({ total: 0, rows: [] })
    importsApi.getImport.mockResolvedValue(batch)
    importsApi.listImportRows.mockResolvedValue({ total: 1, rows: [row] })
    feishuApi.getSystemFields.mockResolvedValue({
      source_fields: [
        { key: 'upstream_order_no', label: '平台订单号', required: true },
        { key: 'unit_price', label: '采购单价', required: false },
        { key: 'price_cap', label: '限价', required: false },
      ],
      result_fields: [],
    })
  })

  it('批次列表里「继续」恢复预览；改对应后预览过期、不能提交，改回来又能提交', async () => {
    const w = await mountWith(ImportPanel)
    buttons('继续')[0].click()
    await flushPromises()
    expect(importsApi.getImport).toHaveBeenCalledWith(42)
    expect(w.text()).toContain('预览 · 共 2 行')
    const commitBtn = () => buttons('提交导入')[0]
    expect(commitBtn().disabled).toBe(false)

    // 限价改成「不导入」
    const selects = w.findAllComponents(ElSelect)
    const capSelect = selects.find((s) => s.props('modelValue') === 'price_cap')!
    capSelect.vm.$emit('update:modelValue', null)
    await nextTick()
    expect(w.text()).toContain('字段对应在预览之后改过')
    expect(commitBtn().disabled).toBe(true)

    // 改回原样：签名一致，预览重新有效
    capSelect.vm.$emit('update:modelValue', 'price_cap')
    await nextTick()
    expect(w.text()).not.toContain('字段对应在预览之后改过')
    expect(commitBtn().disabled).toBe(false)
  })

  it('批次列表里「放弃」调用放弃接口', async () => {
    importsApi.discardImport.mockResolvedValue({ ...batch, status: 'discarded' })
    await mountWith(ImportPanel)
    buttons('放弃')[0].click()
    await flushPromises()
    expect(importsApi.discardImport).toHaveBeenCalledWith(42)
  })
})

describe('文件导入：模板工作区共用，同名保存先确认覆盖', () => {
  const batch: ImportBatch = {
    id: 44, filename: 'orders-demo-tpl.xlsx', sheet_name: '采购订单', status: 'uploaded', stats: null,
    mapping: { 平台订单号: 'upstream_order_no', 限价: 'price_cap' },
    sheets: [{ name: '采购订单', row_count: 3, headers: ['平台订单号', '限价'] }],
    headers: ['平台订单号', '限价'],
    created_at: '2026-11-05T02:00:00+00:00', committed_at: null, uploaded_by_name: 'DEMO 所有者',
  }
  const shared: ImportTemplate = {
    id: 5, name: '小蜜蜂导出', mapping: { 平台订单号: 'upstream_order_no' }, created_at: '2026-11-02T02:00:00+00:00', created_by_name: 'DEMO 运营',
  }

  beforeEach(() => {
    importsApi.listImports.mockResolvedValue({ total: 1, rows: [batch] })
    importsApi.listTemplates.mockResolvedValue({ total: 1, rows: [shared] })
    importsApi.getImport.mockResolvedValue(batch)
    importsApi.createTemplate.mockImplementation(async (name: string, mapping: Record<string, string | null>) => ({ ...shared, name, mapping }))
    feishuApi.getSystemFields.mockResolvedValue({
      source_fields: [
        { key: 'upstream_order_no', label: '平台订单号', required: true },
        { key: 'price_cap', label: '限价', required: false },
      ],
      result_fields: [],
    })
  })

  async function openWizard() {
    const w = await mountWith(ImportPanel)
    buttons('继续')[0].click()
    await flushPromises()
    return w
  }

  it('模板选择框不叫「个人模板」，下拉里显示创建人', async () => {
    const w = await openWizard()
    const tplSelect = w.findAllComponents(ElSelect).find((s) => s.props('clearable') && s.props('modelValue') === null && s.props('placeholder')?.includes('模板'))!
    expect(tplSelect.props('placeholder')).toBe('套用工作区模板')
    expect(tplSelect.findAllComponents({ name: 'ElOption' }).map((o) => o.text())).toEqual([expect.stringContaining('DEMO 运营')])
  })

  it('名称与已有模板相同：先确认覆盖（写明创建人），取消就不保存', async () => {
    feedback.promptText.mockResolvedValue('小蜜蜂导出')
    feedback.confirmAction.mockResolvedValue(false)
    await openWizard()
    buttons('存为模板')[0].click()
    await flushPromises()
    expect(feedback.confirmAction).toHaveBeenCalledTimes(1)
    const msg = (feedback.confirmAction.mock.calls[0] as unknown as [{ message: string; confirmText?: string }])[0]
    expect(msg.message).toContain('确定要覆盖模板「小蜜蜂导出」吗？')
    expect(msg.message).toContain('DEMO 运营')
    expect(msg.confirmText).toBe('覆盖')
    expect(importsApi.createTemplate).not.toHaveBeenCalled()
  })

  it('确认覆盖后才保存，提示「已覆盖模板」', async () => {
    feedback.promptText.mockResolvedValue('小蜜蜂导出')
    await openWizard()
    buttons('存为模板')[0].click()
    await flushPromises()
    expect(feedback.confirmAction).toHaveBeenCalledTimes(1)
    expect(importsApi.createTemplate).toHaveBeenCalledWith('小蜜蜂导出', { 平台订单号: 'upstream_order_no', 限价: 'price_cap' })
    expect(feedback.notifySuccess).toHaveBeenCalledWith('已覆盖模板「小蜜蜂导出」')
  })

  it('别人在面板打开之后才存的同名模板也会提醒（保存前重新读模板列表）', async () => {
    importsApi.listTemplates.mockResolvedValueOnce({ total: 0, rows: [] })
    feedback.promptText.mockResolvedValue('小蜜蜂导出')
    feedback.confirmAction.mockResolvedValue(false)
    await openWizard()
    buttons('存为模板')[0].click()
    await flushPromises()
    expect(feedback.confirmAction).toHaveBeenCalledTimes(1)
    expect(importsApi.createTemplate).not.toHaveBeenCalled()
  })

  it('新名称不弹覆盖确认，直接保存；保存提示说明工作区共用、同名覆盖', async () => {
    feedback.promptText.mockResolvedValue('DEMO 新格式')
    await openWizard()
    buttons('存为模板')[0].click()
    await flushPromises()
    const prompt = (feedback.promptText.mock.calls[0] as unknown as [{ message: string }])[0]
    expect(prompt.message).toContain('工作区')
    expect(prompt.message).toContain('同名')
    expect(feedback.confirmAction).not.toHaveBeenCalled()
    expect(importsApi.createTemplate).toHaveBeenCalledWith('DEMO 新格式', { 平台订单号: 'upstream_order_no', 限价: 'price_cap' })
    expect(feedback.notifySuccess).toHaveBeenCalledWith('已保存模板「DEMO 新格式」')
  })
})

describe('任务详情：回填目标与需要留意', () => {
  function detail(extra: Partial<OrderDetail> = {}): OrderDetail {
    return {
      ...orderRow(7),
      original: { asin: 'B0DEMO0001', quantity: 1, price_cap: '78.69', unit_price: '41.97', ship_days: 4, order_time: null, suggested_purchase_date: '2026-11-05', suggested_date_basis: 'source' },
      override: null,
      address: { recipient_name: 'Demo Buyer', address1: '1 Demo St', address2: null, city: 'Sampletown', state: 'FL', postal_code: '32960', phone: '555-0100', country: 'US', district: null },
      conflict: null,
      sources: [
        { id: 70, kind: 'file', connection_id: null, connection_name: null, record_id: null, batch_id: 3, row_no: 2, filename: 'orders-demo.xlsx', is_writeback_target: false, first_seen_at: '2026-11-05T01:00:00+00:00', last_seen_at: '2026-11-05T01:00:00+00:00', snapshot: {} },
        { id: 71, kind: 'feishu', connection_id: 1, connection_name: '订单中心 DEMO', record_id: 'recDEMO1', batch_id: null, row_no: null, filename: null, is_writeback_target: false, first_seen_at: '2026-11-05T01:00:00+00:00', last_seen_at: '2026-11-05T01:00:00+00:00', snapshot: {} },
      ],
      attempts: [
        {
          id: 9, attempt_no: 1, kind: 'auto', account_id: 3, account_name: 'DEMO-环境-A', device_id: 5, device_name: 'Mac-DEMO-01', run_id: 2,
          simulated: true, status: 'unknown', bj_date: '2026-11-05', stage: null, claimed_at: '2026-11-05T01:00:00+00:00', submitted_at: '2026-11-05T01:01:00+00:00',
          finished_at: '2026-11-05T01:05:00+00:00', guard_verdict: null, amazon_order_no: null, subtotal: null, shipping: null, tax: null, total_before_tax: null, total: null,
          card_last4: null, error_code: null, error_detail: null, resolution: 'not_ordered', resolution_note: '订单页没有这笔', resolved_by_name: 'DEMO 所有者', resolved_at: '2026-11-05T02:00:00+00:00',
        },
      ],
      events: [{ id: 1, entity: 'order', entity_id: 7, order_id: 7, at: '2026-11-05T02:00:00+00:00', actor_kind: 'user', actor_id: 1, actor_name: 'DEMO 所有者', kind: 'order.upstream_changed_after_start', detail: {} }],
      allowed_actions: ['mark_amazon_cancelled', 'clear_attention', 'set_writeback_target'],
      ...extra,
    }
  }

  it('显示尝试的账号、设备与核实人，事件的操作者与中文 kind', async () => {
    ordersApi.getOrder.mockResolvedValue(detail())
    await mountWith(OrderDetailDrawer, { orderId: 7, modelValue: true })
    const text = document.body.textContent ?? ''
    expect(text).toContain('DEMO-环境-A / Mac-DEMO-01')
    expect(text).toContain('DEMO 所有者')
    expect(text).toContain('上游在采购开始后修改')
    expect(text).toContain('用户 DEMO 所有者')
  })

  it('不是回填目标的飞书来源可以「设为回填目标」（确认后带 source_id 调接口）', async () => {
    ordersApi.getOrder.mockResolvedValue(detail())
    ordersApi.setWritebackTarget.mockResolvedValue(detail({
      sources: detail().sources.map((s) => ({ ...s, is_writeback_target: s.kind === 'feishu' })),
      allowed_actions: ['mark_amazon_cancelled', 'clear_attention'],
    }))
    await mountWith(OrderDetailDrawer, { orderId: 7, modelValue: true })
    expect(document.body.textContent).toContain('这笔任务还没有回填目标')
    const btn = buttons('设为回填目标')
    expect(btn).toHaveLength(1)
    btn[0].click()
    await flushPromises()
    expect(feedback.confirmAction).toHaveBeenCalledWith(expect.objectContaining({ message: expect.stringMatching(/^确定要把这条飞书记录设为回填目标吗？/) }))
    expect(ordersApi.setWritebackTarget).toHaveBeenCalledWith(7, 71)
    await nextTick()
    expect(buttons('设为回填目标')).toHaveLength(0)
    expect(document.body.textContent).toContain('回填目标')
  })

  it('没有 set_writeback_target 时不显示该按钮', async () => {
    ordersApi.getOrder.mockResolvedValue(detail({ allowed_actions: ['mark_amazon_cancelled'] }))
    await mountWith(OrderDetailDrawer, { orderId: 7, modelValue: true })
    expect(buttons('设为回填目标')).toHaveLength(0)
    expect(buttons('标记已处理')).toHaveLength(0)
  })

  it('「标记已处理」写明说明后清除需要留意', async () => {
    ordersApi.getOrder.mockResolvedValue(detail())
    ordersApi.clearAttention.mockResolvedValue(detail({ needs_attention: false, attention_note: null, allowed_actions: ['mark_amazon_cancelled', 'set_writeback_target'] }))
    await mountWith(OrderDetailDrawer, { orderId: 7, modelValue: true })
    expect(buttons('标记已处理').length).toBeGreaterThan(0)
    buttons('标记已处理')[0].click()
    await flushPromises()
    expect(feedback.promptText).toHaveBeenCalledWith(expect.objectContaining({ required: true, title: '标记已处理' }))
    expect(ordersApi.clearAttention).toHaveBeenCalledWith(7, '已在亚马逊取消对应订单')
    await nextTick()
    expect(buttons('标记已处理')).toHaveLength(0)
    expect(document.body.textContent).not.toContain('上游在采购开始后修改了 ASIN')
  })
})

describe('飞书向导：多列回退的字段对应', () => {
  const conn = {
    id: 5, name: '订单中心 DEMO', link: 'https://demo.feishu.cn/base/bascnDEMO?table=tblDEMO', app_token: 'bascnDEMO',
    source_table_id: 'tblDEMO', source_view_id: null, result_table_id: 'tblDEMO',
    field_map: { upstream_order_no: '平台订单号', asin: ['ASIN', '来源码'], quantity: ['数量', '数量1'] },
    admission: {}, writeback_map: {}, writeback_enabled: false, writeback_verified_at: null, writeback_state: 'disabled',
    writeback_test_record: null, poll_minutes: 10, enabled: true, last_sync_at: null, last_sync_ok_at: null, last_error: null,
    last_stats: null, created_at: '', updated_at: '',
  } as FeishuConnection

  beforeEach(() => {
    feishuApi.resolveLink.mockResolvedValue({ app_token: 'bascnDEMO', table_id: 'tblDEMO', view_id: null, tables: [{ table_id: 'tblDEMO', name: '订单' }] })
    feishuApi.getSystemFields.mockResolvedValue({
      source_fields: [
        { key: 'upstream_order_no', label: '平台订单号', required: true },
        { key: 'asin', label: 'ASIN', required: false },
        { key: 'quantity', label: '数量', required: false },
        { key: 'address1', label: '收货地址', required: false },
      ],
      result_fields: [],
    })
    const f = (name: string) => ({ field_id: name, field_name: name, type: 1, ui_type: 'Text', is_primary: false, writable: true })
    feishuApi.getFields.mockResolvedValue({
      fields: ['平台订单号', 'ASIN', '来源码', '数量', '数量1', '收货人地址', '地址1'].map(f),
      suggested_field_map: { upstream_order_no: '平台订单号', asin: ['ASIN', '来源码'], address1: ['收货人地址', '地址1'] },
      suggested_writeback_map: {},
    })
    feishuApi.previewFeishu.mockResolvedValue({ stats: { read: 0 }, records: [] })
    feishuApi.updateConnection.mockImplementation(async (_id: number, body: object) => ({ ...conn, ...body }))
  })

  it('编辑连接时列表对应显示为多选（不是「不对应」），保存与预览都保留顺序', async () => {
    const w = await mountWith(FeishuWizard, { connection: conn, modelValue: false })
    await w.setProps({ modelValue: true } as Record<string, unknown>)
    await flushPromises()
    buttons('下一步')[0].click()
    await flushPromises()
    buttons('下一步')[0].click()
    await flushPromises()
    const selects = w.findAllComponents(ElSelect).filter((s) => s.props('multiple'))
    const values = selects.map((s) => s.props('modelValue'))
    expect(values).toContainEqual(['ASIN', '来源码'])
    expect(values).toContainEqual(['数量', '数量1'])
    // 建议里的多列回退也原样补上
    expect(values).toContainEqual(['收货人地址', '地址1'])
    buttons('下一步')[0].click()
    await flushPromises()
    buttons('预览样本')[0].click()
    await flushPromises()
    expect(feishuApi.previewFeishu).toHaveBeenCalledWith(expect.objectContaining({
      field_map: { upstream_order_no: '平台订单号', asin: ['ASIN', '来源码'], quantity: ['数量', '数量1'], address1: ['收货人地址', '地址1'] },
    }))
    buttons('保存连接')[0].click()
    await flushPromises()
    expect(feishuApi.updateConnection).toHaveBeenCalledWith(5, expect.objectContaining({
      field_map: { upstream_order_no: '平台订单号', asin: ['ASIN', '来源码'], quantity: ['数量', '数量1'], address1: ['收货人地址', '地址1'] },
    }))
  })
})
