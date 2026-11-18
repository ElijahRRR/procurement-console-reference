/**
 * 「账号与浏览器」页的接线（执行入口改造后）：
 * - 账号页签顶部的「待登记的买家号」：登记（账号对话框预填 ID 与名称，走新建接口）、填到已有账号（PATCH）、忽略 / 恢复、自动刷新；
 * - 账号表的「买家号 ID」「所在浏览器」两列；账号对话框没有原来的指定执行环境一项；
 * - 浏览器页签：只读列表、各登录状态的文字、改名、移除、显示已移除、空状态；
 * - 概览的待登记提示与「去登记」；设置页显示工作区 ID。
 * 接口与提示框打桩；买家号 ID 全部虚构。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus, { ElDropdown } from 'element-plus'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { defineComponent, h, type Component, type DefineComponent } from 'vue'
import type { Account, Device, DiscoveredBuyer, Overview } from '@/api/types'

const accountsApi = vi.hoisted(() => ({
  listAccounts: vi.fn(),
  getAccount: vi.fn(),
  resumeAccount: vi.fn(),
  pauseAccount: vi.fn(),
  updateAccount: vi.fn(),
  createAccount: vi.fn(),
  listDiscovered: vi.fn(),
  ignoreDiscovered: vi.fn(),
  unignoreDiscovered: vi.fn(),
}))
vi.mock('@/api/accounts', () => accountsApi)

const devicesApi = vi.hoisted(() => ({ listDevices: vi.fn(), renameDevice: vi.fn(), removeDevice: vi.fn() }))
vi.mock('@/api/devices', () => devicesApi)

const settingsApi = vi.hoisted(() => ({ getSettings: vi.fn(), updateSettings: vi.fn() }))
vi.mock('@/api/settings', () => settingsApi)

const overviewApi = vi.hoisted(() => ({ getOverview: vi.fn() }))
vi.mock('@/api/overview', () => overviewApi)

const ordersApi = vi.hoisted(() => ({ listOrders: vi.fn(), getOrder: vi.fn() }))
vi.mock('@/api/orders', () => ordersApi)

const clipboard = vi.hoisted(() => ({ copyText: vi.fn(async () => true) }))
vi.mock('@/utils/clipboard', () => clipboard)

const feedback = vi.hoisted(() => ({
  confirmAction: vi.fn(async () => true),
  promptText: vi.fn(async (): Promise<string | null> => 'Mac-AdsPower-DEMO-09'),
  notifyFailure: vi.fn(),
  notifySuccess: vi.fn(),
  notifySummary: vi.fn(),
  runDownload: vi.fn(),
  toast: vi.fn(),
}))
vi.mock('@/utils/feedback', () => feedback)

import { ApiError } from '@/api/client'
import AccountsPanel from '@/components/accounts/AccountsPanel.vue'
import BrowsersPanel from '@/components/accounts/BrowsersPanel.vue'
import AccountsView from '@/views/AccountsView.vue'
import OverviewView from '@/views/OverviewView.vue'
import SettingsView from '@/views/SettingsView.vue'
import { session } from '@/stores/session'
import { IGNORE_CONFIRM_TEXT, customerIdProblem } from '@/utils/accounts'

function makeRouter(): Router {
  const Blank = defineComponent({ render: () => h('div') })
  return createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: Blank }] })
}

const mounted: VueWrapper[] = []
let router: Router
async function mountWith(comp: Component, props: Record<string, unknown> = {}, path = '/') {
  router = makeRouter()
  await router.push(path)
  await router.isReady()
  const w = mount(comp as DefineComponent, { props, attachTo: document.body, global: { plugins: [ElementPlus, router] } })
  mounted.push(w)
  await flushPromises()
  return w
}

/** 输入：按钮文字、查找范围 → 输出：文字完全相同的按钮（含传送到 body 的弹窗） */
function buttons(text: string, root: ParentNode = document.body): HTMLButtonElement[] {
  return [...root.querySelectorAll('button')].filter((b) => (b.textContent ?? '').trim() === text) as HTMLButtonElement[]
}
/** 输入：按钮文字 → 输出：表格数据行里的按钮（el-table 还会在隐藏的列定义区用空行渲染一遍插槽，不算在内） */
const rowButtons = (text: string) => buttons(text, document.body.querySelector('.el-table__body') ?? document.body)
const bodyText = () => document.body.textContent ?? ''
const openDialog = () => [...document.body.querySelectorAll('.el-dialog')].find((d) => (d as HTMLElement).offsetParent !== null || d.querySelector('form')) as HTMLElement | undefined
async function settle() {
  await flushPromises()
  await new Promise((r) => setTimeout(r, 0))
  await flushPromises()
}

function account(extra: Partial<Account> = {}): Account {
  return {
    id: 3, name: '环境-DEMO-01', amazon_customer_id: 'A1DEMO00001', site: 'US', min_amount: '20.00', max_amount: '30.00', daily_limit: 20,
    priority: 5, max_delivery_days: null, effective_max_delivery_days: 10, purchase_enabled: true, sync_enabled: true,
    execution_paused: false, pause_reason: null, paused_at: null, pause_cause: null, open_result_unknown: 0,
    purchase_mode: 'manual', schedule_times: [], expected_card_last4: null, notes: null,
    quota_today: { limit: 20, used: 3, planned: 5, available: 12 },
    browsers: [{ id: 5, name: 'Mac-AdsPower-DEMO-01', online: true, last_seen_at: '2026-11-18T07:59:00+00:00' }],
    active_run: null, created_at: '2026-11-01T00:00:00+00:00', updated_at: '2026-11-05T01:05:00+00:00',
    ...extra,
  }
}
const noIdAccount = () => account({ id: 8, name: '环境-DEMO-04', amazon_customer_id: null, browsers: [] })

function buyer(customerId: string, extra: Partial<DiscoveredBuyer> = {}): DiscoveredBuyer {
  return {
    customer_id: customerId, ignored: false, name_hint: null, last_reported_at: new Date(Date.now() - 60_000).toISOString(),
    browsers: [{ id: 10, name: '浏览器 10', online: true, last_seen_at: new Date().toISOString() }],
    ...extra,
  }
}

function device(extra: Partial<Device> = {}): Device {
  return {
    id: 5, name: 'Mac-AdsPower-DEMO-01', ext_version: '0.2.0', online: true, last_seen_at: new Date(Date.now() - 30_000).toISOString(),
    created_at: '2026-11-10T00:00:00+00:00', hidden_at: null,
    login: { state: 'ready', customer_id: 'A1DEMO00001', at: new Date(Date.now() - 60_000).toISOString(), stale: false },
    account: { id: 3, name: '环境-DEMO-01' }, active_run: null,
    ...extra,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  feedback.confirmAction.mockResolvedValue(true)
  feedback.promptText.mockResolvedValue('Mac-AdsPower-DEMO-09')
  clipboard.copyText.mockResolvedValue(true)
  settingsApi.getSettings.mockResolvedValue({ default_max_delivery_days: 10, simulation_only: true, logistics_sync_time: '08:30', real_purchase_available: false })
  accountsApi.listAccounts.mockResolvedValue({ total: 2, rows: [account(), noIdAccount()] })
  accountsApi.listDiscovered.mockResolvedValue([])
  devicesApi.listDevices.mockResolvedValue({ total: 0, rows: [] })
})
afterEach(() => {
  while (mounted.length) mounted.pop()!.unmount()
  document.body.innerHTML = ''
  vi.useRealTimers()
  session.me = null
})

// ── 账号页签：待登记的买家号 ──────────────────────────────────────────────────

describe('账号页签：待登记的买家号', () => {
  it('没有条目时不显示这个区；读列表时带上已忽略的', async () => {
    await mountWith(AccountsPanel)
    expect(accountsApi.listDiscovered).toHaveBeenCalledWith(true)
    expect(document.body.querySelector('[data-testid="discovered-buyers"]')).toBeNull()
    expect(bodyText()).not.toContain('待登记的买家号')
  })

  it('每行显示完整的买家号 ID、所在浏览器、最近上报时间和三个动作；可以复制 ID', async () => {
    accountsApi.listDiscovered.mockResolvedValue([buyer('A2TEST00005', { name_hint: '环境-DEMO-05' })])
    await mountWith(AccountsPanel)
    const zone = document.body.querySelector('[data-testid="discovered-buyers"]') as HTMLElement
    expect(zone.textContent).toContain('待登记的买家号')
    expect(zone.textContent).toContain('A2TEST00005')
    expect(zone.textContent).toContain('浏览器 10')
    expect(zone.textContent).toContain('1 分钟前')
    expect(zone.textContent).toContain('来源里这个买家号叫「环境-DEMO-05」')
    expect(buttons('登记', zone)).toHaveLength(1)
    expect(buttons('填到已有账号', zone)).toHaveLength(1)
    expect(buttons('忽略', zone)).toHaveLength(1)
    ;(zone.querySelector('button[aria-label="复制买家号 ID"]') as HTMLButtonElement).click()
    await flushPromises()
    expect(clipboard.copyText).toHaveBeenCalledWith('A2TEST00005')
    expect(feedback.toast).toHaveBeenCalledWith('买家号 ID 已复制')
  })

  it('登记：打开账号对话框，ID 已填（不可改）、名称用来源里的名称预填；提交走新建接口，之后重读账号与待登记列表', async () => {
    accountsApi.listDiscovered.mockResolvedValue([buyer('A2TEST00005', { name_hint: '环境-DEMO-05' })])
    accountsApi.createAccount.mockResolvedValue(account({ id: 99, name: '环境-DEMO-05', amazon_customer_id: 'A2TEST00005' }))
    const w = await mountWith(AccountsPanel)
    buttons('登记')[0].click()
    await settle()
    const dialog = openDialog()!
    expect(dialog.textContent).toContain('登记买家号 · A2TEST00005')
    const idInput = dialog.querySelector('input[aria-label="买家号 ID"]') as HTMLInputElement
    expect(idInput.value).toBe('A2TEST00005')
    expect(idInput.readOnly).toBe(true)
    expect([...dialog.querySelectorAll('input')].map((i) => i.value)).toContain('环境-DEMO-05')
    accountsApi.listAccounts.mockClear()
    accountsApi.listDiscovered.mockClear()
    buttons('登记', dialog)[0].click()
    await settle()
    expect(accountsApi.createAccount).toHaveBeenCalledTimes(1)
    const body = accountsApi.createAccount.mock.calls[0][0] as Record<string, unknown>
    expect(body).toMatchObject({ name: '环境-DEMO-05', amazon_customer_id: 'A2TEST00005', purchase_enabled: true, daily_limit: 20, min_amount: '0.00' })
    expect(body).not.toHaveProperty('default_device_id')
    expect(accountsApi.updateAccount).not.toHaveBeenCalled()
    expect(feedback.notifySuccess).toHaveBeenCalledWith('买家号已登记', expect.any(String))
    expect(accountsApi.listAccounts).toHaveBeenCalled()
    expect(accountsApi.listDiscovered).toHaveBeenCalled()
    expect(w.emitted('changed')).toHaveLength(1)
  })

  it('登记失败：提示「登记买家号失败：原因」，对话框不关', async () => {
    accountsApi.listDiscovered.mockResolvedValue([buyer('A2TEST00005', { name_hint: '环境-DEMO-01' })])
    const e = new ApiError('CONFLICT', '账号名称「环境-DEMO-01」已存在', 409)
    accountsApi.createAccount.mockRejectedValue(e)
    await mountWith(AccountsPanel)
    buttons('登记')[0].click()
    await settle()
    buttons('登记', openDialog()!)[0].click()
    await settle()
    expect(feedback.notifyFailure).toHaveBeenCalledWith('登记买家号', e)
    expect(openDialog()!.textContent).toContain('登记买家号 · A2TEST00005')
  })

  it('填到已有账号：只列还没登记 ID 的账号；确认文案写明 ID 与账号，确认后 PATCH 这个账号', async () => {
    accountsApi.listDiscovered.mockResolvedValue([buyer('A2TEST00004', { name_hint: '环境-DEMO-04' })])
    accountsApi.updateAccount.mockResolvedValue(account({ id: 8, name: '环境-DEMO-04', amazon_customer_id: 'A2TEST00004' }))
    const w = await mountWith(AccountsPanel)
    expect(bodyText()).toContain('已有同名账号还没登记买家号 ID，可以直接填到它上面')
    const dropdown = w.findComponent(ElDropdown)
    expect(dropdown.props('disabled')).toBe(false)
    dropdown.vm.$emit('command', 8)
    await settle()
    expect(feedback.confirmAction).toHaveBeenCalledWith(expect.objectContaining({ message: '确定要把买家号 ID A2TEST00004 登记到「环境-DEMO-04」吗？' }))
    expect(accountsApi.updateAccount).toHaveBeenCalledWith(8, { amazon_customer_id: 'A2TEST00004' })
    expect(accountsApi.createAccount).not.toHaveBeenCalled()
    expect(w.emitted('changed')).toHaveLength(1)
    // 已经登记了 ID 的账号不是候选：选它不发请求
    accountsApi.updateAccount.mockClear()
    dropdown.vm.$emit('command', 12345)
    await settle()
    expect(accountsApi.updateAccount).not.toHaveBeenCalled()
  })

  it('填到已有账号：取消确认不发请求；服务端拒绝时提示「登记买家号失败：原因」', async () => {
    accountsApi.listDiscovered.mockResolvedValue([buyer('A2TEST00004')])
    const w = await mountWith(AccountsPanel)
    feedback.confirmAction.mockResolvedValueOnce(false)
    w.findComponent(ElDropdown).vm.$emit('command', 8)
    await settle()
    expect(accountsApi.updateAccount).not.toHaveBeenCalled()
    const e = new ApiError('CONFLICT', '账号正在执行或有结果待核实的采购，处理完后才能修改买家号 ID', 409)
    accountsApi.updateAccount.mockRejectedValue(e)
    w.findComponent(ElDropdown).vm.$emit('command', 8)
    await settle()
    expect(feedback.notifyFailure).toHaveBeenCalledWith('登记买家号', e)
  })

  it('没有还没登记 ID 的账号时「填到已有账号」不可点', async () => {
    accountsApi.listAccounts.mockResolvedValue({ total: 1, rows: [account()] })
    accountsApi.listDiscovered.mockResolvedValue([buyer('A2TEST00004')])
    const w = await mountWith(AccountsPanel)
    expect(w.findComponent(ElDropdown).props('disabled')).toBe(true)
    expect(buttons('填到已有账号')[0].disabled).toBe(true)
  })

  it('忽略：按约定的文案确认后调接口并重读；已忽略的可展开、每行可恢复', async () => {
    accountsApi.listDiscovered.mockResolvedValue([buyer('A2TEST00005'), buyer('A2TEST00099', { ignored: true })])
    accountsApi.ignoreDiscovered.mockResolvedValue({ ok: true })
    accountsApi.unignoreDiscovered.mockResolvedValue({ ok: true })
    await mountWith(AccountsPanel)
    expect(IGNORE_CONFIRM_TEXT.startsWith('确定要忽略这个买家号吗？')).toBe(true)
    buttons('忽略')[0].click()
    await settle()
    expect(feedback.confirmAction).toHaveBeenCalledWith(expect.objectContaining({ message: IGNORE_CONFIRM_TEXT }))
    expect(accountsApi.ignoreDiscovered).toHaveBeenCalledWith('A2TEST00005')
    expect(accountsApi.listDiscovered).toHaveBeenCalledTimes(2)
    // 已忽略的缺省收起
    expect(bodyText()).toContain('已忽略 1 个')
    expect(bodyText()).not.toContain('A2TEST00099')
    const toggle = [...document.body.querySelectorAll('button')].find((b) => (b.textContent ?? '').includes('已忽略 1 个')) as HTMLButtonElement
    toggle.click()
    await flushPromises()
    expect(bodyText()).toContain('A2TEST00099')
    buttons('恢复')[0].click()
    await settle()
    expect(accountsApi.unignoreDiscovered).toHaveBeenCalledWith('A2TEST00099')
    expect(accountsApi.listDiscovered).toHaveBeenCalledTimes(3)
  })

  it('只有已忽略的买家号时也能展开恢复（不显示「待登记的买家号」标题）', async () => {
    accountsApi.listDiscovered.mockResolvedValue([buyer('A2TEST00099', { ignored: true })])
    await mountWith(AccountsPanel)
    expect(bodyText()).toContain('已忽略 1 个')
    expect(bodyText()).not.toContain('待登记的买家号')
    expect(buttons('登记')).toHaveLength(0)
  })

  it('页面可见时每 10 秒自动刷新；页面不可见时不刷新；卸载后停止', async () => {
    vi.useFakeTimers()
    const state = vi.spyOn(document, 'visibilityState', 'get')
    state.mockReturnValue('visible')
    await mountWith(AccountsPanel)
    expect(accountsApi.listDiscovered).toHaveBeenCalledTimes(1)
    accountsApi.listDiscovered.mockResolvedValue([buyer('A2TEST00005')])
    await vi.advanceTimersByTimeAsync(10_000)
    expect(accountsApi.listDiscovered).toHaveBeenCalledTimes(2)
    expect(bodyText()).toContain('A2TEST00005')
    state.mockReturnValue('hidden')
    await vi.advanceTimersByTimeAsync(30_000)
    expect(accountsApi.listDiscovered).toHaveBeenCalledTimes(2)
    // 回到前台立刻补一次
    state.mockReturnValue('visible')
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(0)
    expect(accountsApi.listDiscovered).toHaveBeenCalledTimes(3)
    mounted.pop()!.unmount()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(accountsApi.listDiscovered).toHaveBeenCalledTimes(3)
    state.mockRestore()
  })

  it('所在页签没有激活时不自动刷新；重新激活时立刻补一次', async () => {
    vi.useFakeTimers()
    const w = await mountWith(AccountsPanel, { active: false })
    expect(accountsApi.listDiscovered).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(accountsApi.listDiscovered).toHaveBeenCalledTimes(1)
    await w.setProps({ active: true } as Record<string, unknown>)
    await vi.advanceTimersByTimeAsync(0)
    expect(accountsApi.listDiscovered).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(accountsApi.listDiscovered).toHaveBeenCalledTimes(3)
  })

  it('动作之后的读取让更早发出、还没回来的自动刷新作废（忽略掉的买家号不会被旧结果带回来）', async () => {
    vi.useFakeTimers()
    accountsApi.listDiscovered.mockResolvedValue([buyer('A2TEST00005')])
    accountsApi.ignoreDiscovered.mockResolvedValue({ ok: true })
    await mountWith(AccountsPanel)
    // 一次很慢的自动刷新：带着忽略之前的旧结果
    let finishSlow: (v: DiscoveredBuyer[]) => void = () => {}
    accountsApi.listDiscovered.mockReturnValueOnce(new Promise<DiscoveredBuyer[]>((r) => (finishSlow = r)))
    await vi.advanceTimersByTimeAsync(10_000)
    accountsApi.listDiscovered.mockResolvedValue([])
    buttons('忽略')[0].click()
    await vi.advanceTimersByTimeAsync(0)
    await flushPromises()
    expect(accountsApi.ignoreDiscovered).toHaveBeenCalledWith('A2TEST00005')
    expect(bodyText()).not.toContain('A2TEST00005')
    finishSlow([buyer('A2TEST00005')])
    await vi.advanceTimersByTimeAsync(0)
    await flushPromises()
    expect(bodyText()).not.toContain('A2TEST00005')
  })

  it('自动刷新失败不弹通知，保留上一次读到的内容并在本区写明', async () => {
    vi.useFakeTimers()
    accountsApi.listDiscovered.mockResolvedValue([buyer('A2TEST00005')])
    await mountWith(AccountsPanel)
    accountsApi.listDiscovered.mockRejectedValue(new ApiError('NETWORK', '无法连接服务器，请检查服务端是否在运行', 0))
    await vi.advanceTimersByTimeAsync(10_000)
    expect(feedback.notifyFailure).not.toHaveBeenCalled()
    expect(bodyText()).toContain('A2TEST00005')
    expect(bodyText()).toContain('读取待登记的买家号失败：无法连接服务器，请检查服务端是否在运行')
  })
})

// ── 账号表与账号对话框 ────────────────────────────────────────────────────────

describe('账号表与账号对话框', () => {
  it('「买家号 ID」列：登记了显示 ID，没登记显示「未登记」；「所在浏览器」列：名称与在线状态，没有显示「—」', async () => {
    accountsApi.listAccounts.mockResolvedValue({
      total: 3,
      rows: [
        account(),
        account({ id: 4, name: '环境-DEMO-02', amazon_customer_id: 'A1DEMO00002', browsers: [{ id: 6, name: 'Mac-Chrome-DEMO-02', online: false, last_seen_at: new Date(Date.now() - 5 * 3600_000).toISOString() }] }),
        noIdAccount(),
      ],
    })
    const w = await mountWith(AccountsPanel)
    const headers = w.findAll('th').map((t) => t.text())
    expect(headers).toContain('买家号 ID')
    expect(headers).toContain('所在浏览器')
    expect(headers.join(' ')).not.toMatch(/设备|绑定/)
    const rows = w.findAll('.el-table__body tr').map((r) => r.text())
    expect(rows[0]).toContain('A1DEMO00001')
    expect(rows[0]).toContain('Mac-AdsPower-DEMO-01')
    expect(rows[1]).toContain('Mac-Chrome-DEMO-02')
    expect(rows[1]).toContain('离线 · 5 小时前')
    expect(rows[2]).toContain('未登记')
    expect(rows[2]).toContain('—')
    expect(bodyText()).toContain('1 个还没登记买家号 ID')
  })

  it('新建账号：ID 说明文字按约定；没有指定执行环境的选项；买家号 ID 留空时提交 null', async () => {
    accountsApi.createAccount.mockResolvedValue(account({ id: 99, name: '环境-DEMO-09', amazon_customer_id: null }))
    await mountWith(AccountsPanel)
    buttons('新建账号')[0].click()
    await settle()
    const dialog = openDialog()!
    expect(dialog.textContent).toContain('新建买家账号')
    expect(dialog.textContent).toContain('亚马逊 customerId。留空也可以：登录着这个买家号的浏览器装上插件后，会出现在上面的待登记列表里。')
    expect(dialog.textContent).not.toMatch(/默认设备|绑定|配对/)
    expect((dialog.querySelector('input[aria-label="买家号 ID"]') as HTMLInputElement).readOnly).toBe(false)
    const name = dialog.querySelector('input') as HTMLInputElement
    name.value = '环境-DEMO-09'
    name.dispatchEvent(new Event('input'))
    await flushPromises()
    buttons('创建', dialog)[0].click()
    await settle()
    const body = accountsApi.createAccount.mock.calls[0][0] as Record<string, unknown>
    expect(body).toMatchObject({ name: '环境-DEMO-09', amazon_customer_id: null })
    expect(body).not.toHaveProperty('default_device_id')
  })

  it('编辑账号：没改买家号 ID 时请求里不带它（账号正在执行时别的字段照样能保存）；改了才带，并转成大写', async () => {
    accountsApi.updateAccount.mockResolvedValue(account())
    await mountWith(AccountsPanel)
    ;(document.body.querySelector('.el-table__body button[aria-label="编辑账号"]') as HTMLButtonElement).click()
    await settle()
    let dialog = openDialog()!
    buttons('保存', dialog)[0].click()
    await settle()
    expect(accountsApi.updateAccount).toHaveBeenCalledTimes(1)
    expect(accountsApi.updateAccount.mock.calls[0][0]).toBe(3)
    expect(accountsApi.updateAccount.mock.calls[0][1]).not.toHaveProperty('amazon_customer_id')
    expect(accountsApi.updateAccount.mock.calls[0][1]).not.toHaveProperty('default_device_id')

    ;(document.body.querySelector('.el-table__body button[aria-label="编辑账号"]') as HTMLButtonElement).click()
    await settle()
    dialog = openDialog()!
    const idInput = dialog.querySelector('input[aria-label="买家号 ID"]') as HTMLInputElement
    idInput.value = ' a1demo00009 '
    idInput.dispatchEvent(new Event('input'))
    await flushPromises()
    expect(dialog.textContent).toContain('修改买家号 ID 后，这个账号的任务改由登录着新买家号的浏览器执行')
    buttons('保存', dialog)[0].click()
    await settle()
    expect(accountsApi.updateAccount).toHaveBeenCalledTimes(2)
    expect(accountsApi.updateAccount.mock.calls[1][1]).toMatchObject({ amazon_customer_id: 'A1DEMO00009' })
  })

  it('买家号 ID 的表单校验：留空可以，形状不对给出说明（校验规则本身；el-form 的规则在这个测试环境里不执行）', () => {
    expect(customerIdProblem('')).toBe('')
    expect(customerIdProblem('   ')).toBe('')
    expect(customerIdProblem(null)).toBe('')
    expect(customerIdProblem(' a1demo00009 ')).toBe('')
    expect(customerIdProblem('B123')).toBe('买家号 ID 以 A 开头，后面至少 8 位字母或数字')
    expect(customerIdProblem('A1DEMO0')).toBe('买家号 ID 以 A 开头，后面至少 8 位字母或数字')
    expect(customerIdProblem('A1DEMO-0000')).toBe('买家号 ID 以 A 开头，后面至少 8 位字母或数字')
  })
})

// ── 浏览器页签 ────────────────────────────────────────────────────────────────

describe('浏览器页签', () => {
  const all: Device[] = [
    device({ active_run: { id: 9, account_id: 3, started_at: new Date(Date.now() - 6 * 60_000).toISOString(), simulated: true } }),
    device({ id: 7, name: 'Win-AdsPower-DEMO-03', login: { state: 'ready', customer_id: 'A1DEMO00003', at: new Date(Date.now() - 120_000).toISOString(), stale: false }, account: { id: 7, name: '环境-DEMO-03' } }),
    device({ id: 9, name: '浏览器 9', login: { state: 'signed_out', customer_id: null, at: new Date().toISOString(), stale: false }, account: null }),
    device({ id: 10, name: '浏览器 10', login: { state: 'unregistered', customer_id: 'A2TEST00005', at: new Date().toISOString(), stale: false }, account: null }),
    device({ id: 12, name: '浏览器 12', online: false, login: { state: 'ignored', customer_id: 'A2TEST00099', at: new Date(Date.now() - 5 * 3600_000).toISOString(), stale: true }, account: null }),
    device({ id: 13, name: '浏览器 13', login: { state: 'no_id', customer_id: null, at: new Date().toISOString(), stale: false }, account: null }),
    device({ id: 14, name: '浏览器 14', login: { state: 'conflict', customer_id: null, at: new Date().toISOString(), stale: false }, account: null }),
    device({ id: 15, name: '浏览器 15', ext_version: null, online: false, last_seen_at: null, login: { state: 'unknown', customer_id: null, at: null, stale: true }, account: null }),
  ]

  beforeEach(() => {
    devicesApi.listDevices.mockResolvedValue({ total: all.length, rows: all })
    accountsApi.listAccounts.mockResolvedValue({
      total: 2,
      rows: [account(), account({ id: 7, name: '环境-DEMO-03', amazon_customer_id: 'A1DEMO00003', execution_paused: true, pause_reason: '需要人工登录', pause_cause: 'manual' })],
    })
  })

  it('只读列表：各登录状态的文字、正在执行、在线计数；没有配对码、绑定、采用 ID 这些入口', async () => {
    const w = await mountWith(BrowsersPanel)
    expect(devicesApi.listDevices).toHaveBeenCalledWith({ include_hidden: false })
    expect(w.findAll('th').map((t) => t.text())).toEqual(['名称', '插件版本', '在线', '当前登录', '正在执行', '最近联系', '操作'])
    const rows = w.findAll('.el-table__body tr').map((r) => r.text())
    expect(rows[0]).toContain('环境-DEMO-01')
    expect(rows[0]).toContain('A1DEMO00001')
    expect(rows[0]).toContain('执行中（模拟）')
    expect(rows[0]).toContain('环境-DEMO-01 · 6 分钟前开始')
    expect(rows[1]).toContain('环境-DEMO-03（已暂停）')
    expect(rows[2]).toContain('未登录亚马逊')
    expect(rows[3]).toContain('未登记的买家号')
    expect(rows[3]).toContain('A2TEST00005')
    expect(rows[4]).toContain('已忽略的买家号')
    expect(rows[4]).toContain('A2TEST00099')
    expect(rows[4]).toContain('5 小时前上报，已过期')
    expect(rows[5]).toContain('已登录，没读到买家号 ID')
    expect(rows[6]).toContain('登录状态不一致')
    expect(rows[7]).toContain('还没有上报')
    expect(rows[7]).toContain('从未联系')
    expect(bodyText()).toContain('6/8 个在线')
    expect(bodyText()).not.toMatch(/配对|绑定|采用此 ID|身份一致|设备/)
    expect(w.findAll('select, .el-select')).toHaveLength(0)
    // 每行只有改名与移除；只有未登记的那一行多一个「登记」
    expect(rowButtons('改名')).toHaveLength(8)
    expect(rowButtons('移除')).toHaveLength(8)
    expect(rowButtons('登记')).toHaveLength(1)
  })

  it('未登记的买家号旁的「登记」把 ID 交给上层', async () => {
    const w = await mountWith(BrowsersPanel)
    rowButtons('登记')[0].click()
    await flushPromises()
    expect(w.emitted('register')).toEqual([['A2TEST00005']])
  })

  it('改名：只发新名称', async () => {
    devicesApi.renameDevice.mockResolvedValue(device({ name: 'Mac-AdsPower-DEMO-09' }))
    const w = await mountWith(BrowsersPanel)
    rowButtons('改名')[0].click()
    await settle()
    expect(feedback.promptText).toHaveBeenCalledWith(expect.objectContaining({ title: '修改浏览器名称' }))
    expect(devicesApi.renameDevice).toHaveBeenCalledWith(5, 'Mac-AdsPower-DEMO-09')
    expect(feedback.notifySuccess).toHaveBeenCalledWith('浏览器名称已保存')
    expect(w.emitted('changed')).toHaveLength(1)
    // 取消或名称没变不发请求
    devicesApi.renameDevice.mockClear()
    feedback.promptText.mockResolvedValueOnce(null)
    rowButtons('改名')[0].click()
    await settle()
    feedback.promptText.mockResolvedValueOnce('Mac-AdsPower-DEMO-01')
    rowButtons('改名')[0].click()
    await settle()
    expect(devicesApi.renameDevice).not.toHaveBeenCalled()
  })

  it('移除：确认文案说明只是清理列表、会自动回来；确认后调移除接口并重读', async () => {
    devicesApi.removeDevice.mockResolvedValue({ ok: true })
    const w = await mountWith(BrowsersPanel)
    rowButtons('移除')[2].click()
    await settle()
    const opts = feedback.confirmAction.mock.calls[0] as unknown as [{ message: string; confirmText: string }]
    expect(opts[0].message).toMatch(/^确定要移除「浏览器 9」吗？/)
    expect(opts[0].message).toContain('再次联系服务端时会自动回到列表')
    expect(opts[0].message).toContain('请暂停对应的账号')
    expect(opts[0].confirmText).toBe('移除')
    expect(devicesApi.removeDevice).toHaveBeenCalledWith(9)
    expect(feedback.notifySuccess).toHaveBeenCalledWith('已移除浏览器')
    expect(devicesApi.listDevices).toHaveBeenCalledTimes(2)
    expect(w.emitted('changed')).toHaveLength(1)
  })

  it('正在执行的浏览器不能移除（409）：提示「移除浏览器失败：原因」', async () => {
    const e = new ApiError('CONFLICT', '这个浏览器正在执行，结束后才能移除', 409)
    devicesApi.removeDevice.mockRejectedValue(e)
    await mountWith(BrowsersPanel)
    rowButtons('移除')[0].click()
    await settle()
    expect(feedback.notifyFailure).toHaveBeenCalledWith('移除浏览器', e)
    expect(feedback.notifySuccess).not.toHaveBeenCalled()
  })

  it('显示已移除：重新读列表并带上已移除的；已移除的行有标记、没有操作', async () => {
    const w = await mountWith(BrowsersPanel)
    devicesApi.listDevices.mockResolvedValue({ total: 2, rows: [all[2], device({ id: 8, name: 'Mac-Chrome-DEMO-旧环境', online: false, hidden_at: '2026-11-10T00:00:00+00:00', login: { state: 'unknown', customer_id: null, at: null, stale: true }, account: null })] })
    await w.find('.el-switch').trigger('click')
    await settle()
    expect(devicesApi.listDevices).toHaveBeenLastCalledWith({ include_hidden: true })
    const rows = w.findAll('.el-table__body tr').map((r) => r.text())
    expect(rows[1]).toContain('Mac-Chrome-DEMO-旧环境')
    expect(rows[1]).toContain('已移除')
    expect(rowButtons('移除')).toHaveLength(1)
    expect(bodyText()).toContain('显示已移除')
  })

  it('页签激活时每 10 秒自动刷新（不转圈、失败不弹通知）；没激活时不刷新', async () => {
    vi.useFakeTimers()
    const w = await mountWith(BrowsersPanel, { active: true })
    expect(devicesApi.listDevices).toHaveBeenCalledTimes(1)
    devicesApi.listDevices.mockRejectedValueOnce(new ApiError('NETWORK', '无法连接服务器，请检查服务端是否在运行', 0))
    await vi.advanceTimersByTimeAsync(10_000)
    expect(devicesApi.listDevices).toHaveBeenCalledTimes(2)
    expect(feedback.notifyFailure).not.toHaveBeenCalled()
    expect(w.findAll('.el-table__body tr')).toHaveLength(8)
    await w.setProps({ active: false } as Record<string, unknown>)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(devicesApi.listDevices).toHaveBeenCalledTimes(2)
  })

  it('空状态：说明浏览器会自动出现，不给任何手工步骤', async () => {
    devicesApi.listDevices.mockResolvedValue({ total: 0, rows: [] })
    await mountWith(BrowsersPanel)
    expect(bodyText()).toContain('还没有浏览器连上来')
    expect(bodyText()).toContain('在装了插件的浏览器里打开亚马逊页面，这里会自动出现。')
    expect(document.body.querySelectorAll('.empty-state button')).toHaveLength(0)
  })

  it('读取失败：手动读取时提示「读取浏览器失败：原因」', async () => {
    const e = new ApiError('NETWORK', '无法连接服务器，请检查服务端是否在运行', 0)
    devicesApi.listDevices.mockRejectedValue(e)
    await mountWith(BrowsersPanel)
    expect(feedback.notifyFailure).toHaveBeenCalledWith('读取浏览器', e)
  })
})

// ── 页面：两个页签 ────────────────────────────────────────────────────────────

describe('账号与浏览器页', () => {
  it('两个页签叫「账号」「浏览器」；旧链接 ?tab=devices 打开「浏览器」页签', async () => {
    const w = await mountWith(AccountsView, {}, '/accounts?tab=devices')
    expect(w.findAll('.el-tabs__item').map((t) => t.text())).toEqual(['账号', '浏览器'])
    expect(w.find('.el-tabs__item.is-active').text()).toBe('浏览器')
    expect(devicesApi.listDevices).toHaveBeenCalled()
  })

  it('缺省打开「账号」页签；浏览器页签里点「登记」会切回账号页签并打开已填好 ID 的对话框', async () => {
    devicesApi.listDevices.mockResolvedValue({
      total: 1,
      rows: [device({ id: 10, name: '浏览器 10', login: { state: 'unregistered', customer_id: 'A2TEST00005', at: new Date().toISOString(), stale: false }, account: null })],
    })
    const w = await mountWith(AccountsView, {}, '/accounts')
    expect(w.find('.el-tabs__item.is-active').text()).toBe('账号')
    await w.findAll('.el-tabs__item')[1].trigger('click')
    await settle()
    expect(router.currentRoute.value.query.tab).toBe('browsers')
    const pane = document.body.querySelector('#pane-browsers .el-table__body') as HTMLElement
    buttons('登记', pane)[0].click()
    await settle()
    expect(w.find('.el-tabs__item.is-active').text()).toBe('账号')
    const dialog = openDialog()!
    expect(dialog.textContent).toContain('登记买家号 · A2TEST00005')
    expect((dialog.querySelector('input[aria-label="买家号 ID"]') as HTMLInputElement).value).toBe('A2TEST00005')
  })
})

// ── 概览与设置 ────────────────────────────────────────────────────────────────

function overview(extra: Partial<Overview> = {}): Overview {
  return {
    bj_date: '2026-11-18', counts: {}, needs_attention: 0, today_executable: 0, future_assigned: 0,
    accounts: { total: 2, enabled: 2, paused: 0 }, devices: { total: 3, online: 2 }, discovered_buyers: 0, recent_exceptions: [],
    last_allocation: null, simulation_only: true, feishu_configured: true,
    ...extra,
  }
}

describe('概览：待登记的买家号提示', () => {
  it('有待登记的买家号时提示个数，「去登记」跳到账号页签', async () => {
    overviewApi.getOverview.mockResolvedValue(overview({ discovered_buyers: 2 }))
    await mountWith(OverviewView)
    expect(bodyText()).toContain('插件发现了 2 个还没登记的买家号，登记后才会分到任务')
    buttons('去登记')[0].click()
    await settle()
    expect(router.currentRoute.value.path).toBe('/accounts')
    expect(router.currentRoute.value.query.tab).toBe('accounts')
  })

  it('没有待登记的买家号（或旧服务端没给这个数）时不提示；统计卡叫「浏览器在线」', async () => {
    overviewApi.getOverview.mockResolvedValue(overview())
    await mountWith(OverviewView)
    expect(bodyText()).not.toContain('还没登记的买家号')
    expect(buttons('去登记')).toHaveLength(0)
    expect(bodyText()).toContain('浏览器在线')
    expect(bodyText()).toContain('2 / 3')
    expect(bodyText()).not.toContain('设备')
    mounted.pop()!.unmount()
    document.body.innerHTML = ''
    const { discovered_buyers: _omit, ...old } = overview()
    overviewApi.getOverview.mockResolvedValue(old)
    await mountWith(OverviewView)
    expect(buttons('去登记')).toHaveLength(0)
  })
})

describe('设置：工作区 ID', () => {
  it('显示本工作区 ID 与一行说明，可以复制', async () => {
    session.me = { user: { id: 1, username: 'owner', display_name: 'DEMO 所有者' }, workspace: { id: 42, name: 'DEMO 采购工作区' }, role: 'owner' }
    await mountWith(SettingsView)
    const card = document.body.querySelector('[data-testid="workspace-card"]') as HTMLElement
    expect(card.textContent).toContain('本工作区 ID')
    expect((card.querySelector('[data-testid="workspace-id"]') as HTMLElement).textContent).toBe('42')
    expect(card.textContent).toContain('系统里有多个工作区时，插件设置里要填它')
    expect(card.textContent).toContain('DEMO 采购工作区')
    buttons('复制', card)[0].click()
    await flushPromises()
    expect(clipboard.copyText).toHaveBeenCalledWith('42')
    expect(feedback.toast).toHaveBeenCalledWith('工作区 ID 已复制')
  })
})
