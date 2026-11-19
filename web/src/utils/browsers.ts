/**
 * 浏览器（装了插件的浏览器环境，接口里叫 device）的展示规则（纯函数）。
 *
 * 「当前登录」一栏的文字按三端统一的状态表：还没有上报 / 未登录亚马逊 / 已登录，没读到买家号 ID / 登录状态不一致 /
 * 未登记的买家号 <ID> / 已忽略的买家号 <ID> / <账号名>（账号暂停执行时加「（已暂停）」）。
 */
import type { BrowserRef, Device, LoginState } from '@/api/types'
import { EMPTY, formatRelative } from './format'
import { LOGIN_STATE_DOT, LOGIN_STATE_LABEL, LOGIN_STATE_TAG, type DotColor, type TagType } from './labels'

export type LoginInfo = Pick<Device, 'login' | 'account'>

export interface BrowserLoginView {
  state: LoginState | string
  /** 不含买家号 ID 的说法：「未登记的买家号」「未登录亚马逊」；ready 时是账号名（暂停时带「（已暂停）」） */
  label: string
  /** 要紧跟在 label 后面显示的买家号 ID（未登记、已忽略时）；其余为 null */
  customerId: string | null
  /** 完整的一句：label + 空格 + customerId */
  text: string
  dot: DotColor
  tag: TagType
  /** 是否给「登记」入口（只有未登记且读到了 ID） */
  canRegister: boolean
  /** ready 且对应账号已暂停执行 */
  paused: boolean
}

/**
 * 输入：浏览器的 login 与 account、已暂停执行的账号 id 集合（服务端没在 account 上给 execution_paused 时用）
 * → 输出：「当前登录」一栏的文字、状态点与标签色。未知的 state 原样显示，不当作已登记。
 */
export function browserLoginView(d: LoginInfo, pausedAccountIds?: ReadonlySet<number> | null): BrowserLoginView {
  const state = d.login?.state ?? 'unknown'
  const id = d.login?.customer_id?.trim() || null
  const known = state in LOGIN_STATE_LABEL
  const base = {
    state,
    dot: known ? LOGIN_STATE_DOT[state as LoginState] : ('gray' as DotColor),
    tag: known ? LOGIN_STATE_TAG[state as LoginState] : ('info' as TagType),
    canRegister: false,
    paused: false,
  }
  if (state === 'ready') {
    const acc = d.account
    if (!acc) return { ...base, label: LOGIN_STATE_LABEL.ready, customerId: id, text: id ? `${LOGIN_STATE_LABEL.ready} ${id}` : LOGIN_STATE_LABEL.ready }
    const paused = acc.execution_paused ?? pausedAccountIds?.has(acc.id) ?? false
    const label = paused ? `${acc.name}（已暂停）` : acc.name
    return { ...base, label, customerId: null, text: label, paused, dot: paused ? 'red' : base.dot, tag: paused ? 'danger' : base.tag }
  }
  if (state === 'unregistered' || state === 'ignored') {
    const label = LOGIN_STATE_LABEL[state]
    return { ...base, label, customerId: id, text: id ? `${label} ${id}` : label, canRegister: state === 'unregistered' && !!id }
  }
  const label = known ? LOGIN_STATE_LABEL[state as LoginState] : String(state)
  return { ...base, label, customerId: null, text: label }
}

/** 输入：同 browserLoginView → 输出：完整的一句（「未登记的买家号 A1DEMO00000」「环境-A（已暂停）」） */
export function browserLoginText(d: LoginInfo, pausedAccountIds?: ReadonlySet<number> | null): string {
  return browserLoginView(d, pausedAccountIds).text
}

/**
 * 输入：浏览器的 login、当前时刻 → 输出：上报时间的说明（「2 分钟前上报」；太久没有上报时「3 小时前上报，已过期」）；
 * 从未上报为空串。过期的登录状态不能用来开始或继续执行，打开或刷新亚马逊页面后插件会重新上报。
 */
export function loginReportedText(login: Device['login'] | null | undefined, now: Date = new Date()): string {
  if (!login?.at) return ''
  const when = formatRelative(login.at, now)
  if (when === EMPTY) return ''
  // formatRelative：刚刚 / 即将（时钟有偏差）/ N 分钟前 / N 小时前 / N 天前 / 更早给日期
  let base: string
  if (when === '刚刚' || when === '即将') base = '刚刚上报'
  else if (when.endsWith('前') || when.endsWith('后')) base = `${when}上报`
  else base = `${when} 上报`
  return login.stale ? `${base}，已过期` : base
}

/** 输入：浏览器（在线标记与最近联系时间）、当前时刻 → 输出：「在线」或「离线 · 5 小时前」；从未联系为「离线」 */
export function browserOnlineText(b: Pick<BrowserRef, 'online' | 'last_seen_at'>, now: Date = new Date()): string {
  if (b.online) return '在线'
  return b.last_seen_at ? `离线 · ${formatRelative(b.last_seen_at, now)}` : '离线'
}

/** 输入：浏览器列表 → 输出：没有移除的浏览器里 {在线数, 总数} */
export function browserCounts(list: readonly Pick<Device, 'online' | 'hidden_at'>[]): { online: number; total: number } {
  const shown = list.filter((d) => !d.hidden_at)
  return { online: shown.filter((d) => d.online).length, total: shown.length }
}
