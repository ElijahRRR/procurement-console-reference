/**
 * 路由守卫的判断逻辑（纯函数，tests/unit/guard.test.ts 覆盖）：
 * 首次设置未完成 → /setup；未登录访问受保护页面 → /login?redirect=…；已登录访问登录页 → 回到原处或概览。
 */
import type { Me } from '@/api/types'

export interface GuardState {
  needsSetup: boolean
  me: Me | null
}

export interface GuardRoute {
  path: string
  fullPath: string
  query?: Record<string, unknown>
  meta?: { public?: boolean }
}

export type GuardResult = true | { path: string; query?: Record<string, string> }

/** 输入：登录后要回到的地址 → 输出：站内路径（只接受以单个 / 开头的路径，防止跳到外站） */
export function safeRedirect(raw: unknown): string {
  const s = Array.isArray(raw) ? raw[0] : raw
  if (typeof s !== 'string') return '/'
  if (!s.startsWith('/') || s.startsWith('//') || s.startsWith('/\\')) return '/'
  if (s.startsWith('/login') || s.startsWith('/setup')) return '/'
  return s
}

/** 输入：目标路由、会话状态 → 输出：true 放行，或要跳转的地址 */
export function decideRoute(to: GuardRoute, state: GuardState): GuardResult {
  if (state.needsSetup) {
    return to.path === '/setup' ? true : { path: '/setup' }
  }
  if (to.path === '/setup') {
    return state.me ? { path: '/' } : { path: '/login' }
  }
  if (to.meta?.public) {
    if (state.me && to.path === '/login') return { path: safeRedirect(to.query?.redirect) }
    return true
  }
  if (!state.me) {
    return to.fullPath && to.fullPath !== '/'
      ? { path: '/login', query: { redirect: to.fullPath } }
      : { path: '/login' }
  }
  return true
}
