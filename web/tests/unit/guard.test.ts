import { describe, expect, it } from 'vitest'
import { decideRoute, safeRedirect, type GuardState } from '@/router/guard'
import type { Me } from '@/api/types'

const me: Me = { user: { id: 1, username: 'owner', display_name: 'DEMO 所有者' }, workspace: { id: 1, name: 'DEMO 工作区' }, role: 'owner' }
const loggedIn: GuardState = { needsSetup: false, me }
const anonymous: GuardState = { needsSetup: false, me: null }
const fresh: GuardState = { needsSetup: true, me: null }

const route = (path: string, extra: Partial<{ fullPath: string; query: Record<string, unknown>; public: boolean }> = {}) => ({
  path,
  fullPath: extra.fullPath ?? path,
  query: extra.query ?? {},
  meta: { public: extra.public ?? (path === '/login' || path === '/setup') },
})

describe('路由守卫', () => {
  it('尚未完成首次设置时一律去 /setup', () => {
    expect(decideRoute(route('/'), fresh)).toEqual({ path: '/setup' })
    expect(decideRoute(route('/login'), fresh)).toEqual({ path: '/setup' })
    expect(decideRoute(route('/orders'), fresh)).toEqual({ path: '/setup' })
    expect(decideRoute(route('/setup'), fresh)).toBe(true)
  })
  it('设置完成后不能再进 /setup', () => {
    expect(decideRoute(route('/setup'), anonymous)).toEqual({ path: '/login' })
    expect(decideRoute(route('/setup'), loggedIn)).toEqual({ path: '/' })
  })
  it('未登录访问受保护页面跳登录并带回跳地址', () => {
    expect(decideRoute(route('/orders', { fullPath: '/orders?view=today' }), anonymous)).toEqual({
      path: '/login',
      query: { redirect: '/orders?view=today' },
    })
    expect(decideRoute(route('/'), anonymous)).toEqual({ path: '/login' })
  })
  it('未登录可以打开登录页', () => {
    expect(decideRoute(route('/login'), anonymous)).toBe(true)
  })
  it('已登录打开登录页回到 redirect 或概览', () => {
    expect(decideRoute(route('/login', { query: { redirect: '/accounts?tab=devices' } }), loggedIn)).toEqual({ path: '/accounts?tab=devices' })
    expect(decideRoute(route('/login'), loggedIn)).toEqual({ path: '/' })
  })
  it('已登录访问受保护页面放行', () => {
    expect(decideRoute(route('/orders'), loggedIn)).toBe(true)
    expect(decideRoute(route('/settings'), loggedIn)).toBe(true)
  })
})

describe('回跳地址只接受站内路径', () => {
  it.each([
    ['/orders', '/orders'],
    ['/orders?view=today', '/orders?view=today'],
    ['https://evil.example.com', '/'],
    ['//evil.example.com', '/'],
    ['/\\evil.example.com', '/'],
    ['/login?redirect=/x', '/'],
    [undefined, '/'],
    [42, '/'],
  ])('%s → %s', (raw, expected) => {
    expect(safeRedirect(raw)).toBe(expected)
  })
  it('数组取第一个', () => {
    expect(safeRedirect(['/records', '/x'])).toBe('/records')
  })
})
