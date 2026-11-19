/**
 * 来源指定买家号的展示规则与文案（纯函数，tests/unit/designation.test.ts 覆盖）。
 *
 * 来源（飞书表格或上传的表格）的一行没有可信的 AMZ 订单号、又填了「买家号 ID」或「买家号」时，这一行指定了采购账号；
 * 任务行与详情用 designated_buyer: {customer_id, name, account} 给出（两列都空为 null，account 是此刻解析到的未归档账号）。
 * 优先级：人工改派 > 来源指定 > 自动分配。只在任务有指定时才出现的句子都从这里取：没有指定时得到原来的文案或空串。
 */
import type { DesignatedBuyer, OrderRow } from '@/api/types'

type WithDesignation = Pick<OrderRow, 'designated_buyer'>
type WithAssignment = Pick<OrderRow, 'designated_buyer' | 'assigned_account' | 'assignment_kind'>

/** 输入：任意值 → 输出：去掉首尾空白的字符串；不是字符串或为空返回 null */
function text(v: unknown): string | null {
  const s = typeof v === 'string' ? v.trim() : ''
  return s === '' ? null : s
}

/**
 * 输入：任务行或详情 → 输出：来源指定的买家号；没有指定为 null。
 * 字段缺失（旧服务端）、整项为 null、两列都空，都算没有指定。
 */
export function designationOf(o: WithDesignation | null | undefined): DesignatedBuyer | null {
  const d = o?.designated_buyer
  if (!d || typeof d !== 'object') return null
  const customer_id = text(d.customer_id)
  const name = text(d.name)
  if (!customer_id && !name) return null
  return { customer_id, name, account: d.account }
}

/** 输入：任务行或详情 → 输出：来源有没有给它指定买家号 */
export function hasDesignation(o: WithDesignation | null | undefined): boolean {
  return designationOf(o) !== null
}

/**
 * 输入：任务行或详情 → 输出：只要一个短标签时的「名或 ID」：有买家号（名称）用名称，否则用买家号 ID；没有指定为空串。
 * 确认框与改派说明不用它（两列都填时对应账号的是 ID），改用 designationText 的「名称（ID）」。
 */
export function designationName(o: WithDesignation | null | undefined): string {
  const d = designationOf(o)
  return d ? (d.name ?? d.customer_id ?? '') : ''
}

/** 输入：任务行或详情 → 输出：「环境-DEMO-A（A1DEMO00000）」，缺哪个省哪个；没有指定为空串 */
export function designationText(o: WithDesignation | null | undefined): string {
  const d = designationOf(o)
  if (!d) return ''
  if (d.name && d.customer_id) return `${d.name}（${d.customer_id}）`
  return d.name ?? d.customer_id ?? ''
}

/**
 * 输入：任务行或详情 → 输出：人工改派到的账号名，只在「有指定、当前是人工改派、改派的账号不是来源指定的那个」时给出，其余为空串。
 * 是不是同一个账号按服务端解析到的 account 判断（解析不出也算不是同一个）；旧服务端不给 account 时退回按名称比。
 */
export function manualOverrideAccount(o: WithAssignment | null | undefined): string {
  const d = designationOf(o)
  const assigned = o?.assigned_account
  if (!d || !assigned || o?.assignment_kind !== 'manual') return ''
  const same = d.account !== undefined ? d.account !== null && d.account.id === assigned.id : d.name !== null && d.name === assigned.name.trim()
  return same ? '' : assigned.name
}

/**
 * 输入：任务详情 → 输出：详情「分配」区「来源指定的买家号」一行的内容：「名（ID）」，
 * 当前是人工改派到别的账号时后面加「· 已人工改派到 B」；没有指定为空串（这一行不显示）。
 */
export function designationRowText(o: WithAssignment | null | undefined): string {
  const base = designationText(o)
  if (!base) return ''
  const other = manualOverrideAccount(o)
  if (!other) return base
  return `${base}${base.endsWith('）') ? '' : ' '}· 已人工改派到 ${other}`
}

// ── 文案 ──────────────────────────────────────────────────────────────────────

/** 「重新分配」的确认文案（概览、待采购页共用） */
export const REALLOCATE_CONFIRM_TEXT =
  '确定要重新分配吗？来源指定了买家号的任务按指定分配，其余自动分配的任务按当前账号配置重算，人工改派的任务保持不动。'

/** 「待分配」标签页的空状态说明 */
export const UNASSIGNED_EMPTY_TEXT =
  '分不出去的任务会写明原因：金额不在任何启用账号的区间内、匹配账号当天额度已满，或来源指定的买家号暂时接不了。'

/**
 * 两个字段对应向导（飞书、文件）帮助段落里关于买家号两列的一句。两列都填而买家号对应到另一个账号时 ID 并不「胜出」：
 * 任务停在待分配，写明两边不是同一个账号（方案 v2「两列都填以 ID 为准，名称指向另一个账号时不分配」）。
 */
export const DESIGNATION_MAPPING_HELP =
  '「买家号」「买家号 ID」填了，这笔订单就指定给对应的账号（买家号按账号名称对应，买家号 ID 按账号登记的 ID 对应；都填时以 ID 为准，买家号对应到另一个账号时不分配，停在待分配）；留空由系统自动分配。已有 AMZ 订单号的行，这两列只当作历史采购记录。'

/**
 * 文件导入向导多一句：每次上传都是一批新的行，空格子读不出「以前填过、现在清掉了」，所以已导入过的订单空着重新上传
 * 不会清除指定（飞书来源清空格子会清除，飞书向导不加这一句）。
 */
export const FILE_DESIGNATION_NOTE = '已导入过的订单，这两列空着重新上传不会清除指定。'

/** 文件导入向导字段对应一步的买家号两列说明 = 共用的一句 + 文件独有的一句 */
export const FILE_DESIGNATION_MAPPING_HELP = `${DESIGNATION_MAPPING_HELP}${FILE_DESIGNATION_NOTE}`

/** 账号对话框「账号名称」的帮助文字 */
export const ACCOUNT_NAME_HELP = '来源按「买家号」指定时靠名称对应账号，改名后这些任务会回到待分配。'

/** 没有指定的任务的「解除分配」确认文案 */
export const UNASSIGN_CONFIRM_TEXT = '确定要解除这笔任务的账号分配吗？任务会回到待分配。'

/**
 * 输入：任务详情 → 输出：「解除分配」的确认文案。来源指定了买家号时说明下次分配仍按来源指定：这句话对「指定的账号接得了」
 * 与「指定解析不到账号 / 接不了」两种情况都成立（不承诺一定分给它）；引用「名称（ID）」，两列都填时 ID 才是对应账号的依据。
 */
export function unassignConfirmText(o: WithDesignation | null | undefined): string {
  const text = designationText(o)
  if (!text) return UNASSIGN_CONFIRM_TEXT
  return `确定要解除这笔任务的账号分配吗？来源指定了买家号「${text}」，下次分配仍按来源指定：接得了就分给它，接不了就停在待分配并写明原因；要换账号请用「改派账号」。`
}

/** 输入：任务详情 → 输出：改派对话框里放在原说明之前的一段（引用「名称（ID）」）；没有指定为空串 */
export function assignDesignationNote(o: WithDesignation | null | undefined): string {
  const text = designationText(o)
  if (!text) return ''
  return `来源给这笔任务指定了买家号「${text}」。改派后以改派为准；任务留在新账号期间不再跟随来源的指定，回到待分配后仍按来源指定分配。`
}
