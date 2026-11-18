/**
 * 来源指定买家号（分配两种方式并存）在控制台上的呈现：
 * - 纯函数：指定的显示文字、人工改派后缀、解除分配 / 改派 / 重新分配的文案、分配摘要一行、时间线里的指定变化；
 * - 组件：任务列表的分配方式、任务详情「分配」区的「来源指定的买家号」一行、解除分配确认与改派对话框、
 *   概览的分配摘要与未分出列表上的「来源指定」小标、待采购页的重新分配确认与待分配空状态。
 * 接口与提示框打桩；账号名、买家号 ID、订单号全部虚构。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h, type Component, type DefineComponent } from 'vue'
import type { Account, AllocationSummary, DesignatedBuyer, EventRecord, OrderDetail, OrderRow, Overview } from '@/api/types'

const ordersApi = vi.hoisted(() => ({ listOrders: vi.fn(), getOrder: vi.fn(), assignOrder: vi.fn(), unassignOrder: vi.fn() }))
vi.mock('@/api/orders', () => ordersApi)

const accountsApi = vi.hoisted(() => ({ listAccounts: vi.fn() }))
vi.mock('@/api/accounts', () => accountsApi)

const overviewApi = vi.hoisted(() => ({ getOverview: vi.fn() }))
vi.mock('@/api/overview', () => overviewApi)

const allocationApi = vi.hoisted(() => ({ runAllocation: vi.fn() }))
vi.mock('@/api/allocation', () => allocationApi)

const feedback = vi.hoisted(() => ({
  confirmAction: vi.fn(async (_opts: { message: string }) => true),
  promptText: vi.fn(async (): Promise<string | null> => null),
  notifyFailure: vi.fn(),
  notifySuccess: vi.fn(),
  notifySummary: vi.fn(),
  runDownload: vi.fn(),
  toast: vi.fn(),
}))
vi.mock('@/utils/feedback', () => feedback)

import OrderTable from '@/components/orders/OrderTable.vue'
import OrderDetailDrawer from '@/components/orders/OrderDetailDrawer.vue'
import OverviewView from '@/views/OverviewView.vue'
import OrdersView from '@/views/OrdersView.vue'
import {
  ACCOUNT_NAME_HELP,
  DESIGNATION_MAPPING_HELP,
  REALLOCATE_CONFIRM_TEXT,
  UNASSIGNED_EMPTY_TEXT,
  UNASSIGN_CONFIRM_TEXT,
  assignDesignationNote,
  designationName,
  designationOf,
  designationRowText,
  designationText,
  hasDesignation,
  manualOverrideAccount,
  unassignConfirmText,
} from '@/utils/designation'
import { ASSIGNMENT_KIND_LABEL, eventKindLabel, fieldLabel, labelOf } from '@/utils/labels'
import { NOT_DESIGNATED_TEXT, changesText, eventSourceText, eventSummary } from '@/utils/orderDetail'
import { allocationBreakdown } from '@/utils/overview'
import { allocationBrief } from '@/utils/results'

// ── 夹具 ──────────────────────────────────────────────────────────────────────

const ACC_A = { id: 3, name: '环境-DEMO-A' }
const ACC_B = { id: 4, name: '环境-DEMO-B' }

function orderRow(id: number, extra: Partial<OrderRow> = {}): OrderRow {
  return {
    id, upstream_order_no: `PO-DEMO-${String(id).padStart(4, '0')}`, asin: 'B0DEMO0001', asin_eff: 'B0DEMO0001', quantity: 1, quantity_eff: 1,
    unit_price: '24.99', subtotal_eff: '24.99', price_cap: '31.80', price_cap_eff: '31.80', has_override: false,
    suggested_purchase_date: '2026-11-18', order_time: null, recipient_name: 'Demo Buyer', state: 'FL', postal_code: '32960',
    status: 'assigned', status_reason: null, missing_fields: [], review_status: 'approved', needs_attention: false,
    attention_note: null, assigned_account: ACC_A, assignment_kind: 'auto', assignment_note: '保底：当天首单',
    primary_source_kind: 'feishu', source_kinds: ['feishu'], latest_attempt: null, updated_at: '2026-11-18T02:00:00+00:00',
    ...extra,
  }
}

const des = (customer_id: string | null, name: string | null, account?: { id: number; name: string } | null): DesignatedBuyer =>
  account === undefined ? { customer_id, name } : { customer_id, name, account }

/** 按来源的买家号 ID 分给了登记着它的账号 */
const bySource = (id = 11) =>
  orderRow(id, { assigned_account: ACC_A, assignment_kind: 'source', assignment_note: '按来源的买家号 ID A1DEMO0000', designated_buyer: des('A1DEMO0000', '环境-DEMO-A', ACC_A) })
/** 来源指定了，账号此刻接不了：留在待分配并写明原因 */
const stuck = (id = 12) =>
  orderRow(id, {
    status: 'queued', assigned_account: null, assignment_kind: null, assignment_note: null,
    status_reason: '来源指定的买家号 ID A2TEST0007（买家号「环境-DEMO-C」）还没有登记为账号；在「账号与浏览器」登记后会自动分配',
    designated_buyer: des('A2TEST0007', '环境-DEMO-C', null),
  })
/** 来源指定了 A，人工改派到了 B */
const overridden = (id = 13) =>
  orderRow(id, { assigned_account: ACC_B, assignment_kind: 'manual', assignment_note: '人工改派', designated_buyer: des('A1DEMO0000', '环境-DEMO-A', ACC_A) })

function event(id: number, kind: string, detail: Record<string, unknown>, extra: Partial<EventRecord> = {}): EventRecord {
  return { id, entity: 'order', entity_id: 7, order_id: 7, at: '2026-11-18T02:00:00+00:00', actor_kind: 'source', actor_id: null, actor_name: null, kind, detail, ...extra }
}

function detail(row: OrderRow, extra: Partial<OrderDetail> = {}): OrderDetail {
  return {
    ...row,
    original: { asin: 'B0DEMO0001', quantity: 1, price_cap: '31.80', unit_price: '24.99', ship_days: 4, order_time: null, suggested_purchase_date: '2026-11-18', suggested_date_basis: 'source' },
    override: null,
    address: { recipient_name: 'Demo Buyer', address1: '1 Demo St', address2: null, city: 'Sampletown', state: 'FL', postal_code: '32960', phone: '555-0100', country: 'US', district: null },
    conflict: null,
    sources: [],
    attempts: [],
    events: [],
    allowed_actions: row.assigned_account ? ['assign', 'unassign', 'override', 'cancel'] : ['assign', 'override', 'cancel'],
    ...extra,
  }
}

function account(extra: Partial<Account> = {}): Account {
  return {
    id: 3, name: '环境-DEMO-A', amazon_customer_id: 'A1DEMO0000', site: 'US', min_amount: '20.00', max_amount: '30.00', daily_limit: 20,
    priority: 5, max_delivery_days: null, effective_max_delivery_days: 10, purchase_enabled: true, sync_enabled: true,
    execution_paused: false, pause_reason: null, paused_at: null, pause_cause: null, open_result_unknown: 0,
    purchase_mode: 'manual', schedule_times: [], expected_card_last4: null, notes: null,
    quota_today: { limit: 20, used: 3, planned: 5, available: 12 }, browsers: [], active_run: null,
    created_at: '2026-11-01T00:00:00+00:00', updated_at: '2026-11-05T01:05:00+00:00',
    ...extra,
  }
}

function allocation(stats: AllocationSummary['stats'], unassigned: AllocationSummary['unassigned'] = []): AllocationSummary {
  return { id: 57, bj_date: '2026-11-18', at: '2026-11-18T02:00:00+00:00', trigger: 'intake', stats, uncovered: [], unassigned }
}

function overview(extra: Partial<Overview> = {}): Overview {
  return {
    bj_date: '2026-11-18', counts: { queued: 2, assigned: 5 }, needs_attention: 0, today_executable: 3, future_assigned: 2,
    accounts: { total: 2, enabled: 2, paused: 0 }, devices: { total: 1, online: 1 }, discovered_buyers: 0, recent_exceptions: [],
    last_allocation: null, simulation_only: true, feishu_configured: true,
    ...extra,
  }
}

// ── 挂载 ──────────────────────────────────────────────────────────────────────

const mounted: VueWrapper[] = []
async function mountWith(comp: Component, props: Record<string, unknown> = {}, path = '/') {
  const Blank = defineComponent({ render: () => h('div') })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: Blank }] })
  await router.push(path)
  await router.isReady()
  const w = mount(comp as DefineComponent, { props, attachTo: document.body, global: { plugins: [ElementPlus, router] } })
  mounted.push(w)
  await flushPromises()
  return w
}
function buttons(text: string, root: ParentNode = document.body): HTMLButtonElement[] {
  return [...root.querySelectorAll('button')].filter((b) => (b.textContent ?? '').trim() === text) as HTMLButtonElement[]
}
const bodyText = () => document.body.textContent ?? ''
const confirmMessage = (call = 0) => (feedback.confirmAction.mock.calls[call] as unknown as [{ message: string }])[0].message
async function settle() {
  await flushPromises()
  await new Promise((r) => setTimeout(r, 0))
  await flushPromises()
}

beforeEach(() => {
  vi.clearAllMocks()
  feedback.confirmAction.mockResolvedValue(true)
  accountsApi.listAccounts.mockResolvedValue({ total: 2, rows: [account(), account({ id: 4, name: '环境-DEMO-B', amazon_customer_id: 'A1DEMO0001', max_amount: '50.00' })] })
  ordersApi.listOrders.mockResolvedValue({ total: 0, rows: [] })
})
afterEach(() => {
  while (mounted.length) mounted.pop()!.unmount()
  document.body.innerHTML = ''
})

// ── 纯函数 ────────────────────────────────────────────────────────────────────

describe('来源指定的买家号：有没有指定', () => {
  it('字段缺失（旧服务端）、null、两列都空都算没有指定', () => {
    expect(hasDesignation(orderRow(1))).toBe(false)
    expect(hasDesignation(orderRow(1, { designated_buyer: null }))).toBe(false)
    expect(hasDesignation(orderRow(1, { designated_buyer: des(null, null, null) }))).toBe(false)
    expect(hasDesignation(orderRow(1, { designated_buyer: des('  ', '', null) }))).toBe(false)
    expect(hasDesignation(null)).toBe(false)
    expect(designationOf(orderRow(1))).toBeNull()
    expect(designationName(orderRow(1))).toBe('')
    expect(designationText(orderRow(1))).toBe('')
    expect(designationRowText(orderRow(1))).toBe('')
  })

  it('只填 ID、只填名称、都填：去掉首尾空白，解析到的账号原样带上', () => {
    expect(hasDesignation(orderRow(1, { designated_buyer: des('A1DEMO0000', null) }))).toBe(true)
    expect(hasDesignation(orderRow(1, { designated_buyer: des(null, '环境-DEMO-A') }))).toBe(true)
    expect(designationOf(orderRow(1, { designated_buyer: des(' A1DEMO0000 ', ' 环境-DEMO-A ', ACC_A) }))).toEqual({ customer_id: 'A1DEMO0000', name: '环境-DEMO-A', account: ACC_A })
    expect(designationOf(orderRow(1, { designated_buyer: des('A1DEMO0000', '') }))).toEqual({ customer_id: 'A1DEMO0000', name: null, account: undefined })
  })

  it('文案里的「名或 ID」：有名称用名称，否则用买家号 ID', () => {
    expect(designationName(orderRow(1, { designated_buyer: des('A1DEMO0000', '环境-DEMO-A') }))).toBe('环境-DEMO-A')
    expect(designationName(orderRow(1, { designated_buyer: des('A1DEMO0000', null) }))).toBe('A1DEMO0000')
    expect(designationName(orderRow(1, { designated_buyer: des(null, '环境-DEMO-A') }))).toBe('环境-DEMO-A')
  })

  it('详情里显示「名（ID）」，缺哪个省哪个', () => {
    expect(designationText(orderRow(1, { designated_buyer: des('A1DEMO0000', '环境-DEMO-A') }))).toBe('环境-DEMO-A（A1DEMO0000）')
    expect(designationText(orderRow(1, { designated_buyer: des('A1DEMO0000', null) }))).toBe('A1DEMO0000')
    expect(designationText(orderRow(1, { designated_buyer: des(null, '环境-DEMO-A') }))).toBe('环境-DEMO-A')
  })
})

describe('人工改派到别的账号时的后缀', () => {
  it('来源指定 A、人工改派到 B：后面加「· 已人工改派到 B」', () => {
    expect(manualOverrideAccount(overridden())).toBe('环境-DEMO-B')
    expect(designationRowText(overridden())).toBe('环境-DEMO-A（A1DEMO0000）· 已人工改派到 环境-DEMO-B')
    // 只有名称或只有 ID 时，分隔点前留一个空格
    expect(designationRowText(orderRow(1, { assigned_account: ACC_B, assignment_kind: 'manual', designated_buyer: des(null, '环境-DEMO-A', ACC_A) }))).toBe('环境-DEMO-A · 已人工改派到 环境-DEMO-B')
    expect(designationRowText(orderRow(1, { assigned_account: ACC_B, assignment_kind: 'manual', designated_buyer: des('A1DEMO0000', null, ACC_A) }))).toBe('A1DEMO0000 · 已人工改派到 环境-DEMO-B')
  })

  it('人工改派到的就是来源指定的账号、分配方式不是人工、没有分配：都不加后缀', () => {
    expect(designationRowText(orderRow(1, { assigned_account: ACC_A, assignment_kind: 'manual', designated_buyer: des('A1DEMO0000', '环境-DEMO-A', ACC_A) }))).toBe('环境-DEMO-A（A1DEMO0000）')
    expect(designationRowText(bySource())).toBe('环境-DEMO-A（A1DEMO0000）')
    expect(designationRowText(stuck())).toBe('环境-DEMO-C（A2TEST0007）')
    // 自动分配的任务后来被来源指定了别的账号（分配还没重算）：不是人工改派，不加后缀
    expect(manualOverrideAccount(orderRow(1, { assigned_account: ACC_B, assignment_kind: 'auto', designated_buyer: des('A1DEMO0000', null, ACC_A) }))).toBe('')
  })

  it('指定解析不出账号（没登记、已归档）时，人工改派到任何账号都算「别的账号」', () => {
    expect(designationRowText(orderRow(1, { assigned_account: ACC_B, assignment_kind: 'manual', designated_buyer: des('A2TEST0007', null, null) }))).toBe('A2TEST0007 · 已人工改派到 环境-DEMO-B')
  })

  it('旧服务端不给解析到的账号：退回按名称比', () => {
    expect(manualOverrideAccount(orderRow(1, { assigned_account: ACC_A, assignment_kind: 'manual', designated_buyer: des(null, '环境-DEMO-A') }))).toBe('')
    expect(manualOverrideAccount(orderRow(1, { assigned_account: ACC_B, assignment_kind: 'manual', designated_buyer: des(null, '环境-DEMO-A') }))).toBe('环境-DEMO-B')
    // 只有 ID、没有可比的名称：按不是同一个账号处理
    expect(manualOverrideAccount(orderRow(1, { assigned_account: ACC_B, assignment_kind: 'manual', designated_buyer: des('A1DEMO0000', null) }))).toBe('环境-DEMO-B')
  })
})

describe('文案', () => {
  it('解除分配确认：有指定时说明下次分配会再分给它；没有指定时是原来的一句', () => {
    expect(unassignConfirmText(bySource())).toBe('确定要解除这笔任务的账号分配吗？来源指定了买家号「环境-DEMO-A」，下次分配时会再分给它；要换账号请用「改派账号」。')
    expect(unassignConfirmText(orderRow(1, { designated_buyer: des('A1DEMO0000', null, ACC_A) }))).toBe(
      '确定要解除这笔任务的账号分配吗？来源指定了买家号「A1DEMO0000」，下次分配时会再分给它；要换账号请用「改派账号」。',
    )
    expect(unassignConfirmText(orderRow(1))).toBe('确定要解除这笔任务的账号分配吗？任务会回到待分配。')
    expect(unassignConfirmText(orderRow(1))).toBe(UNASSIGN_CONFIRM_TEXT)
    expect(unassignConfirmText(null)).toBe(UNASSIGN_CONFIRM_TEXT)
  })

  it('改派对话框：有指定时在原说明前加一段；没有指定时不加', () => {
    expect(assignDesignationNote(overridden())).toBe(
      '来源给这笔任务指定了买家号「环境-DEMO-A」。改派后以改派为准；任务留在新账号期间不再跟随来源的指定，回到待分配后仍按来源指定分配。',
    )
    expect(assignDesignationNote(orderRow(1, { designated_buyer: des('A2TEST0007', null, null) }))).toContain('指定了买家号「A2TEST0007」')
    expect(assignDesignationNote(orderRow(1))).toBe('')
    expect(assignDesignationNote(orderRow(1, { designated_buyer: null }))).toBe('')
  })

  it('固定文案逐字与定稿一致', () => {
    expect(REALLOCATE_CONFIRM_TEXT).toBe('确定要重新分配吗？来源指定了买家号的任务按指定分配，其余自动分配的任务按当前账号配置重算，人工改派的任务保持不动。')
    expect(UNASSIGNED_EMPTY_TEXT).toBe('分不出去的任务会写明原因：金额不在任何启用账号的区间内、匹配账号当天额度已满，或来源指定的买家号暂时接不了。')
    expect(DESIGNATION_MAPPING_HELP).toBe(
      '「买家号」「买家号 ID」填了，这笔订单就指定给对应的账号（买家号按账号名称对应，买家号 ID 按账号登记的 ID 对应，都填以 ID 为准）；留空由系统自动分配。已有 AMZ 订单号的行，这两列只当作历史采购记录。',
    )
    expect(ACCOUNT_NAME_HELP).toBe('来源按「买家号」指定时靠名称对应账号，改名后这些任务会回到待分配。')
  })

  it('统一叫「来源指定」，不出现「按表指定」；确认文案都是「确定要……吗？」；不用感叹号', () => {
    const all = [
      REALLOCATE_CONFIRM_TEXT, UNASSIGNED_EMPTY_TEXT, DESIGNATION_MAPPING_HELP, ACCOUNT_NAME_HELP, UNASSIGN_CONFIRM_TEXT,
      unassignConfirmText(bySource()), assignDesignationNote(bySource()), allocationBreakdown({ considered: 1, assigned: 1, coverage: 0, fill: 0, designated: 1, unassigned: 0 }),
      ASSIGNMENT_KIND_LABEL.source,
    ]
    for (const s of all) {
      expect(s).not.toContain('按表指定')
      expect(s).not.toMatch(/[!！]/)
    }
    for (const s of [REALLOCATE_CONFIRM_TEXT, UNASSIGN_CONFIRM_TEXT, unassignConfirmText(bySource())]) expect(s).toMatch(/^确定要[^？]+吗？/)
  })
})

describe('分配方式与分配摘要', () => {
  it('分配方式标签：来源指定', () => {
    expect(ASSIGNMENT_KIND_LABEL.source).toBe('来源指定')
    expect(labelOf(ASSIGNMENT_KIND_LABEL, 'source')).toBe('来源指定')
    expect(labelOf(ASSIGNMENT_KIND_LABEL, 'auto')).toBe('自动分配')
    expect(labelOf(ASSIGNMENT_KIND_LABEL, 'manual')).toBe('人工改派')
  })

  it('摘要一行：来源指定 n 笔 · 保底 c 笔（每个账号当天首单）· 按优先级填充 f 笔', () => {
    expect(allocationBreakdown({ considered: 12, assigned: 9, coverage: 3, fill: 4, designated: 2, unassigned: 3 })).toBe('来源指定 2 笔 · 保底 3 笔（每个账号当天首单）· 按优先级填充 4 笔')
    expect(allocationBreakdown({ considered: 1234, assigned: 1234, coverage: 4, fill: 30, designated: 1200, unassigned: 0 })).toBe('来源指定 1,200 笔 · 保底 4 笔（每个账号当天首单）· 按优先级填充 30 笔')
  })

  it('升级前存下的分配记录没有 designated：按 0 显示，不是「—」', () => {
    expect(allocationBreakdown({ considered: 9, assigned: 7, coverage: 3, fill: 4, unassigned: 2 })).toBe('来源指定 0 笔 · 保底 3 笔（每个账号当天首单）· 按优先级填充 4 笔')
    expect(allocationBreakdown({ considered: 9, assigned: 7, coverage: 3, fill: 4, designated: null as unknown as number, unassigned: 2 })).toContain('来源指定 0 笔')
    expect(allocationBreakdown(null)).toBe('')
  })

  it('导入 / 同步之后的一句（分出、未分出）不受影响：分出里已经含来源指定的', () => {
    expect(allocationBrief(allocation({ considered: 12, assigned: 9, coverage: 3, fill: 4, designated: 2, unassigned: 3 }))).toBe('随后分配：分出 9 笔，未分出 3 笔')
  })
})

describe('时间线：来源指定的买家号变了', () => {
  it('事件与字段的中文名', () => {
    expect(eventKindLabel('order.designation_changed')).toBe('来源指定的买家号变了')
    expect(fieldLabel('designated_buyer_name')).toBe('指定买家号')
    expect(fieldLabel('designated_customer_id')).toBe('指定买家号 ID')
    expect(NOT_DESIGNATED_TEXT).toBe('未指定')
  })

  it('按 detail.changes 的扁平键显示「旧 → 新」，空显示「未指定」', () => {
    const src = { kind: 'feishu', connection_id: 1, record_id: 'recDEMO1' }
    expect(eventSummary(event(1, 'order.designation_changed', { changes: { designated_customer_id: [null, 'A1DEMO0000'], designated_buyer_name: [null, '环境-DEMO-A'] }, source: src }))).toBe(
      '飞书记录 recDEMO1 · 指定买家号 ID 未指定 → A1DEMO0000；指定买家号 未指定 → 环境-DEMO-A',
    )
    // 改了：只列变了的那一列
    expect(eventSummary(event(1, 'order.designation_changed', { changes: { designated_buyer_name: ['环境-DEMO-A', '环境-DEMO-B'] }, source: src }))).toBe('飞书记录 recDEMO1 · 指定买家号 环境-DEMO-A → 环境-DEMO-B')
    // 清空：null、空串、缺失都写「未指定」
    expect(eventSummary(event(1, 'order.designation_changed', { changes: { designated_customer_id: ['A1DEMO0000', null], designated_buyer_name: ['环境-DEMO-A', ''] }, source: { kind: 'file', batch_id: 3, row_no: 6 } }))).toBe(
      '文件批次 3 第 6 行 · 指定买家号 ID A1DEMO0000 → 未指定；指定买家号 环境-DEMO-A → 未指定',
    )
    expect(changesText({ designated_customer_id: [undefined, 'A1DEMO0000'] })).toBe('指定买家号 ID 未指定 → A1DEMO0000')
  })

  it('旧库升级时的一次性回填（source = "upgrade"）写明是升级补上的', () => {
    expect(eventSourceText('upgrade')).toBe('升级时按来源里已有的买家号补上')
    expect(eventSummary(event(1, 'order.designation_changed', { changes: { designated_customer_id: [null, 'A1DEMO0000'] }, source: 'upgrade' }, { actor_kind: 'system' }))).toBe(
      '升级时按来源里已有的买家号补上 · 指定买家号 ID 未指定 → A1DEMO0000',
    )
    // 别的字符串不当作来源
    expect(eventSourceText('something')).toBe('')
  })

  it('来源更新事件里的指定变化与别的字段写在一起；别的字段的空值仍是「—」；不出现英文键与 JSON', () => {
    const s = eventSummary(event(1, 'order.source_updated', {
      source: { kind: 'feishu', connection_id: 1, record_id: 'recDEMO1' },
      changes: { quantity: [1, 2], price_cap: [null, '30.00'], designated_buyer_name: ['环境-DEMO-A', null] },
    }))
    expect(s).toBe('飞书记录 recDEMO1 · 数量 1 → 2；限价 — → $30.00；指定买家号 环境-DEMO-A → 未指定')
    expect(s).not.toMatch(/designated|[{}]/)
  })

  it('分配事件带 kind = source 时摘要仍是分配说明与账号名（不把 kind 原样打出来）', () => {
    expect(eventSummary(event(1, 'order.assigned', { account_id: 3, account_name: '环境-DEMO-A', note: '按来源的买家号 ID A1DEMO0000', trigger: 'intake', from_account_id: null, kind: 'source' }, { actor_kind: 'system' }))).toBe(
      '按来源的买家号 ID A1DEMO0000 · 环境-DEMO-A',
    )
  })
})

// ── 任务列表 ──────────────────────────────────────────────────────────────────

describe('任务列表：分配方式与说明', () => {
  const rows = [bySource(11), stuck(12), overridden(13), orderRow(14)]
  const texts = (w: VueWrapper) => w.findAll('.el-table__body tr').map((r) => r.text())

  it('账号列在账号名下显示分配方式「来源指定」；说明列是分配说明，待分配的写服务端给的原因', async () => {
    const w = await mountWith(OrderTable, { rows, mode: 'pending' })
    const t = texts(w)
    expect(t).toHaveLength(4)
    expect(t[0]).toContain('环境-DEMO-A')
    expect(t[0]).toContain('来源指定')
    expect(t[0]).toContain('按来源的买家号 ID A1DEMO0000')
    // 指定了但分不出去：没有账号、没有分配方式，说明列写原因
    expect(t[1]).toContain('未分配')
    expect(t[1]).toContain('来源指定的买家号 ID A2TEST0007（买家号「环境-DEMO-C」）还没有登记为账号；在「账号与浏览器」登记后会自动分配')
    // 人工改派优先：显示改派到的账号与「人工改派」
    expect(t[2]).toContain('环境-DEMO-B')
    expect(t[2]).toContain('人工改派')
    expect(t[2]).not.toContain('来源指定')
    expect(t[3]).toContain('自动分配')
    expect(t[3]).toContain('保底：当天首单')
    expect(t[3]).not.toContain('来源指定')
  })

  it('采购记录里也显示分配方式；精简模式（概览）不受影响', async () => {
    const purchased = bySource(21)
    const w = await mountWith(OrderTable, { rows: [{ ...purchased, status: 'purchased' }], mode: 'records' })
    expect(texts(w)[0]).toContain('环境-DEMO-A')
    expect(texts(w)[0]).toContain('来源指定')
    mounted.pop()!.unmount()
    const c = await mountWith(OrderTable, { rows: [stuck(22)], mode: 'compact' })
    expect(texts(c)[0]).toContain('未分配')
    expect(texts(c)[0]).toContain('还没有登记为账号')
  })
})

// ── 任务详情 ──────────────────────────────────────────────────────────────────

describe('任务详情：「分配」区的来源指定', () => {
  const designatedRow = () => document.body.querySelector('[data-testid="designated-buyer"]') as HTMLElement | null

  it('有指定时多一行「来源指定的买家号」：名（ID）；分配方式显示「来源指定」', async () => {
    ordersApi.getOrder.mockResolvedValue(detail(bySource(7)))
    await mountWith(OrderDetailDrawer, { orderId: 7, modelValue: true })
    expect(bodyText()).toContain('来源指定的买家号')
    expect(designatedRow()!.textContent).toBe('环境-DEMO-A（A1DEMO0000）')
    const dts = [...document.body.querySelectorAll('.pc-kv dt')].map((d) => d.textContent)
    expect(dts).toContain('来源指定的买家号')
    // 紧跟在分配说明之后
    expect(dts.indexOf('来源指定的买家号')).toBe(dts.indexOf('分配说明') + 1)
    const kind = [...document.body.querySelectorAll('.pc-kv dt')].find((d) => d.textContent === '分配方式')!.nextElementSibling as HTMLElement
    expect(kind.textContent).toBe('来源指定')
  })

  it('缺哪个省哪个；指定了但没分出去时照样显示（账号写「未分配」）', async () => {
    ordersApi.getOrder.mockResolvedValue(detail(orderRow(7, { status: 'queued', assigned_account: null, assignment_kind: null, assignment_note: null, status_reason: '来源指定的买家号「环境-DEMO-C」没有同名账号；新建同名账号、在来源里改正，或改派到其他账号', designated_buyer: des(null, '环境-DEMO-C', null) })))
    await mountWith(OrderDetailDrawer, { orderId: 7, modelValue: true })
    expect(designatedRow()!.textContent).toBe('环境-DEMO-C')
    expect(bodyText()).toContain('未分配')
    // 原因在顶部的状态说明里
    expect(bodyText()).toContain('来源指定的买家号「环境-DEMO-C」没有同名账号')
  })

  it('当前是人工改派到别的账号：后面加「· 已人工改派到 B」', async () => {
    ordersApi.getOrder.mockResolvedValue(detail(overridden(7)))
    await mountWith(OrderDetailDrawer, { orderId: 7, modelValue: true })
    expect(designatedRow()!.textContent).toBe('环境-DEMO-A（A1DEMO0000）· 已人工改派到 环境-DEMO-B')
    expect(bodyText()).toContain('人工改派')
  })

  it('没有指定（null 或旧服务端不给这个字段）时不显示这一行', async () => {
    ordersApi.getOrder.mockResolvedValue(detail(orderRow(7, { designated_buyer: null })))
    await mountWith(OrderDetailDrawer, { orderId: 7, modelValue: true })
    expect(designatedRow()).toBeNull()
    expect(bodyText()).not.toContain('来源指定的买家号')
    mounted.pop()!.unmount()
    document.body.innerHTML = ''
    ordersApi.getOrder.mockResolvedValue(detail(orderRow(7)))
    await mountWith(OrderDetailDrawer, { orderId: 7, modelValue: true })
    expect(designatedRow()).toBeNull()
    expect(bodyText()).toContain('自动分配')
  })

  it('事件时间线：「来源指定的买家号变了」按「旧 → 新」显示，空写「未指定」', async () => {
    ordersApi.getOrder.mockResolvedValue(detail(bySource(7), {
      events: [
        event(2, 'order.assigned', { account_name: '环境-DEMO-A', note: '按来源的买家号 ID A1DEMO0000', kind: 'source' }, { actor_kind: 'system' }),
        event(1, 'order.designation_changed', { changes: { designated_customer_id: [null, 'A1DEMO0000'], designated_buyer_name: [null, '环境-DEMO-A'] }, source: { kind: 'feishu', connection_id: 1, record_id: 'recDEMO1' } }),
      ],
    }))
    await mountWith(OrderDetailDrawer, { orderId: 7, modelValue: true })
    expect(bodyText()).toContain('来源指定的买家号变了')
    expect(bodyText()).toContain('飞书记录 recDEMO1 · 指定买家号 ID 未指定 → A1DEMO0000；指定买家号 未指定 → 环境-DEMO-A')
    expect(bodyText()).not.toContain('order.designation_changed')
    expect(bodyText()).not.toContain('designated_')
  })
})

describe('任务详情：解除分配与改派的文案', () => {
  it('解除分配：来源指定了买家号时，确认框说明下次分配会再分给它', async () => {
    ordersApi.getOrder.mockResolvedValue(detail(bySource(7)))
    ordersApi.unassignOrder.mockResolvedValue(detail(orderRow(7, { status: 'queued', assigned_account: null, assignment_kind: null, assignment_note: null, status_reason: '人工解除分配', designated_buyer: des('A1DEMO0000', '环境-DEMO-A', ACC_A) })))
    await mountWith(OrderDetailDrawer, { orderId: 7, modelValue: true })
    buttons('解除分配')[0].click()
    await settle()
    expect(confirmMessage()).toBe('确定要解除这笔任务的账号分配吗？来源指定了买家号「环境-DEMO-A」，下次分配时会再分给它；要换账号请用「改派账号」。')
    expect(ordersApi.unassignOrder).toHaveBeenCalledWith(7)
    // 解除之后指定还在
    expect((document.body.querySelector('[data-testid="designated-buyer"]') as HTMLElement).textContent).toBe('环境-DEMO-A（A1DEMO0000）')
  })

  it('解除分配：人工改派到别的账号的任务，文案引用的仍是来源指定的买家号', async () => {
    ordersApi.getOrder.mockResolvedValue(detail(overridden(7)))
    feedback.confirmAction.mockResolvedValue(false)
    await mountWith(OrderDetailDrawer, { orderId: 7, modelValue: true })
    buttons('解除分配')[0].click()
    await settle()
    expect(confirmMessage()).toContain('来源指定了买家号「环境-DEMO-A」')
    expect(ordersApi.unassignOrder).not.toHaveBeenCalled()
  })

  it('解除分配：没有指定时是原来的一句', async () => {
    ordersApi.getOrder.mockResolvedValue(detail(orderRow(7)))
    feedback.confirmAction.mockResolvedValue(false)
    await mountWith(OrderDetailDrawer, { orderId: 7, modelValue: true })
    buttons('解除分配')[0].click()
    await settle()
    expect(confirmMessage()).toBe('确定要解除这笔任务的账号分配吗？任务会回到待分配。')
  })

  it('改派对话框：来源指定了买家号时在原说明前加一段；原说明保留', async () => {
    ordersApi.getOrder.mockResolvedValue(detail(stuck(7)))
    await mountWith(OrderDetailDrawer, { orderId: 7, modelValue: true })
    buttons('改派账号')[0].click()
    await settle()
    const note = document.body.querySelector('[data-testid="assign-designation-note"]') as HTMLElement
    expect(note.textContent).toBe('来源给这笔任务指定了买家号「环境-DEMO-C」。改派后以改派为准；任务留在新账号期间不再跟随来源的指定，回到待分配后仍按来源指定分配。')
    const original = note.nextElementSibling as HTMLElement
    expect(original.textContent).toContain('人工改派后任务固定在该账号，不参与自动重算。')
  })

  it('改派对话框：没有指定时只有原说明', async () => {
    ordersApi.getOrder.mockResolvedValue(detail(orderRow(7)))
    await mountWith(OrderDetailDrawer, { orderId: 7, modelValue: true })
    buttons('改派账号')[0].click()
    await settle()
    expect(document.body.querySelector('[data-testid="assign-designation-note"]')).toBeNull()
    expect(bodyText()).toContain('人工改派后任务固定在该账号，不参与自动重算。')
    expect(bodyText()).not.toContain('来源给这笔任务指定了买家号')
  })
})

// ── 概览与待采购页 ────────────────────────────────────────────────────────────

describe('概览：最近一次分配', () => {
  const breakdown = () => (document.body.querySelector('[data-testid="allocation-breakdown"]') as HTMLElement).textContent

  it('摘要一行带来源指定的笔数；未分出的列表里来源指定的项有「来源指定」小标', async () => {
    overviewApi.getOverview.mockResolvedValue(overview({
      last_allocation: allocation({ considered: 12, assigned: 9, coverage: 3, fill: 4, designated: 2, unassigned: 3 }, [
        { order_id: 31, upstream_order_no: 'PO-DEMO-0031', reason: '金额 $64.00 不在任何启用账号的区间内' },
        { order_id: 32, upstream_order_no: 'PO-DEMO-0032', reason: '来源指定的账号 环境-DEMO-A 在 2026-11-18 的额度已满（上限 20）；有空位后的分配会再安排', designated: true },
        { order_id: 33, upstream_order_no: 'PO-DEMO-0033', reason: '匹配账号当天额度已满', designated: false },
      ]),
    }))
    await mountWith(OverviewView)
    expect(breakdown()).toBe('来源指定 2 笔 · 保底 3 笔（每个账号当天首单）· 按优先级填充 4 笔')
    const items = [...document.body.querySelectorAll('li')].filter((li) => (li.textContent ?? '').includes('PO-DEMO-00'))
    expect(items).toHaveLength(3)
    const tagOf = (li: Element) => [...li.querySelectorAll('.el-tag')].map((t) => (t.textContent ?? '').trim())
    expect(tagOf(items[0])).toEqual([])
    expect(tagOf(items[1])).toEqual(['来源指定'])
    expect(tagOf(items[2])).toEqual([])
    expect(items[1].textContent).toContain('额度已满（上限 20）')
  })

  it('升级前的分配记录（没有 designated、未分出项没有标记）：来源指定按 0 笔，不出现「—」，没有小标', async () => {
    overviewApi.getOverview.mockResolvedValue(overview({
      last_allocation: allocation({ considered: 9, assigned: 7, coverage: 3, fill: 4, unassigned: 2 }, [
        { order_id: 31, upstream_order_no: 'PO-DEMO-0031', reason: '匹配账号当天额度已满' },
      ]),
    }))
    await mountWith(OverviewView)
    expect(breakdown()).toBe('来源指定 0 笔 · 保底 3 笔（每个账号当天首单）· 按优先级填充 4 笔')
    const item = [...document.body.querySelectorAll('li')].find((li) => (li.textContent ?? '').includes('PO-DEMO-0031'))!
    expect(item.querySelectorAll('.el-tag')).toHaveLength(0)
  })

  it('没有分配记录时不显示摘要行', async () => {
    overviewApi.getOverview.mockResolvedValue(overview())
    await mountWith(OverviewView)
    expect(document.body.querySelector('[data-testid="allocation-breakdown"]')).toBeNull()
    expect(bodyText()).toContain('还没有分配记录')
  })

  it('重新分配的确认文案写明三种任务各怎么处理', async () => {
    overviewApi.getOverview.mockResolvedValue(overview())
    allocationApi.runAllocation.mockResolvedValue(allocation({ considered: 3, assigned: 2, coverage: 1, fill: 0, designated: 1, unassigned: 1 }))
    await mountWith(OverviewView)
    buttons('重新分配')[0].click()
    await settle()
    expect(confirmMessage()).toBe('确定要重新分配吗？来源指定了买家号的任务按指定分配，其余自动分配的任务按当前账号配置重算，人工改派的任务保持不动。')
    expect(allocationApi.runAllocation).toHaveBeenCalledTimes(1)
    expect(feedback.notifySuccess).toHaveBeenCalledWith('重新分配完成', '参与 3 笔，分出 2 笔，未分出 1 笔。')
  })
})

describe('待采购页', () => {
  it('「待分配」的空状态说明含来源指定的买家号暂时接不了', async () => {
    overviewApi.getOverview.mockResolvedValue(overview({ counts: { queued: 0 } }))
    await mountWith(OrdersView, {}, '/orders?view=unassigned')
    expect(ordersApi.listOrders).toHaveBeenCalledWith(expect.objectContaining({ view: 'unassigned' }))
    expect(bodyText()).toContain('没有待分配的任务')
    expect(bodyText()).toContain('分不出去的任务会写明原因：金额不在任何启用账号的区间内、匹配账号当天额度已满，或来源指定的买家号暂时接不了。')
  })

  it('重新分配的确认文案与概览相同；取消就不分配', async () => {
    overviewApi.getOverview.mockResolvedValue(overview())
    feedback.confirmAction.mockResolvedValue(false)
    await mountWith(OrdersView, {}, '/orders?view=today')
    buttons('重新分配')[0].click()
    await settle()
    expect(confirmMessage()).toBe(REALLOCATE_CONFIRM_TEXT)
    expect(allocationApi.runAllocation).not.toHaveBeenCalled()
  })

  it('待分配列表里来源指定而没分出去的任务：说明列写服务端给的原因；这一页不显示账号列（都还没分配），宽度让给说明', async () => {
    overviewApi.getOverview.mockResolvedValue(overview())
    ordersApi.listOrders.mockResolvedValue({ total: 1, rows: [stuck(12)] })
    const w = await mountWith(OrdersView, {}, '/orders?view=unassigned')
    const heads = w.findAll('.el-table__header th').map((th) => th.text())
    expect(heads).toContain('说明')
    expect(heads).not.toContain('账号')
    const row = w.findAll('.el-table__body tr')[0].text()
    expect(row).toContain('待分配')
    expect(row).not.toContain('未分配')
    expect(row).toContain('来源指定的买家号 ID A2TEST0007（买家号「环境-DEMO-C」）还没有登记为账号')
  })

  it('其他标签页照旧显示账号列', async () => {
    overviewApi.getOverview.mockResolvedValue(overview())
    ordersApi.listOrders.mockResolvedValue({ total: 0, rows: [] })
    const w = await mountWith(OrdersView, {}, '/orders?view=today')
    expect(w.findAll('.el-table__header th').map((th) => th.text())).toContain('账号')
  })
})
