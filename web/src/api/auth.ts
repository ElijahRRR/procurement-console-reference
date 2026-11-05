/** 认证：首次设置、登录、退出、当前用户（契约 §8）。 */
import { get, post } from './client'
import type { AuthState, Me } from './types'

/** 输入：无 → 输出：{needs_setup}（尚无任何用户时为 true） */
export const getAuthState = () => get<AuthState>('/auth/state', undefined, { silent: true })

/** 输入：所有者用户名、密码、工作区名 → 输出：当前用户（创建后即登录） */
export const setup = (body: { username: string; password: string; workspace_name: string }) =>
  post<Me>('/auth/setup', body, { silent: true })

/** 输入：用户名、密码 → 输出：当前用户 */
export const login = (body: { username: string; password: string }) => post<Me>('/auth/login', body, { silent: true })

/** 输入：无 → 输出：无（清除会话 Cookie） */
export const logout = () => post<null>('/auth/logout', {}, { silent: true })

/** 输入：无 → 输出：当前用户与工作区；未登录抛 401（silent，不跳转） */
export const getMe = () => get<Me>('/auth/me', undefined, { silent: true })
