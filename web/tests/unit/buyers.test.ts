/**
 * 执行入口改造后的展示规则（纯函数）：浏览器的登录状态 → 「当前登录」一栏的文字与颜色、上报时间说明；
 * 待登记的买家号整理（插件读到的与来源指定的合在一起）、「填到已有账号」的候选与确认文案；概览提示；页签取值。
 * 买家号 ID 全部虚构。
 */
import { describe, expect, it } from 'vitest'
import type { Account, Device, DiscoveredBuyer, LoginState } from '@/api/types'
import { LOGIN_STATES } from '@/api/types'
import {
  CUSTOMER_ID_RE,
  IGNORE_CONFIRM_TEXT,
  NO_BROWSER_TEXT,
  accountsTab,
  discoveredNotice,
  fillConfirmText,
  fillTargets,
  normalizeCustomerId,
  registerHelpText,
  registeredEffectText,
  sourceOrdersText,
  splitDiscovered,
  suggestedFillTarget,
} from '@/utils/accounts'
import { browserCounts, browserLoginText, browserLoginView, browserOnlineText, loginReportedText } from '@/utils/browsers'
import { LOGIN_STATE_DOT, LOGIN_STATE_LABEL, LOGIN_STATE_TAG } from '@/utils/labels'

const now = new Date('2026-11-18T08:00:00+00:00')
const ago = (minutes: number) => new Date(now.getTime() - minutes * 60_000).toISOString()

function login(state: LoginState | string, customerId: string | null = null, extra: Partial<Device['login']> = {}): Pick<Device, 'login' | 'account'> {
  return { login: { state: state as LoginState, customer_id: customerId, at: ago(1), stale: false, ...extra }, account: null }
}

describe('浏览器「当前登录」一栏（三端统一的状态表）', () => {
  it('每个登录状态的文字', () => {
    expect(browserLoginText(login('unknown'))).toBe('还没有上报')
    expect(browserLoginText(login('signed_out'))).toBe('未登录亚马逊')
    expect(browserLoginText(login('no_id'))).toBe('已登录，没读到买家号 ID')
    expect(browserLoginText(login('conflict'))).toBe('登录状态不一致')
    expect(browserLoginText(login('unregistered', 'A2TEST00005'))).toBe('未登记的买家号 A2TEST00005')
    expect(browserLoginText(login('ignored', 'A2TEST00099'))).toBe('已忽略的买家号 A2TEST00099')
    expect(browserLoginText({ ...login('ready', 'A1DEMO00001'), account: { id: 3, name: '环境-DEMO-01' } })).toBe('环境-DEMO-01')
  })

  it('已登记且账号暂停执行：账号名后加「（已暂停）」；暂停状态取服务端给的，没给时按账号列表补', () => {
    const d = { ...login('ready', 'A1DEMO00003'), account: { id: 7, name: '环境-DEMO-03' } }
    expect(browserLoginText(d, new Set([7]))).toBe('环境-DEMO-03（已暂停）')
    expect(browserLoginText(d, new Set([9]))).toBe('环境-DEMO-03')
    expect(browserLoginText({ ...d, account: { ...d.account, execution_paused: true } })).toBe('环境-DEMO-03（已暂停）')
    // 服务端明确说没暂停时，以服务端为准
    expect(browserLoginText({ ...d, account: { ...d.account, execution_paused: false } }, new Set([7]))).toBe('环境-DEMO-03')
    const v = browserLoginView(d, new Set([7]))
    expect(v).toMatchObject({ paused: true, dot: 'red', tag: 'danger', customerId: null, canRegister: false })
  })

  it('只有读到 ID 的未登记买家号给「登记」入口', () => {
    expect(browserLoginView(login('unregistered', 'A2TEST00005'))).toMatchObject({ canRegister: true, customerId: 'A2TEST00005', label: '未登记的买家号' })
    expect(browserLoginView(login('unregistered', null))).toMatchObject({ canRegister: false, text: '未登记的买家号' })
    for (const s of ['unknown', 'signed_out', 'no_id', 'conflict', 'ignored', 'ready'] as const) {
      expect(browserLoginView({ ...login(s, 'A1DEMO00001'), account: { id: 1, name: 'DEMO' } }).canRegister).toBe(false)
    }
  })

  it('状态点与标签色：已登记绿、要处理的黄、读数不一致红、没有信息灰', () => {
    expect(LOGIN_STATES.map((s) => LOGIN_STATE_DOT[s])).toEqual(['gray', 'yellow', 'yellow', 'red', 'yellow', 'gray', 'green'])
    expect(LOGIN_STATES.map((s) => LOGIN_STATE_TAG[s])).toEqual(['info', 'warning', 'warning', 'danger', 'warning', 'info', 'success'])
    for (const s of LOGIN_STATES) {
      const v = browserLoginView({ ...login(s, 'A1DEMO00001'), account: { id: 1, name: 'DEMO' } })
      expect(v.dot).toBe(LOGIN_STATE_DOT[s])
      expect(v.tag).toBe(LOGIN_STATE_TAG[s])
    }
  })

  it('已登记但没带账号、未知的状态、没有 login 字段都不报错，也不当作已登记的账号', () => {
    expect(browserLoginText(login('ready', 'A1DEMO00001'))).toBe(`${LOGIN_STATE_LABEL.ready} A1DEMO00001`)
    expect(browserLoginView(login('something_new', 'A1DEMO00001'))).toMatchObject({ text: 'something_new', dot: 'gray', tag: 'info', canRegister: false })
    expect(browserLoginText({ login: undefined as unknown as Device['login'], account: null })).toBe('还没有上报')
  })

  it('上报时间说明：过期时写明；从未上报为空', () => {
    expect(loginReportedText({ state: 'ready', customer_id: 'A1DEMO00001', at: ago(2), stale: false }, now)).toBe('2 分钟前上报')
    expect(loginReportedText({ state: 'ready', customer_id: 'A1DEMO00001', at: ago(0.2), stale: false }, now)).toBe('刚刚上报')
    expect(loginReportedText({ state: 'ready', customer_id: 'A1DEMO00002', at: ago(60 * 5), stale: true }, now)).toBe('5 小时前上报，已过期')
    expect(loginReportedText({ state: 'signed_out', customer_id: null, at: ago(60 * 24 * 30), stale: true }, now)).toBe('2026-10-19 上报，已过期')
    expect(loginReportedText({ state: 'unknown', customer_id: null, at: null, stale: true }, now)).toBe('')
    expect(loginReportedText(null, now)).toBe('')
  })

  it('在线说明与在线计数（已移除的不算）', () => {
    expect(browserOnlineText({ online: true, last_seen_at: ago(0.5) }, now)).toBe('在线')
    expect(browserOnlineText({ online: false, last_seen_at: ago(60 * 5) }, now)).toBe('离线 · 5 小时前')
    expect(browserOnlineText({ online: false, last_seen_at: null }, now)).toBe('离线')
    expect(browserCounts([{ online: true, hidden_at: null }, { online: false, hidden_at: null }, { online: true, hidden_at: ago(5) }])).toEqual({ online: 1, total: 2 })
  })
})

function buyer(customerId: string, extra: Partial<DiscoveredBuyer> = {}): DiscoveredBuyer {
  return { customer_id: customerId, ignored: false, browsers: [{ id: 10, name: '浏览器 10', online: true, last_seen_at: ago(1) }], last_reported_at: ago(1), name_hint: null, ...extra }
}

describe('待登记的买家号整理', () => {
  it('分成待登记与已忽略两组，各按最近上报时间倒序', () => {
    const { pending, ignored } = splitDiscovered([
      buyer('A2TEST00004', { last_reported_at: ago(30) }),
      buyer('A2TEST00099', { ignored: true, last_reported_at: ago(600) }),
      buyer('A2TEST00005', { last_reported_at: ago(1) }),
      buyer('A2TEST00098', { ignored: true, last_reported_at: ago(5) }),
      buyer('A2TEST00006', { last_reported_at: null }),
    ])
    expect(pending.map((x) => x.customer_id)).toEqual(['A2TEST00005', 'A2TEST00004', 'A2TEST00006'])
    expect(ignored.map((x) => x.customer_id)).toEqual(['A2TEST00098', 'A2TEST00099'])
  })

  it('没有 ID 的条目丢掉；同一个 ID 合并浏览器、取较新的上报时间；空输入得到两组空列表', () => {
    const { pending } = splitDiscovered([
      buyer(''),
      buyer('a2test00005', { browsers: [{ id: 10, name: '浏览器 10', online: true, last_seen_at: null }], last_reported_at: ago(9), name_hint: ' 环境-DEMO-05 ' }),
      buyer('A2TEST00005', { browsers: [{ id: 11, name: '浏览器 11', online: false, last_seen_at: null }], last_reported_at: ago(2) }),
    ])
    expect(pending).toHaveLength(1)
    expect(pending[0]).toMatchObject({ customer_id: 'A2TEST00005', last_reported_at: ago(2), name_hint: '环境-DEMO-05' })
    expect(pending[0].browsers.map((b) => b.id)).toEqual([10, 11])
    expect(splitDiscovered(null)).toEqual({ pending: [], ignored: [] })
    // 旧服务端不给 source_orders：按 0
    expect(splitDiscovered([{ customer_id: 'A2TEST00007', ignored: false } as DiscoveredBuyer]).pending[0]).toEqual({
      customer_id: 'A2TEST00007', ignored: false, browsers: [], last_reported_at: null, name_hint: null, source_orders: 0,
    })
  })

  it('只来自来源指定的买家号（没有浏览器、没有上报时间）排在后面，指定任务多的在前；同一个 ID 取较大的任务数', () => {
    const fromSource = (id: string, n: number, extra: Partial<DiscoveredBuyer> = {}) => buyer(id, { browsers: [], last_reported_at: null, source_orders: n, ...extra })
    const { pending, ignored } = splitDiscovered([
      fromSource('A2TEST00008', 1),
      fromSource('A2TEST00007', 3, { name_hint: '环境-DEMO-07' }),
      buyer('A2TEST00005', { last_reported_at: ago(5), source_orders: 2 }),
      fromSource('A2TEST00009', 1),
      buyer('A2TEST00005', { last_reported_at: ago(9), source_orders: 1 }),
      // 忽略名单里的 ID 此刻没有浏览器登录着也照样列出
      fromSource('A2TEST00098', 0, { ignored: true }),
      buyer('A2TEST00099', { ignored: true, last_reported_at: ago(600) }),
    ])
    expect(pending.map((x) => [x.customer_id, x.source_orders])).toEqual([['A2TEST00005', 2], ['A2TEST00007', 3], ['A2TEST00008', 1], ['A2TEST00009', 1]])
    expect(pending[1]).toMatchObject({ browsers: [], last_reported_at: null, name_hint: '环境-DEMO-07' })
    expect(ignored.map((x) => x.customer_id)).toEqual(['A2TEST00099', 'A2TEST00098'])
    // 不是数字、负数都按 0
    expect(splitDiscovered([buyer('A2TEST00004', { source_orders: -2 }), buyer('A2TEST00006', { source_orders: 'x' as unknown as number })]).pending.map((x) => x.source_orders)).toEqual([0, 0])
  })

  it('来源指定的任务数与没有浏览器登录的说法', () => {
    expect(sourceOrdersText({ source_orders: 3 })).toBe('来源指定了 3 笔任务')
    expect(sourceOrdersText({ source_orders: 0 })).toBe('')
    expect(sourceOrdersText({})).toBe('')
    expect(sourceOrdersText(null)).toBe('')
    expect(NO_BROWSER_TEXT).toBe('还没有浏览器登录这个买家号')
  })

  it('登记之后的提示：有浏览器登录着 → 可以领取；来源指定了任务 → 按指定重新分配', () => {
    expect(registeredEffectText(buyer('A2TEST00005'))).toBe('登录着这个买家号的浏览器现在可以领取它的任务。')
    expect(registeredEffectText(buyer('A2TEST00005', { source_orders: 2 }))).toBe('登录着这个买家号的浏览器现在可以领取它的任务；来源指定给它的 2 笔任务会按指定重新分配。')
    expect(registeredEffectText(buyer('A2TEST00007', { browsers: [], source_orders: 1 }))).toBe('来源指定给它的 1 笔任务会按指定重新分配。')
    expect(registeredEffectText(buyer('A2TEST00098', { browsers: [] }))).toBe('')
  })

  it('登记对话框的说明：插件读到的写从浏览器读出；只来自来源的写明还没有浏览器登录', () => {
    const fromBrowser = '买家号 ID 由插件从浏览器登录的亚马逊页面读出。登记后，登录着这个买家号的浏览器就能领取这个账号的任务；请确认名称、金额区间与每日单数。'
    expect(registerHelpText(null)).toBe(fromBrowser)
    expect(registerHelpText({ hasBrowser: true, sourceOrders: 0 })).toBe(fromBrowser)
    expect(registerHelpText({ hasBrowser: true, sourceOrders: 2 })).toBe(
      '买家号 ID 由插件从浏览器登录的亚马逊页面读出。登记后，登录着这个买家号的浏览器就能领取这个账号的任务；来源指定给它的 2 笔任务会按指定重新分配；请确认名称、金额区间与每日单数。',
    )
    expect(registerHelpText({ hasBrowser: false, sourceOrders: 2 })).toBe(
      '这个买家号 ID 是来源指定的，还没有浏览器登录这个买家号。登记后，来源指定给它的 2 笔任务会按指定重新分配；登录着这个买家号的浏览器装上插件后才能领取。请确认名称、金额区间与每日单数。',
    )
  })

  const acc = (id: number, name: string, customerId: string | null) => ({ id, name, amazon_customer_id: customerId }) as Account
  const accounts = [acc(3, '环境-DEMO-01', 'A1DEMO00001'), acc(8, '环境-DEMO-04', null), acc(9, '环境-DEMO-06', null), acc(4, '环境-DEMO-02', 'A1DEMO00002')]

  it('「填到已有账号」只列还没登记买家号 ID 的账号；与来源里的名称同名的排最前', () => {
    expect(fillTargets(accounts, buyer('A2TEST00005')).map((a) => a.id)).toEqual([8, 9])
    expect(fillTargets(accounts, buyer('A2TEST00005', { name_hint: '环境-DEMO-06' })).map((a) => a.id)).toEqual([9, 8])
    expect(fillTargets(accounts, buyer('A2TEST00005', { name_hint: '环境-DEMO-01' })).map((a) => a.id)).toEqual([8, 9])
    expect(fillTargets([accounts[0]], buyer('A2TEST00005'))).toEqual([])
    expect(suggestedFillTarget(accounts, buyer('A2TEST00004', { name_hint: '环境-DEMO-04' }))?.id).toBe(8)
    // 同名账号已经登记了别的 ID：不建议
    expect(suggestedFillTarget(accounts, buyer('A2TEST00004', { name_hint: '环境-DEMO-01' }))).toBeNull()
    expect(suggestedFillTarget(accounts, buyer('A2TEST00004'))).toBeNull()
  })

  it('确认文案与概览提示', () => {
    expect(fillConfirmText('A2TEST00004', '环境-DEMO-04')).toBe('确定要把买家号 ID A2TEST00004 登记到「环境-DEMO-04」吗？')
    expect(IGNORE_CONFIRM_TEXT).toBe('确定要忽略这个买家号吗？忽略后它不再出现在待登记列表里；之后可以在已忽略的买家号里恢复。')
    // 个数是插件读到的与来源指定的并集，所以不再写「插件发现了」
    expect(discoveredNotice(2)).toBe('有 2 个还没登记的买家号（插件读到的或来源指定的），登记后才会分到任务')
    expect(discoveredNotice(0)).toBe('')
    expect(discoveredNotice(undefined)).toBe('')
    expect(discoveredNotice(null)).toBe('')
  })

  it('买家号 ID：去空白、转大写，空为 null；形状是 A 开头加至少 8 位大写字母或数字', () => {
    expect(normalizeCustomerId(' a1demo00000 ')).toBe('A1DEMO00000')
    expect(normalizeCustomerId('A1 DEMO 00000')).toBe('A1DEMO00000')
    expect(normalizeCustomerId('   ')).toBeNull()
    expect(normalizeCustomerId(null)).toBeNull()
    expect(CUSTOMER_ID_RE.test('A1DEMO00000')).toBe(true)
    expect(CUSTOMER_ID_RE.test('A1DEMO0')).toBe(false)
    expect(CUSTOMER_ID_RE.test('B1DEMO00000')).toBe(false)
    expect(CUSTOMER_ID_RE.test('a1demo00000')).toBe(false)
  })

  it('页签取值：旧链接的 ?tab=devices 落到「浏览器」', () => {
    expect(accountsTab('browsers')).toBe('browsers')
    expect(accountsTab('devices')).toBe('browsers')
    expect(accountsTab(['devices'])).toBe('browsers')
    expect(accountsTab('accounts')).toBe('accounts')
    expect(accountsTab(undefined)).toBe('accounts')
    expect(accountsTab('other')).toBe('accounts')
  })
})
