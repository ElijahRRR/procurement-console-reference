/**
 * 模拟后端（tests/mocks）与执行入口改造后的接口形状一致：待登记的买家号、登记 / 填到已有账号 / 忽略 / 恢复、
 * 浏览器列表（含已移除）、改名、移除；配对码、绑定、采用 ID、撤销这些旧接口不再存在。夹具里的买家号 ID 全部是明显的假值。
 * 来源指定买家号：任务行带 designated_buyer、分配方式可为 source、分配摘要带 designated；来源指定而没登记的买家号 ID
 * 并入待登记列表（source_orders），登记后任务立刻分给它；忽略名单里的 ID 全部列出。
 */
import { describe, expect, it } from 'vitest'
import type { Account, AllocationSummary, Device, DiscoveredBuyer, OrderDetail, OrderRow, Overview, Page } from '@/api/types'
import { ASSIGNMENT_KINDS, LOGIN_STATES } from '@/api/types'
import { createMockApi } from '../mocks/handlers'

const now = new Date('2026-11-18T08:00:00Z')
const base = 'http://127.0.0.1:8790/api/v1'

function api() {
  const mock = createMockApi({}, now)
  const call = <T>(method: string, path: string, body?: unknown) => {
    const res = mock.handle(method, new URL(base + path), body)
    return { status: res.status, body: res.body as T }
  }
  return { ...mock, call }
}

describe('模拟后端：待登记的买家号', () => {
  it('列表缺省不带已忽略的；include_ignored=1 带上忽略名单里的全部 ID；形状是 {items: [...]}，每项带 source_orders', () => {
    const { call } = api()
    const open = call<{ items: DiscoveredBuyer[] }>('GET', '/accounts/discovered?include_ignored=0').body.items
    // 浏览器上报的两个 + 只来自来源指定的一个（没有浏览器、没有上报时间，排在后面）
    expect(open.map((x) => x.customer_id)).toEqual(['A2TEST00005', 'A2TEST00004', 'A2TEST00007'])
    expect(open.every((x) => !x.ignored)).toBe(true)
    expect(open.map((x) => x.source_orders)).toEqual([1, 0, 2])
    expect(open[2]).toMatchObject({ browsers: [], last_reported_at: null, name_hint: '环境-DEMO-07' })
    const all = call<{ items: DiscoveredBuyer[] }>('GET', '/accounts/discovered?include_ignored=1').body.items
    // 忽略名单里的 A2TEST00098 此刻没有浏览器登录着，也照样列出
    expect(all.map((x) => x.customer_id)).toEqual(['A2TEST00005', 'A2TEST00004', 'A2TEST00099', 'A2TEST00007', 'A2TEST00098'])
    expect(all.filter((x) => x.ignored).map((x) => [x.customer_id, x.browsers.length])).toEqual([['A2TEST00099', 1], ['A2TEST00098', 0]])
    expect(Object.keys(all[0]).sort()).toEqual(['browsers', 'customer_id', 'ignored', 'last_reported_at', 'name_hint', 'source_orders'])
    expect(Object.keys(all[0].browsers[0]).sort()).toEqual(['id', 'last_seen_at', 'name', 'online'])
  })

  it('登记 = 带 amazon_customer_id 的新建：买家号离开待登记列表，浏览器变成已登记，概览计数减一', () => {
    const { call } = api()
    expect(call<Overview>('GET', '/overview').body.discovered_buyers).toBe(3)
    const created = call<Account>('POST', '/accounts', { name: '环境-DEMO-05', amazon_customer_id: 'A2TEST00005', daily_limit: 20, min_amount: '0.00' })
    expect(created.status).toBe(200)
    expect(created.body).toMatchObject({ name: '环境-DEMO-05', amazon_customer_id: 'A2TEST00005' })
    expect(created.body.browsers.map((b) => b.id)).toEqual([10])
    expect(created.body).not.toHaveProperty('devices')
    expect(created.body).not.toHaveProperty('default_device_id')
    expect(call<{ items: DiscoveredBuyer[] }>('GET', '/accounts/discovered?include_ignored=0').body.items.map((x) => x.customer_id)).toEqual(['A2TEST00004', 'A2TEST00007'])
    const dev = call<Page<Device>>('GET', '/devices').body.rows.find((d) => d.id === 10)!
    expect(dev.login.state).toBe('ready')
    expect(dev.account).toEqual({ id: created.body.id, name: '环境-DEMO-05' })
    expect(call<Overview>('GET', '/overview').body.discovered_buyers).toBe(2)
    // 同一个 ID 不能登记到两个账号
    expect(call('POST', '/accounts', { name: '环境-DEMO-06', amazon_customer_id: 'A2TEST00005' }).status).toBe(409)
    expect(call('POST', '/accounts', { name: '环境-DEMO-06', amazon_customer_id: 'not-an-id' }).status).toBe(422)
  })

  it('填到已有账号 = PATCH 现有账号；账号正在执行时不能改买家号 ID（409）', () => {
    const { call } = api()
    const filled = call<Account>('PATCH', '/accounts/8', { amazon_customer_id: 'A2TEST00004' })
    expect(filled.status).toBe(200)
    expect(filled.body.amazon_customer_id).toBe('A2TEST00004')
    expect(filled.body.browsers.map((b) => b.id)).toEqual([11])
    expect(call<{ items: DiscoveredBuyer[] }>('GET', '/accounts/discovered?include_ignored=0').body.items.map((x) => x.customer_id)).toEqual(['A2TEST00005', 'A2TEST00007'])
    // 账号 3 有未结束的批次
    const busy = call<{ code: string; message: string }>('PATCH', '/accounts/3', { amazon_customer_id: 'A2TEST00005' })
    expect(busy.status).toBe(409)
    expect(busy.body.code).toBe('CONFLICT')
    // 不带买家号 ID 的修改不受影响
    expect(call('PATCH', '/accounts/3', { priority: 6 }).status).toBe(200)
  })

  it('忽略与恢复：幂等，返回 {ok: true}；忽略后浏览器显示为已忽略的买家号', () => {
    const { call } = api()
    expect(call('POST', '/accounts/discovered/ignore', { customer_id: 'A2TEST00005' }).body).toEqual({ ok: true })
    expect(call('POST', '/accounts/discovered/ignore', { customer_id: 'A2TEST00005' }).body).toEqual({ ok: true })
    expect(call<{ items: DiscoveredBuyer[] }>('GET', '/accounts/discovered?include_ignored=0').body.items.map((x) => x.customer_id)).toEqual(['A2TEST00004', 'A2TEST00007'])
    expect(call<Page<Device>>('GET', '/devices').body.rows.find((d) => d.id === 10)!.login.state).toBe('ignored')
    expect(call('POST', '/accounts/discovered/unignore', { customer_id: 'A2TEST00005' }).body).toEqual({ ok: true })
    expect(call<Page<Device>>('GET', '/devices').body.rows.find((d) => d.id === 10)!.login.state).toBe('unregistered')
    expect(call('POST', '/accounts/discovered/ignore', { customer_id: 'bad' }).status).toBe(422)
  })

  it('忽略名单与浏览器无关：没有浏览器登录着的 ID 也能忽略、列出、恢复；概览只数没忽略的', () => {
    const { call } = api()
    const ids = (all: boolean) => call<{ items: DiscoveredBuyer[] }>('GET', `/accounts/discovered?include_ignored=${all ? 1 : 0}`).body.items.map((x) => x.customer_id)
    // 只来自来源的 A2TEST00007：忽略后离开待登记列表，但仍在已忽略里
    call('POST', '/accounts/discovered/ignore', { customer_id: 'A2TEST00007' })
    expect(ids(false)).toEqual(['A2TEST00005', 'A2TEST00004'])
    expect(ids(true)).toEqual(['A2TEST00005', 'A2TEST00004', 'A2TEST00099', 'A2TEST00007', 'A2TEST00098'])
    expect(call<Overview>('GET', '/overview').body.discovered_buyers).toBe(2)
    call('POST', '/accounts/discovered/unignore', { customer_id: 'A2TEST00007' })
    expect(ids(false)).toEqual(['A2TEST00005', 'A2TEST00004', 'A2TEST00007'])
    // 恢复一个既没有浏览器也没有来源指定的 ID：它不再出现在任何一组里
    call('POST', '/accounts/discovered/unignore', { customer_id: 'A2TEST00098' })
    expect(ids(true)).toEqual(['A2TEST00005', 'A2TEST00004', 'A2TEST00099', 'A2TEST00007'])
    expect(call<Overview>('GET', '/overview').body.discovered_buyers).toBe(3)
  })
})

describe('模拟后端：来源指定买家号', () => {
  const designated = (rows: OrderRow[]) => rows.filter((o) => o.designated_buyer)

  it('任务行与详情带 designated_buyer（没有指定为 null）；分配方式取值都在枚举里，夹具覆盖三种', () => {
    const { call, fixtures: fx } = api()
    const rows = call<Page<OrderRow>>('GET', '/orders?view=all&page_size=100').body.rows
    for (const o of rows) {
      expect(o).toHaveProperty('designated_buyer')
      if (o.designated_buyer) expect(Object.keys(o.designated_buyer).sort()).toEqual(['account', 'customer_id', 'name'])
      if (o.assignment_kind) expect(ASSIGNMENT_KINDS).toContain(o.assignment_kind)
      // 有账号才有分配方式
      expect(o.assigned_account === null).toBe(o.assignment_kind === null)
    }
    expect([...new Set(rows.map((o) => o.assignment_kind).filter(Boolean))].sort()).toEqual([...ASSIGNMENT_KINDS].sort())
    // 有代表性的四种：按来源分出去的、指定了但留在待分配并写明原因的、人工改派到别的账号的、指定的买家号还没登记的
    const des = designated(rows)
    const bySource = des.filter((o) => o.assignment_kind === 'source')
    expect(bySource.length).toBeGreaterThanOrEqual(2)
    for (const o of bySource) {
      expect(o.designated_buyer!.account).toEqual(o.assigned_account)
      expect(o.assignment_note).toMatch(/^按来源的买家号/)
    }
    const stuck = des.filter((o) => o.status === 'queued')
    expect(stuck.length).toBeGreaterThanOrEqual(3)
    for (const o of stuck) expect(o.status_reason).toContain('来源指定的')
    const overridden = des.filter((o) => o.assignment_kind === 'manual')
    expect(overridden).toHaveLength(1)
    expect(overridden[0].designated_buyer!.account!.id).not.toBe(overridden[0].assigned_account!.id)
    expect(stuck.some((o) => o.designated_buyer!.account === null && !!o.designated_buyer!.customer_id)).toBe(true)
    // 只填买家号（名称）与只填买家号 ID 的都有
    expect(des.some((o) => !o.designated_buyer!.customer_id && !!o.designated_buyer!.name)).toBe(true)
    expect(des.some((o) => !!o.designated_buyer!.customer_id && !o.designated_buyer!.name)).toBe(true)
    // 详情与列表行一致；来源指定的买家号 ID 全部是假值
    for (const o of des) {
      expect(call<OrderDetail>('GET', `/orders/${o.id}`).body.designated_buyer).toEqual(o.designated_buyer)
      if (o.designated_buyer!.customer_id) expect(o.designated_buyer!.customer_id).toMatch(/^A(1DEMO|2TEST)\d+$/)
    }
    // 待登记列表里的 source_orders 与任务上的指定对得上
    for (const x of fx.discovered) {
      const n = rows.filter((o) => o.designated_buyer?.customer_id === x.customer_id && !['purchased', 'closed', 'cancelled'].includes(o.status)).length
      expect([x.customer_id, x.source_orders]).toEqual([x.customer_id, n])
    }
  })

  it('时间线：指定的任务有 order.designation_changed（扁平键 [旧, 新]）；分配事件的 kind 与任务的分配方式一致', () => {
    const { call } = api()
    const rows = designated(call<Page<OrderRow>>('GET', '/orders?view=all&page_size=100').body.rows)
    for (const o of rows) {
      const events = call<OrderDetail>('GET', `/orders/${o.id}`).body.events
      const changed = events.find((e) => e.kind === 'order.designation_changed')!
      const changes = changed.detail.changes as Record<string, [unknown, unknown]>
      expect(Object.keys(changes).length).toBeGreaterThan(0)
      for (const k of Object.keys(changes)) expect(['designated_customer_id', 'designated_buyer_name']).toContain(k)
      if (o.designated_buyer!.customer_id) expect(changes.designated_customer_id).toEqual([null, o.designated_buyer!.customer_id])
      if (o.designated_buyer!.name) expect(changes.designated_buyer_name).toEqual([null, o.designated_buyer!.name])
      const assignedEvent = events.find((e) => e.kind === 'order.assigned')
      if (o.assigned_account) expect(assignedEvent!.detail.kind).toBe(o.assignment_kind)
      else expect(assignedEvent).toBeUndefined()
    }
  })

  it('分配摘要：assigned = coverage + fill + designated；来源指定而没分出去的项带 designated', () => {
    const { call, fixtures: fx } = api()
    const check = (a: AllocationSummary) => {
      expect(a.stats.designated).toBeGreaterThan(0)
      expect(a.stats.assigned).toBe(a.stats.coverage + a.stats.fill + (a.stats.designated ?? 0))
      expect(a.stats.considered).toBe(a.stats.assigned + a.stats.unassigned)
      expect(a.unassigned).toHaveLength(a.stats.unassigned)
      for (const u of a.unassigned) expect(!!u.designated).toBe(!!fx.details[u.order_id].designated_buyer)
      expect(a.unassigned.some((u) => u.designated)).toBe(true)
      expect(a.unassigned.some((u) => !u.designated)).toBe(true)
    }
    check(call<Overview>('GET', '/overview').body.last_allocation!)
    check(call<AllocationSummary>('POST', '/allocation/run', {}).body)
  })

  it('人工改派后分配方式变成 manual、指定原样保留；解除分配后任务回到待分配，指定还在', () => {
    const { call, fixtures: fx } = api()
    const o = fx.orders.find((x) => x.assignment_kind === 'source' && x.designated_buyer?.customer_id === 'A1DEMO00002')!
    const before = o.designated_buyer
    const moved = call<OrderDetail>('POST', `/orders/${o.id}/assign`, { account_id: 3 }).body
    expect(moved).toMatchObject({ status: 'assigned', assignment_kind: 'manual', assigned_account: { id: 3, name: '环境-DEMO-01' } })
    expect(moved.designated_buyer).toEqual(before)
    const freed = call<OrderDetail>('POST', `/orders/${o.id}/unassign`, {}).body
    expect(freed).toMatchObject({ status: 'queued', assigned_account: null, assignment_kind: null, status_reason: '人工解除分配' })
    expect(freed.designated_buyer).toEqual(before)
  })

  it('登记来源指定的买家号（新建账号）：等着它的任务立刻分给新账号，分配方式「来源指定」，买家号离开待登记列表', () => {
    const { call } = api()
    const waiting = () => call<Page<OrderRow>>('GET', '/orders?view=unassigned&page_size=100').body.rows.filter((o) => o.designated_buyer?.customer_id === 'A2TEST00007')
    expect(waiting()).toHaveLength(2)
    const queuedBefore = call<Overview>('GET', '/overview').body.counts.queued ?? 0
    const created = call<Account>('POST', '/accounts', { name: '环境-DEMO-07', amazon_customer_id: 'A2TEST00007', daily_limit: 20, min_amount: '0.00', max_amount: null })
    expect(created.status).toBe(200)
    expect(created.body.browsers).toEqual([])
    expect(waiting()).toHaveLength(0)
    const mine = call<Page<OrderRow>>('GET', `/orders?view=all&account_id=${created.body.id}&page_size=100`).body.rows
    expect(mine).toHaveLength(2)
    for (const o of mine) {
      expect(o).toMatchObject({ status: 'assigned', assignment_kind: 'source', assignment_note: '按来源的买家号 ID A2TEST00007', status_reason: null })
      expect(o.designated_buyer!.account).toEqual({ id: created.body.id, name: '环境-DEMO-07' })
      expect(call<OrderDetail>('GET', `/orders/${o.id}`).body.allowed_actions).toContain('unassign')
    }
    expect(call<{ items: DiscoveredBuyer[] }>('GET', '/accounts/discovered?include_ignored=0').body.items.map((x) => x.customer_id)).toEqual(['A2TEST00005', 'A2TEST00004'])
    const after = call<Overview>('GET', '/overview').body
    expect(after.discovered_buyers).toBe(2)
    expect(after.counts.queued).toBe(queuedBefore - 2)
  })

  it('填到已有账号：账号接不了（采购开关关闭）时任务留在待分配并写明原因，不分给别的账号', () => {
    const { call } = api()
    // 账号 8（环境-DEMO-04）没开采购开关
    expect(call<Account>('PATCH', '/accounts/8', { amazon_customer_id: 'A2TEST00005' }).status).toBe(200)
    const o = call<Page<OrderRow>>('GET', '/orders?view=unassigned&page_size=100').body.rows.find((x) => x.designated_buyer?.customer_id === 'A2TEST00005')!
    expect(o.assigned_account).toBeNull()
    expect(o.status_reason).toBe('来源指定的账号 环境-DEMO-04 已关闭采购开关，不接收新分配；打开开关后会自动分配')
    expect(o.designated_buyer!.account).toEqual({ id: 8, name: '环境-DEMO-04' })
    // 已登记了，不再是待登记的买家号
    expect(call<{ items: DiscoveredBuyer[] }>('GET', '/accounts/discovered?include_ignored=0').body.items.map((x) => x.customer_id)).toEqual(['A2TEST00004', 'A2TEST00007'])
  })

  it('字段对应用的系统字段含「买家号」「买家号ID」两列', () => {
    const { fixtures: fx } = api()
    const keys = fx.systemFields.source_fields.map((f) => f.key)
    expect(keys).toContain('external_buyer_account')
    expect(keys).toContain('external_buyer_customer_id')
    expect(fx.upload.suggested_mapping).toMatchObject({ 买家号: 'external_buyer_account', 买家号ID: 'external_buyer_customer_id' })
  })
})

describe('模拟后端：浏览器', () => {
  it('列表缺省不带已移除的；include_hidden=1 带上；每行的形状与登录状态取值', () => {
    const { call } = api()
    const shown = call<Page<Device>>('GET', '/devices?include_hidden=0').body.rows
    expect(shown.every((d) => !d.hidden_at)).toBe(true)
    const all = call<Page<Device>>('GET', '/devices?include_hidden=1').body.rows
    expect(all.length).toBe(shown.length + 1)
    expect(Object.keys(all[0]).sort()).toEqual(['account', 'active_run', 'created_at', 'ext_version', 'hidden_at', 'id', 'last_seen_at', 'login', 'name', 'online'])
    expect(Object.keys(all[0].login).sort()).toEqual(['at', 'customer_id', 'stale', 'state'])
    // 夹具覆盖全部登录状态
    expect([...new Set(all.map((d) => d.login.state))].sort()).toEqual([...LOGIN_STATES].sort())
    for (const d of all) expect(d.account !== null).toBe(d.login.state === 'ready')
    const overview = call<Overview>('GET', '/overview').body
    expect(overview.devices).toEqual({ total: shown.length, online: shown.filter((d) => d.online).length })
  })

  it('只能改名；正在执行的浏览器不能移除（409），空闲的移除后从缺省列表消失', () => {
    const { call } = api()
    expect(call<Device>('PATCH', '/devices/9', { name: 'Mac-Chrome-DEMO-09' }).body.name).toBe('Mac-Chrome-DEMO-09')
    expect(call('PATCH', '/devices/9', { name: '  ' }).status).toBe(422)
    const busy = call<{ code: string; message: string }>('POST', '/devices/5/remove', {})
    expect(busy.status).toBe(409)
    expect(busy.body).toMatchObject({ code: 'CONFLICT', message: '这个浏览器正在执行，结束后才能移除' })
    expect(call<Device>('POST', '/devices/9/remove', {}).body.hidden_at).toBeTruthy()
    expect(call<Page<Device>>('GET', '/devices').body.rows.some((d) => d.id === 9)).toBe(false)
    expect(call<Page<Device>>('GET', '/devices?include_hidden=1').body.rows.some((d) => d.id === 9)).toBe(true)
    expect(call('POST', '/devices/999/remove', {}).status).toBe(404)
  })

  it('配对码、采用 ID、撤销这些旧接口不再存在', () => {
    const { call } = api()
    expect(call('POST', '/devices/pairing-codes', {}).status).toBe(404)
    expect(call('POST', '/devices/5/revoke', {}).status).toBe(404)
    expect(call('POST', '/accounts/3/adopt-customer-id', { device_id: 5 }).status).toBe(404)
  })
})

describe('夹具', () => {
  it('账号与浏览器的形状里没有旧字段；买家号 ID 全部是明显的假值', () => {
    const { fixtures: fx } = api()
    for (const a of fx.accounts) {
      expect(a).not.toHaveProperty('devices')
      expect(a).not.toHaveProperty('default_device_id')
      expect(Array.isArray(a.browsers)).toBe(true)
    }
    for (const d of fx.devices) {
      for (const k of ['bound_account', 'identity_status', 'revoked_at', 'browser', 'identity']) expect(d).not.toHaveProperty(k)
    }
    const ids = [
      ...fx.accounts.map((a) => a.amazon_customer_id),
      ...fx.devices.map((d) => d.login.customer_id),
      ...fx.discovered.map((x) => x.customer_id),
      ...fx.orders.map((o) => o.designated_buyer?.customer_id),
    ].filter((x): x is string => !!x)
    expect(ids.length).toBeGreaterThan(5)
    for (const id of ids) expect(id).toMatch(/^A(1DEMO|2TEST)\d+$/)
    // 账号的「所在浏览器」与浏览器的登录状态互相对得上
    for (const a of fx.accounts) {
      const on = fx.devices.filter((d) => !d.hidden_at && !!a.amazon_customer_id && d.login.customer_id === a.amazon_customer_id).map((d) => d.id)
      expect(a.browsers.map((b) => b.id)).toEqual(on)
    }
    expect(fx.overview.discovered_buyers).toBe(fx.discovered.filter((x) => !x.ignored).length)
  })
})
