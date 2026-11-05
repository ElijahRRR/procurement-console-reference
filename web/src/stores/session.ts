/**
 * 会话状态：是否需要首次设置、当前用户。路由守卫在第一次导航时加载，登录 / 退出时更新。
 * 只用 Vue 的 reactive，不引入状态库。
 */
import { reactive } from 'vue'
import * as auth from '@/api/auth'
import type { Me } from '@/api/types'

export const session = reactive({
  loaded: false,
  needsSetup: false,
  me: null as Me | null,
})

let loading: Promise<void> | null = null

/** 输入：是否强制重新加载 → 输出：无（填好 session.needsSetup 与 session.me） */
export function ensureSession(force = false): Promise<void> {
  if (session.loaded && !force) return Promise.resolve()
  if (loading && !force) return loading
  loading = (async () => {
    try {
      const state = await auth.getAuthState()
      session.needsSetup = !!state?.needs_setup
    } catch {
      // 服务端不可达时按「已完成设置」处理，交给登录页显示错误
      session.needsSetup = false
    }
    if (session.needsSetup) {
      session.me = null
    } else {
      try {
        session.me = await auth.getMe()
      } catch {
        // 401 = 未登录；其他错误也按未登录处理，登录页会再次报告连接问题
        session.me = null
      }
    }
    session.loaded = true
    loading = null
  })()
  return loading
}

/** 输入：登录 / 设置成功返回的当前用户 → 输出：无 */
export function setMe(me: Me | null): void {
  session.me = me
  session.needsSetup = false
  session.loaded = true
}

/** 输入：无 → 输出：显示用的用户名（显示名优先） */
export function displayName(me: Me | null): string {
  if (!me) return ''
  return me.user.display_name || me.user.username
}
