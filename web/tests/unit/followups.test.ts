/**
 * 组件接线（第二轮）：
 * - 任务详情：核实结果对话框按迟到回执预填单号与金额并显示回执说明；冲突按钮按冲突种类；事件里显示叫停原因；
 * - 账号：恢复执行前重新读账号，按 pause_cause / open_result_unknown 提醒结果待核实，不再另查任务列表；
 * - 异常处理：「需要留意」的数字取概览的 needs_attention；
 * - 登录：429 显示「登录失败：尝试次数过多，请稍后再试」；
 * - 飞书连接：写回验收后没能恢复原值时在连接卡片上醒目提示；
 * - 文件导入：「继续」按原文件的表头顺序与工作表列表恢复。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus, { ElSelect } from 'element-plus'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h, type Component, type DefineComponent } from 'vue'
import type { Account, Attempt, FeishuConnection, ImportBatch, OrderDetail, Overview } from '@/api/types'

const ordersApi = vi.hoisted(() => ({
  listOrders: vi.fn(),
  getOrder: vi.fn(),
  bulkReview: vi.fn(),
  resolveConflict: vi.fn(),
  resolveUnknown: vi.fn(),
}))
vi.mock('@/api/orders', () => ordersApi)

const accountsApi = vi.hoisted(() => ({
  listAccounts: vi.fn(),
  getAccount: vi.fn(),
  resumeAccount: vi.fn(),
  pauseAccount: vi.fn(),
  updateAccount: vi.fn(),
  createAccount: vi.fn(),
  adoptCustomerId: vi.fn(),
}))
vi.mock('@/api/accounts', () => accountsApi)

const devicesApi = vi.hoisted(() => ({ listDevices: vi.fn() }))
vi.mock('@/api/devices', () => devicesApi)

const settingsApi = vi.hoisted(() => ({ getSettings: vi.fn(), updateSettings: vi.fn() }))
vi.mock('@/api/settings', () => settingsApi)

const overviewApi = vi.hoisted(() => ({ getOverview: vi.fn() }))
vi.mock('@/api/overview', () => overviewApi)

const authApi = vi.hoisted(() => ({ login: vi.fn(), getMe: vi.fn(), getAuthState: vi.fn(), logout: vi.fn(), setup: vi.fn() }))
vi.mock('@/api/auth', () => authApi)

const feishuApi = vi.hoisted(() => ({
  getFeishuStatus: vi.fn(),
  listConnections: vi.fn(),
  verifyWriteback: vi.fn(),
  syncConnection: vi.fn(),
  updateConnection: vi.fn(),
  listSyncRuns: vi.fn(),
  getSystemFields: vi.fn(),
  resolveLink: vi.fn(),
  getFields: vi.fn(),
  previewFeishu: vi.fn(),
  createConnection: vi.fn(),
}))
vi.mock('@/api/feishu', () => feishuApi)

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

const feedback = vi.hoisted(() => ({
  confirmAction: vi.fn(async () => true),
  promptText: vi.fn(async () => '核实说明'),
  notifyFailure: vi.fn(),
  notifySuccess: vi.fn(),
  notifySummary: vi.fn(),
  runDownload: vi.fn(),
  toast: vi.fn(),
}))
vi.mock('@/utils/feedback', () => feedback)

import { ApiError } from '@/api/client'
import OrderDetailDrawer from '@/components/orders/OrderDetailDrawer.vue'
import AccountsPanel from '@/components/accounts/AccountsPanel.vue'
import ExceptionsView from '@/views/ExceptionsView.vue'
import LoginView from '@/views/LoginView.vue'
import FeishuPanel from '@/components/sources/FeishuPanel.vue'
import FeishuWizard from '@/components/sources/FeishuWizard.vue'
import ImportPanel from '@/components/sources/ImportPanel.vue'

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
const bodyText = () => document.body.textContent ?? ''

beforeEach(() => {
  vi.clearAllMocks()
  feedback.confirmAction.mockResolvedValue(true)
  feedback.promptText.mockResolvedValue('核实说明')
})
afterEach(() => {
  while (mounted.length) mounted.pop()!.unmount()
  document.body.innerHTML = ''
})

// ── 任务详情 ──────────────────────────────────────────────────────────────────

function attempt(extra: Partial<Attempt> = {}): Attempt {
  return {
    id: 27, attempt_no: 1, kind: 'auto', account_id: 7, account_name: 'DEMO-环境-C', device_id: 7, device_name: 'Win-DEMO-03', run_id: 9,
    simulated: true, status: 'unknown', bj_date: '2026-11-05', stage: null, claimed_at: '2026-11-05T01:00:00+00:00',
    submitted_at: '2026-11-05T01:01:00+00:00', finished_at: '2026-11-05T01:05:00+00:00', guard_verdict: null,
    amazon_order_no: 'SIM-20261105-27', subtotal: '47.50', shipping: '0.00', tax: '2.87', total_before_tax: '47.50', total: '50.37',
    card_last4: '1234', error_code: null, error_detail: '登记提交后 3 分钟未收到结果，执行权超时',
    ...extra,
  }
}

function detail(extra: Partial<OrderDetail> = {}): OrderDetail {
  return {
    id: 7, upstream_order_no: 'PO-DEMO-7', asin: 'B0DEMO0007', asin_eff: 'B0DEMO0007', quantity: 1, quantity_eff: 1,
    unit_price: '47.50', subtotal_eff: '47.50', price_cap: '60.00', price_cap_eff: '60.00', has_override: false,
    suggested_purchase_date: '2026-11-05', order_time: null, recipient_name: 'Demo Buyer', state: 'FL', postal_code: '32960',
    status: 'result_unknown', status_reason: '已登记提交，插件失联后未回报结果', missing_fields: [], review_status: 'approved',
    needs_attention: false, attention_note: null, assigned_account: { id: 7, name: 'DEMO-环境-C' }, assignment_kind: 'auto',
    assignment_note: null, primary_source_kind: 'file', source_kinds: ['file'], latest_attempt: null, updated_at: '2026-11-05T02:00:00+00:00',
    original: { asin: 'B0DEMO0007', quantity: 1, price_cap: '60.00', unit_price: '47.50', ship_days: 4, order_time: null, suggested_purchase_date: '2026-11-05', suggested_date_basis: 'source' },
    override: null,
    address: { recipient_name: 'Demo Buyer', address1: '1 Demo St', address2: null, city: 'Sampletown', state: 'FL', postal_code: '32960', phone: '555-0100', country: 'US', district: null },
    conflict: null,
    sources: [],
    attempts: [attempt()],
    events: [
      {
        id: 3, entity: 'attempt', entity_id: 27, order_id: 7, at: '2026-11-05T02:00:00+00:00', actor_kind: 'device', actor_id: 7, actor_name: 'Win-DEMO-03',
        kind: 'attempt.late_result', detail: { outcome: 'succeeded', amazon_order_no: 'SIM-20261105-27', reason: '插件迟到上报的结果：成功，亚马逊订单号 SIM-20261105-27，请据此核实' },
      },
      {
        id: 2, entity: 'attempt', entity_id: 26, order_id: 7, at: '2026-11-05T00:30:00+00:00', actor_kind: 'device', actor_id: 7, actor_name: 'Win-DEMO-03',
        kind: 'attempt.progress', detail: { stage: '结算页核对', continue: false, cancel_reason: '账号已暂停执行：人工暂停', cancel_code: 'ACCOUNT_PAUSED' },
      },
    ],
    allowed_actions: ['resolve_unknown'],
    ...extra,
  }
}

describe('任务详情：核实结果按迟到的回执预填', () => {
  it('预填单号与金额、显示回执说明；模拟尝试按 SIM- 单号提交', async () => {
    ordersApi.getOrder.mockResolvedValue(detail())
    ordersApi.resolveUnknown.mockResolvedValue({ ...detail(), status: 'purchased', allowed_actions: [] })
    await mountWith(OrderDetailDrawer, { orderId: 7, modelValue: true })
    // 事件里写出叫停原因的中文
    expect(bodyText()).toContain('服务端叫停：账号已暂停执行')
    expect(bodyText()).toContain('插件迟到上报的结果')
    buttons('核实结果')[0].click()
    await flushPromises()
    const dialog = [...document.body.querySelectorAll('.el-dialog')].find((d) => (d.textContent ?? '').includes('核实采购结果'))!
    expect(dialog.textContent).toContain('插件迟到上报的结果：成功，亚马逊订单号 SIM-20261105-27，请据此核实')
    expect(dialog.textContent).toContain('已按插件上报的回执预填')
    const values = [...dialog.querySelectorAll('input')].map((i) => (i as HTMLInputElement).value)
    expect(values).toContain('SIM-20261105-27')
    expect(values).toContain('50.37')
    // 核实依据必填：表单里填上（promptText 是打桩的，这里直接改输入框）
    const note = dialog.querySelector('textarea') as HTMLTextAreaElement
    note.value = '订单页找到了对应订单'
    note.dispatchEvent(new Event('input'))
    await flushPromises()
    buttons('登记已下单')[0].click()
    await flushPromises()
    await new Promise((r) => setTimeout(r, 0))
    await flushPromises()
    expect(ordersApi.resolveUnknown).toHaveBeenCalledWith(7, expect.objectContaining({
      outcome: 'ordered',
      amazon_order_no: 'SIM-20261105-27',
      amounts: expect.objectContaining({ total: '50.37', subtotal: '47.50' }),
      note: '订单页找到了对应订单',
    }))
  })

  it('尝试上没有单号时不预填、不显示回执说明', async () => {
    ordersApi.getOrder.mockResolvedValue(detail({ attempts: [attempt({ amazon_order_no: null, subtotal: null, shipping: null, tax: null, total_before_tax: null, total: null })], events: [] }))
    await mountWith(OrderDetailDrawer, { orderId: 7, modelValue: true })
    buttons('核实结果')[0].click()
    await flushPromises()
    const dialog = [...document.body.querySelectorAll('.el-dialog')].find((d) => (d.textContent ?? '').includes('核实采购结果'))!
    expect(dialog.textContent).not.toContain('已按插件上报的回执预填')
    const values = [...dialog.querySelectorAll('input:not([type=radio])')].map((i) => (i as HTMLInputElement).value).filter(Boolean)
    expect(values).toEqual([])
  })
})

describe('任务详情：冲突按钮按冲突种类', () => {
  const conflictDetail = (conflict: OrderDetail['conflict']) =>
    detail({ status: 'conflict', status_reason: conflict?.reason ?? null, conflict, attempts: [], events: [], allowed_actions: ['resolve_conflict', 'cancel'] })

  it('人审拒绝：保留通过 / 接受拒绝（回待确认），接受时确认框写明回待确认', async () => {
    ordersApi.getOrder.mockResolvedValue(conflictDetail({
      kind: 'review', source_kind: 'file', reason: '文件来源的人工审核为「拒绝」，与任务已有的通过结论不同，需人工核实',
      fields: { review_status: ['approved', 'rejected'] }, incoming: { review_status: 'rejected' }, at: '2026-11-05T02:00:00+00:00',
    }))
    ordersApi.resolveConflict.mockResolvedValue(conflictDetail(null))
    await mountWith(OrderDetailDrawer, { orderId: 7, modelValue: true })
    expect(bodyText()).toContain('来源送来人审拒绝')
    expect(buttons('保留通过')).toHaveLength(1)
    buttons('接受拒绝（回待确认）')[0].click()
    await flushPromises()
    expect(feedback.promptText).toHaveBeenCalledWith(expect.objectContaining({ message: expect.stringContaining('任务回到「待确认」'), danger: true }))
    expect(ordersApi.resolveConflict).toHaveBeenCalledWith(7, 'accept', '核实说明')
  })

  it('外部单号格式不对：没有接受按钮，说明只能保留', async () => {
    ordersApi.getOrder.mockResolvedValue(conflictDetail({
      kind: 'external_order_no', source_kind: 'feishu', reason: '来源的 AMZ 订单号「114-DEMO」格式不对，需人工核实（不当作已采购）',
      fields: { external_amz_order_no: [null, '114-DEMO'] }, incoming: { external_amz_order_no: '114-DEMO' }, at: null,
    }))
    await mountWith(OrderDetailDrawer, { orderId: 7, modelValue: true })
    expect(buttons('保留当前')).toHaveLength(1)
    expect(buttons('登记为已采购')).toHaveLength(0)
    expect(bodyText()).toContain('不能登记为已采购，只能保留当前')
  })

  it('准入变化与平台订单号变化的接受按钮', async () => {
    ordersApi.getOrder.mockResolvedValue(conflictDetail({ kind: 'admission', source_kind: 'feishu', reason: '飞书来源的采购方已不是本系统', fields: {}, incoming: {}, at: null }))
    await mountWith(OrderDetailDrawer, { orderId: 7, modelValue: true })
    expect(buttons('不再采购（取消任务）')).toHaveLength(1)
    mounted.pop()!.unmount()
    document.body.innerHTML = ''
    ordersApi.getOrder.mockResolvedValue(conflictDetail({
      kind: 'source_key_changed', source_kind: 'feishu', reason: '同一来源记录的平台订单号由 PO-DEMO-7 变为 PO-DEMO-8，需人工核实',
      fields: { upstream_order_no: ['PO-DEMO-7', 'PO-DEMO-8'] }, incoming: { upstream_order_no: 'PO-DEMO-8' }, at: null,
    }))
    await mountWith(OrderDetailDrawer, { orderId: 7, modelValue: true })
    expect(buttons('按新订单号接收（取消原任务）')).toHaveLength(1)
    expect(buttons('保留原任务')).toHaveLength(1)
  })
})

// ── 账号 ──────────────────────────────────────────────────────────────────────

function account(extra: Partial<Account> = {}): Account {
  return {
    id: 7, name: 'DEMO-环境-C', amazon_customer_id: 'A1DEMO00003', site: 'US', min_amount: '30.00', max_amount: null, daily_limit: 10,
    priority: 2, max_delivery_days: null, effective_max_delivery_days: 10, purchase_enabled: true, sync_enabled: false,
    execution_paused: true, pause_reason: '结果待核实：PO-DEMO-7', paused_at: '2026-11-05T01:05:00+00:00', pause_cause: 'result_unknown',
    open_result_unknown: 0, purchase_mode: 'manual', schedule_times: [], default_device_id: null, expected_card_last4: null, notes: null,
    quota_today: { limit: 10, used: 1, planned: 0, available: 9 }, devices: [], active_run: null,
    created_at: '2026-11-01T00:00:00+00:00', updated_at: '2026-11-05T01:05:00+00:00',
    ...extra,
  }
}

describe('账号：恢复执行前按最新的暂停起因提醒', () => {
  beforeEach(() => {
    devicesApi.listDevices.mockResolvedValue({ total: 0, rows: [] })
    settingsApi.getSettings.mockResolvedValue({ default_max_delivery_days: 10, simulation_only: true, logistics_sync_time: '08:30', real_purchase_available: false })
  })

  it('重新读账号（GET /accounts/{id}），还有结果待核实的任务时写明数量、按危险操作确认；不再查任务列表', async () => {
    accountsApi.listAccounts.mockResolvedValue({ total: 1, rows: [account()] })
    accountsApi.getAccount.mockResolvedValue(account({ open_result_unknown: 2 }))
    accountsApi.resumeAccount.mockResolvedValue(account({ execution_paused: false, pause_reason: null, pause_cause: null }))
    await mountWith(AccountsPanel)
    expect(bodyText()).toContain('已暂停 · 结果待核实')
    buttons('恢复执行')[0].click()
    await flushPromises()
    expect(accountsApi.getAccount).toHaveBeenCalledWith(7)
    expect(ordersApi.listOrders).not.toHaveBeenCalled()
    expect(feedback.confirmAction).toHaveBeenCalledWith(expect.objectContaining({ message: expect.stringContaining('还有 2 笔结果待核实的任务'), danger: true }))
    expect(accountsApi.resumeAccount).toHaveBeenCalledWith(7)
  })

  it('人工暂停、没有待核实任务：只问一句', async () => {
    accountsApi.listAccounts.mockResolvedValue({ total: 1, rows: [account({ pause_reason: '需要人工登录', pause_cause: 'manual' })] })
    accountsApi.getAccount.mockResolvedValue(account({ pause_reason: '需要人工登录', pause_cause: 'manual' }))
    accountsApi.resumeAccount.mockResolvedValue(account({ execution_paused: false }))
    await mountWith(AccountsPanel)
    buttons('恢复执行')[0].click()
    await flushPromises()
    expect(feedback.confirmAction).toHaveBeenCalledWith(expect.objectContaining({ message: '确定要恢复「DEMO-环境-C」的执行吗？', danger: false }))
  })
})

// ── 异常处理 ──────────────────────────────────────────────────────────────────

describe('异常处理：需要留意的数字取概览的 needs_attention', () => {
  it('标签页显示概览给的数，只为列表查一次任务', async () => {
    overviewApi.getOverview.mockResolvedValue({
      bj_date: '2026-11-05', counts: { exception: 2, result_unknown: 1, repurchase_pending: 0 }, needs_attention: 4, today_executable: 0,
      future_assigned: 0, accounts: { total: 1, enabled: 1, paused: 0 }, devices: { total: 0, online: 0 }, recent_exceptions: [],
      last_allocation: null, simulation_only: true, feishu_configured: true,
    } satisfies Overview)
    ordersApi.listOrders.mockResolvedValue({ total: 0, rows: [] })
    await mountWith(ExceptionsView)
    expect(bodyText()).toContain('需要留意 4')
    expect(ordersApi.listOrders).toHaveBeenCalledTimes(1)
    expect(ordersApi.listOrders).not.toHaveBeenCalledWith(expect.objectContaining({ page_size: 1 }))
  })
})

// ── 登录 ──────────────────────────────────────────────────────────────────────

describe('登录：失败过多', () => {
  it('429 RATE_LIMITED 显示「登录失败：尝试次数过多，请稍后再试」', async () => {
    authApi.login.mockRejectedValue(new ApiError('RATE_LIMITED', '尝试次数过多，请稍后再试', 429, { retry_after_s: 420 }))
    const w = await mountWith(LoginView)
    const inputs = w.findAll('input')
    await inputs[0].setValue('owner')
    await inputs[1].setValue('wrong-password')
    await w.find('form').trigger('submit')
    await flushPromises()
    await new Promise((r) => setTimeout(r, 0))
    await flushPromises()
    expect(authApi.login).toHaveBeenCalled()
    expect(bodyText()).toContain('登录失败：尝试次数过多，请稍后再试')
  })
})

// ── 飞书连接 ──────────────────────────────────────────────────────────────────

describe('飞书连接：写回验收没能恢复原值', () => {
  const conn = {
    id: 5, name: '订单中心 DEMO', link: 'https://demo.feishu.cn/base/bascnDEMO?table=tblDEMO', app_token: 'bascnDEMO',
    source_table_id: 'tblDEMO', source_view_id: null, result_table_id: 'tblDEMO', field_map: {}, admission: {},
    writeback_map: { total: '总额' }, writeback_enabled: true, writeback_verified_at: null, writeback_state: 'unverified',
    writeback_test_record: null, poll_minutes: 10, enabled: true, last_sync_at: null, last_sync_ok_at: null, last_error: null,
    last_stats: null, created_at: '', updated_at: '',
  } as FeishuConnection

  it('通知按错误样式，并在连接卡片上一直提示到手动关掉', async () => {
    feishuApi.getFeishuStatus.mockResolvedValue({ configured: true })
    feishuApi.listConnections.mockResolvedValue({ total: 1, rows: [conn] })
    feedback.promptText.mockResolvedValue('recDEMOtest01')
    feishuApi.verifyWriteback.mockResolvedValue({
      ok: true, mismatches: [], written: { 总额: 0 }, restored: false, restore_error: '恢复测试记录原值失败：飞书接口限流（DEMO）', error: null,
      connection: { ...conn, writeback_verified_at: '2026-11-05T02:00:00+00:00', writeback_state: 'active' },
    })
    await mountWith(FeishuPanel)
    buttons('写回验收')[0].click()
    await flushPromises()
    expect(feishuApi.verifyWriteback).toHaveBeenCalledWith(5, 'recDEMOtest01')
    expect(feedback.notifySummary).toHaveBeenCalledWith(expect.objectContaining({ title: '写回验收通过，但测试记录的原值没能恢复' }), 'error')
    expect(bodyText()).toContain('测试记录 recDEMOtest01 里还留着测试值，请到飞书里手动改回原值。恢复测试记录原值失败：飞书接口限流（DEMO）')
    buttons('关闭提醒')[0].click()
    await flushPromises()
    expect(bodyText()).not.toContain('里还留着测试值')
  })

  it('测试记录已关联采购任务（422）：失败提示带服务端原因', async () => {
    feishuApi.getFeishuStatus.mockResolvedValue({ configured: true })
    feishuApi.listConnections.mockResolvedValue({ total: 1, rows: [conn] })
    feedback.promptText.mockResolvedValue('recDEMO000101')
    const e = new ApiError('VALIDATION', '这条记录已关联采购任务 PO-DEMO-101，验收会写入测试值，请改用一条专用测试记录', 422, { field: 'record_id' })
    feishuApi.verifyWriteback.mockRejectedValue(e)
    await mountWith(FeishuPanel)
    buttons('写回验收')[0].click()
    await flushPromises()
    expect(feedback.notifyFailure).toHaveBeenCalledWith('写回验收', e)
  })
})

describe('飞书向导：表格没有授权给本工作区（403 FORBIDDEN）', () => {
  it('原样显示服务端的说明（含 python cli.py feishu_grant）', async () => {
    const message = '这个多维表格还没有授权给当前工作区使用。链接只用来定位表格，不等于授权；请让部署管理员在服务器上运行 python cli.py feishu_grant 授权'
    feishuApi.getSystemFields.mockResolvedValue({ source_fields: [{ key: 'upstream_order_no', label: '平台订单号', required: true }], result_fields: [] })
    feishuApi.resolveLink.mockRejectedValue(new ApiError('FORBIDDEN', message, 403))
    const w = await mountWith(FeishuWizard, { connection: null, modelValue: false })
    await w.setProps({ modelValue: true } as Record<string, unknown>)
    await flushPromises()
    const input = [...document.body.querySelectorAll('.el-dialog input')][0] as HTMLInputElement
    input.value = 'https://demo-tenant.feishu.cn/base/bascnDEMO0000000000000009?table=tblDEMO'
    input.dispatchEvent(new Event('input'))
    await flushPromises()
    buttons('下一步')[0].click()
    await flushPromises()
    expect(feishuApi.resolveLink).toHaveBeenCalled()
    expect(bodyText()).toContain(`解析链接失败：${message}`)
  })
})

// ── 文件导入 ──────────────────────────────────────────────────────────────────

describe('文件导入：「继续」按原文件的表头与工作表恢复', () => {
  it('表头按 GET /imports/{id} 的 headers 排，工作表下拉列出全部工作表', async () => {
    const batch: ImportBatch = {
      id: 43, filename: 'orders-demo-draft.xlsx', sheet_name: '采购订单', status: 'uploaded', stats: null,
      mapping: { 限价: 'price_cap', 平台订单号: 'upstream_order_no', 备注: null },
      sheets: [
        { name: '采购订单', row_count: 12, headers: ['平台订单号', '备注', '限价'] },
        { name: '说明', row_count: 2, headers: ['说明'] },
      ],
      headers: ['平台订单号', '备注', '限价'],
      created_at: '2026-11-05T02:00:00+00:00', committed_at: null, uploaded_by_name: 'DEMO 所有者',
    }
    importsApi.listImports.mockResolvedValue({ total: 1, rows: [batch] })
    importsApi.listTemplates.mockResolvedValue({ total: 0, rows: [] })
    importsApi.getImport.mockResolvedValue(batch)
    feishuApi.getSystemFields.mockResolvedValue({
      source_fields: [
        { key: 'upstream_order_no', label: '平台订单号', required: true },
        { key: 'price_cap', label: '限价', required: false },
      ],
      result_fields: [],
    })
    const w = await mountWith(ImportPanel)
    buttons('继续')[0].click()
    await flushPromises()
    const rows = [...document.body.querySelectorAll('.el-table__body tr')].map((r) => (r.textContent ?? '').trim())
    const order = ['平台订单号', '备注', '限价'].map((h) => rows.findIndex((t) => t.startsWith(h)))
    expect(order.every((i) => i >= 0)).toBe(true)
    expect([...order].sort((a, b) => a - b)).toEqual(order)
    const sheetSelect = w.findAllComponents(ElSelect).find((s) => s.props('modelValue') === '采购订单')!
    expect(sheetSelect).toBeTruthy()
    const options = sheetSelect.findAllComponents({ name: 'ElOption' }).map((o) => o.props('value'))
    expect(options).toEqual(['采购订单', '说明'])
  })
})
