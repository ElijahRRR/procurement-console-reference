/**
 * 模拟后端（tests/mocks）与执行入口改造后的接口形状一致：待登记的买家号、登记 / 填到已有账号 / 忽略 / 恢复、
 * 浏览器列表（含已移除）、改名、移除；配对码、绑定、采用 ID、撤销这些旧接口不再存在。夹具里的买家号 ID 全部是明显的假值。
 */
import { describe, expect, it } from 'vitest'
import type { Account, Device, DiscoveredBuyer, Overview, Page } from '@/api/types'
import { LOGIN_STATES } from '@/api/types'
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
  it('列表缺省不带已忽略的；include_ignored=1 带上；形状是 {items: [...]}', () => {
    const { call } = api()
    const open = call<{ items: DiscoveredBuyer[] }>('GET', '/accounts/discovered?include_ignored=0').body.items
    expect(open.map((x) => x.customer_id)).toEqual(['A2TEST00005', 'A2TEST00004'])
    expect(open.every((x) => !x.ignored)).toBe(true)
    const all = call<{ items: DiscoveredBuyer[] }>('GET', '/accounts/discovered?include_ignored=1').body.items
    expect(all.map((x) => x.customer_id)).toEqual(['A2TEST00005', 'A2TEST00004', 'A2TEST00099'])
    expect(Object.keys(all[0]).sort()).toEqual(['browsers', 'customer_id', 'ignored', 'last_reported_at', 'name_hint'])
    expect(Object.keys(all[0].browsers[0]).sort()).toEqual(['id', 'last_seen_at', 'name', 'online'])
  })

  it('登记 = 带 amazon_customer_id 的新建：买家号离开待登记列表，浏览器变成已登记，概览计数减一', () => {
    const { call } = api()
    expect(call<Overview>('GET', '/overview').body.discovered_buyers).toBe(2)
    const created = call<Account>('POST', '/accounts', { name: '环境-DEMO-05', amazon_customer_id: 'A2TEST00005', daily_limit: 20, min_amount: '0.00' })
    expect(created.status).toBe(200)
    expect(created.body).toMatchObject({ name: '环境-DEMO-05', amazon_customer_id: 'A2TEST00005' })
    expect(created.body.browsers.map((b) => b.id)).toEqual([10])
    expect(created.body).not.toHaveProperty('devices')
    expect(created.body).not.toHaveProperty('default_device_id')
    expect(call<{ items: DiscoveredBuyer[] }>('GET', '/accounts/discovered?include_ignored=0').body.items.map((x) => x.customer_id)).toEqual(['A2TEST00004'])
    const dev = call<Page<Device>>('GET', '/devices').body.rows.find((d) => d.id === 10)!
    expect(dev.login.state).toBe('ready')
    expect(dev.account).toEqual({ id: created.body.id, name: '环境-DEMO-05' })
    expect(call<Overview>('GET', '/overview').body.discovered_buyers).toBe(1)
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
    expect(call<{ items: DiscoveredBuyer[] }>('GET', '/accounts/discovered?include_ignored=0').body.items.map((x) => x.customer_id)).toEqual(['A2TEST00005'])
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
    expect(call<{ items: DiscoveredBuyer[] }>('GET', '/accounts/discovered?include_ignored=0').body.items.map((x) => x.customer_id)).toEqual(['A2TEST00004'])
    expect(call<Page<Device>>('GET', '/devices').body.rows.find((d) => d.id === 10)!.login.state).toBe('ignored')
    expect(call('POST', '/accounts/discovered/unignore', { customer_id: 'A2TEST00005' }).body).toEqual({ ok: true })
    expect(call<Page<Device>>('GET', '/devices').body.rows.find((d) => d.id === 10)!.login.state).toBe('unregistered')
    expect(call('POST', '/accounts/discovered/ignore', { customer_id: 'bad' }).status).toBe(422)
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
