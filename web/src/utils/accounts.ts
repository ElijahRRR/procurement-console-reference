/**
 * 账号相关的展示规则（纯函数）。
 *
 * 账号输出带 pause_cause（result_unknown / account_error / manual，未暂停为 null）与 open_result_unknown
 * （这个账号当前处在「结果待核实」的任务数，services/accounts）。执行模块因「已登记提交后没有确认结果」暂停账号时，
 * 暂停原因写成「结果待核实：<平台订单号>」（services/execution）；旧服务端没有 pause_cause 时按这个前缀判断。
 */
import type { Account, DiscoveredBuyer } from '@/api/types'

/** 暂停原因的前缀：结果待核实（铁律 9：已登记提交的非成功结局一律暂停账号，人工核实后才恢复） */
export const RESULT_UNKNOWN_PAUSE_PREFIX = '结果待核实'

/** 输入：账号的暂停原因 → 输出：是否因结果待核实而暂停 */
export function isResultUnknownPause(reason: string | null | undefined): boolean {
  return !!reason && reason.trim().startsWith(RESULT_UNKNOWN_PAUSE_PREFIX)
}

export type ResumeInfo = Pick<Account, 'name'> & Partial<Pick<Account, 'pause_reason' | 'pause_cause' | 'open_result_unknown'>>

/** 输入：账号 → 输出：该账号仍为「结果待核实」的任务数；服务端没给为 null */
function openUnknown(a: ResumeInfo): number | null {
  return typeof a.open_result_unknown === 'number' && Number.isFinite(a.open_result_unknown) ? a.open_result_unknown : null
}

/** 输入：账号 → 输出：是否因结果待核实暂停，或还有结果待核实的任务（恢复确认框要醒目提醒） */
export function resumeNeedsCaution(a: ResumeInfo): boolean {
  const cause = a.pause_cause === undefined ? (isResultUnknownPause(a.pause_reason) ? 'result_unknown' : null) : a.pause_cause
  return cause === 'result_unknown' || (openUnknown(a) ?? 0) > 0
}

/**
 * 输入：账号（名称、暂停原因、pause_cause、open_result_unknown）→ 输出：恢复执行的确认文案（「确定要……吗？」）。
 * 还有结果待核实的任务时写明数量，提醒先在亚马逊订单页核实并登记结果；因结果待核实暂停而任务都已核实时说明；
 * 账号级错误暂停时提醒先在浏览器里处理好。
 */
export function resumeConfirmText(a: ResumeInfo): string {
  const head = `确定要恢复「${a.name}」的执行吗？`
  const open = openUnknown(a)
  if (open !== null && open > 0) {
    return `${head}该账号还有 ${open} 笔结果待核实的任务：请先在亚马逊订单页核实并登记结果。恢复执行不会让这些任务被再次领取，但未核实前可能重复下单的风险仍在。`
  }
  const cause = a.pause_cause === undefined ? (isResultUnknownPause(a.pause_reason) ? 'result_unknown' : null) : a.pause_cause
  if (cause === 'result_unknown') {
    return open === null
      ? `${head}暂停原因是结果待核实，请先确认该账号的结果待核实任务都已核实。`
      : `${head}暂停时的结果待核实任务都已核实。`
  }
  if (cause === 'account_error') {
    const why = (a.pause_reason ?? '').trim()
    return `${head}暂停原因是账号级错误${why ? `（${why}）` : ''}，请先在浏览器里处理好（如重新登录、解除受限）。`
  }
  return head
}

// ── 买家号 ID 与待登记的买家号 ────────────────────────────────────────────────

/** 买家号 ID（亚马逊 customerId）的形状：A 开头，后面至少 8 位大写字母或数字（与 services/accounts.CUSTOMER_ID_RE 相同） */
export const CUSTOMER_ID_RE = /^A[0-9A-Z]{8,}$/

/** 输入：输入框里的买家号 ID → 输出：去掉空白并转大写；空为 null（留空 = 不登记，不当作空串写入） */
export function normalizeCustomerId(v: string | null | undefined): string | null {
  const s = (v ?? '').replace(/\s+/g, '').toUpperCase()
  return s === '' ? null : s
}

/** 输入：输入框里的买家号 ID → 输出：问题说明；留空或形状合格为空串（留空 = 不登记） */
export function customerIdProblem(v: string | null | undefined): string {
  const id = normalizeCustomerId(v)
  return id !== null && !CUSTOMER_ID_RE.test(id) ? '买家号 ID 以 A 开头，后面至少 8 位字母或数字' : ''
}

/** 整理后的待登记项：source_orders 一定是数字（服务端没给按 0） */
export type DiscoveredItem = DiscoveredBuyer & { source_orders: number }

export interface DiscoveredSplit {
  /** 等待登记的（没有忽略的），最近上报的在前；只来自来源指定、没有浏览器上报过的排在后面 */
  pending: DiscoveredItem[]
  /** 已忽略的（忽略名单里的全部 ID，不要求此刻有浏览器登录着），排序同上 */
  ignored: DiscoveredItem[]
}

/** 输入：来源指定的任务数 → 输出：非负整数；缺失或不是数字为 0 */
function sourceOrders(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) && v > 0 ? Math.floor(v) : 0
}

/** 输入：时刻 → 输出：排序用的毫秒数；空或解析不了排到最后 */
function reportedMs(v: string | null | undefined): number {
  const t = v ? Date.parse(v) : NaN
  return Number.isNaN(t) ? -Infinity : t
}

/**
 * 输入：GET /accounts/discovered 的条目（可含已忽略的）→ 输出：待登记与已忽略两组。
 * 没有 customer_id 的条目丢掉；同一个 ID 只留一条（合并浏览器，取较新的上报时间、较大的来源指定任务数）；
 * 每组按最近上报时间倒序，没有浏览器上报过的（只来自来源指定）排在后面，其中指定任务多的在前。
 */
export function splitDiscovered(items: readonly DiscoveredBuyer[] | null | undefined): DiscoveredSplit {
  const byId = new Map<string, DiscoveredItem>()
  for (const raw of items ?? []) {
    const id = normalizeCustomerId(raw?.customer_id)
    if (!id) continue
    const item: DiscoveredItem = {
      customer_id: id,
      ignored: !!raw.ignored,
      browsers: [...(raw.browsers ?? [])],
      last_reported_at: raw.last_reported_at ?? null,
      name_hint: raw.name_hint?.trim() || null,
      source_orders: sourceOrders(raw.source_orders),
    }
    const prev = byId.get(id)
    if (!prev) {
      byId.set(id, item)
      continue
    }
    for (const b of item.browsers) if (!prev.browsers.some((x) => x.id === b.id)) prev.browsers.push(b)
    if (reportedMs(item.last_reported_at) > reportedMs(prev.last_reported_at)) prev.last_reported_at = item.last_reported_at
    prev.ignored = prev.ignored && item.ignored
    prev.name_hint = prev.name_hint ?? item.name_hint
    prev.source_orders = Math.max(prev.source_orders, item.source_orders)
  }
  const sorted = [...byId.values()].sort((a, b) => {
    const ta = reportedMs(a.last_reported_at)
    const tb = reportedMs(b.last_reported_at)
    // 两边都没有上报时间时不能相减（-Infinity − -Infinity 是 NaN）
    if (ta !== tb) return tb > ta ? 1 : -1
    return b.source_orders - a.source_orders || a.customer_id.localeCompare(b.customer_id)
  })
  return { pending: sorted.filter((x) => !x.ignored), ignored: sorted.filter((x) => x.ignored) }
}

/** 没有浏览器登录着这个买家号时「所在浏览器」一栏的说法（只来自来源指定的买家号就是这样） */
export const NO_BROWSER_TEXT = '还没有浏览器登录这个买家号'

/** 输入：待登记的买家号 → 输出：「来源指定了 3 笔任务」；没有来源指定的任务为空串 */
export function sourceOrdersText(item: Pick<DiscoveredBuyer, 'source_orders'> | null | undefined): string {
  const n = sourceOrders(item?.source_orders)
  return n > 0 ? `来源指定了 ${n} 笔任务` : ''
}

/**
 * 输入：待登记的买家号（有没有浏览器登录着、来源指定了几笔任务）→ 输出：把 ID 登记到账号之后的提示：
 * 有浏览器登录着 → 它现在可以领取任务；来源指定了任务 → 这些任务会按指定重新分配。都没有为空串。
 */
export function registeredEffectText(item: Pick<DiscoveredBuyer, 'browsers' | 'source_orders'> | null | undefined): string {
  const parts: string[] = []
  if (item?.browsers?.length) parts.push('登录着这个买家号的浏览器现在可以领取它的任务')
  const n = sourceOrders(item?.source_orders)
  if (n > 0) parts.push(`来源指定给它的 ${n} 笔任务会按指定重新分配`)
  return parts.length ? `${parts.join('；')}。` : ''
}

export interface RegisterSource {
  /** 来源指定了这个买家号 ID 的未采购任务数 */
  sourceOrders?: number | null
  /** 有没有浏览器登录着这个买家号；不给时按有（从「浏览器」页签点的登记） */
  hasBrowser?: boolean
}

/**
 * 输入：要登记的买家号从哪里来 → 输出：登记对话框里买家号 ID 下面的说明。
 * 插件读到的：登记后登录着它的浏览器就能领取任务；来源指定的：登记后这些任务按指定重新分配，没有浏览器登录时写明还领不了。
 */
export function registerHelpText(src: RegisterSource | null | undefined): string {
  const n = sourceOrders(src?.sourceOrders)
  const tail = '请确认名称、金额区间与每日单数。'
  if (src?.hasBrowser === false && n > 0) {
    return `这个买家号 ID 是来源指定的，还没有浏览器登录这个买家号。登记后，来源指定给它的 ${n} 笔任务会按指定重新分配；登录着这个买家号的浏览器装上插件后才能领取。${tail}`
  }
  const designated = n > 0 ? `来源指定给它的 ${n} 笔任务会按指定重新分配；` : ''
  return `买家号 ID 由插件从浏览器登录的亚马逊页面读出。登记后，登录着这个买家号的浏览器就能领取这个账号的任务；${designated}${tail}`
}

/**
 * 输入：账号列表、待登记的买家号 → 输出：可以「填到已有账号」的账号：还没登记买家号 ID 的。
 * 名称与来源里这个 ID 用过的「买家号」名称相同的排在最前（多半就是它），其余保持列表顺序。
 */
export function fillTargets(accounts: readonly Account[], item: Pick<DiscoveredBuyer, 'name_hint'> | null | undefined): Account[] {
  const open = accounts.filter((a) => !a.amazon_customer_id)
  const hint = item?.name_hint?.trim()
  if (!hint) return open
  return [...open.filter((a) => a.name === hint), ...open.filter((a) => a.name !== hint)]
}

/** 输入：账号列表、待登记的买家号 → 输出：与来源里的名称同名、还没登记 ID 的账号（没有为 null） */
export function suggestedFillTarget(accounts: readonly Account[], item: Pick<DiscoveredBuyer, 'name_hint'> | null | undefined): Account | null {
  const hint = item?.name_hint?.trim()
  if (!hint) return null
  return accounts.find((a) => !a.amazon_customer_id && a.name === hint) ?? null
}

/** 输入：待登记的买家号、目标账号 → 输出：「填到已有账号」的确认文案 */
export function fillConfirmText(customerId: string, accountName: string): string {
  return `确定要把买家号 ID ${customerId} 登记到「${accountName}」吗？`
}

/** 「忽略」的确认文案 */
export const IGNORE_CONFIRM_TEXT = '确定要忽略这个买家号吗？忽略后它不再出现在待登记列表里；之后可以在已忽略的买家号里恢复。'

/** 输入：待登记的买家号个数（插件读到的与来源指定的合在一起）→ 输出：概览上的提示（没有为空串） */
export function discoveredNotice(count: number | null | undefined): string {
  const n = typeof count === 'number' && Number.isFinite(count) ? Math.floor(count) : 0
  return n > 0 ? `有 ${n} 个还没登记的买家号（插件读到的或来源指定的），登记后才会分到任务` : ''
}

// ── 页签 ──────────────────────────────────────────────────────────────────────

export type AccountsTab = 'accounts' | 'browsers'

/** 输入：地址里的 ?tab= 取值 → 输出：页签；旧链接的 devices 落到「浏览器」，其余落到「账号」 */
export function accountsTab(v: unknown): AccountsTab {
  const s = Array.isArray(v) ? v[0] : v
  return s === 'browsers' || s === 'devices' ? 'browsers' : 'accounts'
}
