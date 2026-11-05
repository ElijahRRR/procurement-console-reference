/**
 * 任务详情抽屉的展示规则（纯函数）：状态说明的提示颜色、缺失字段提示是否与状态说明重复、事件摘要、
 * 核实结果对话框的预填与单号校验、按冲突种类给出的处理按钮。
 * 颜色表达含义（设计系统）：成功为绿、进行中为蓝灰、需要人处理为黄、出错与待核实为红。
 */
import type { Attempt, EventRecord, OrderConflict, OrderDetail, OrderRow, OrderStatus } from '@/api/types'
import { displayValue, formatMoney, parseAmount } from './format'
import {
  CONFLICT_KIND_LABEL,
  FEISHU_VERDICT_LABEL,
  GUARD_CODE_LABEL,
  ORDER_STATUS_LABEL,
  PROGRESS_CANCEL_LABEL,
  SOURCE_KIND_LABEL,
  errorCodeLabel,
  fieldLabel,
  labelOf,
} from './labels'

export type AlertType = 'success' | 'info' | 'warning' | 'error'

export const STATUS_ALERT_TYPE: Record<OrderStatus, AlertType> = {
  purchased: 'success',
  queued: 'info',
  assigned: 'info',
  executing: 'info',
  cancelled: 'info',
  closed: 'info',
  pending_review: 'warning',
  needs_info: 'warning',
  repurchase_pending: 'warning',
  conflict: 'error',
  exception: 'error',
  result_unknown: 'error',
}

/** 输入：任务状态 → 输出：状态说明（status_reason）用的 el-alert 类型 */
export function statusAlertType(status: OrderStatus | string): AlertType {
  return (STATUS_ALERT_TYPE as Record<string, AlertType>)[status] ?? 'info'
}

/** 输入：任务 → 输出：缺失字段的中文提示「缺少执行所需字段：采购单价、限价」，没有缺失为空串 */
export function missingFieldsText(o: Pick<OrderRow, 'missing_fields'>): string {
  return o.missing_fields?.length ? `缺少执行所需字段：${o.missing_fields.map(fieldLabel).join('、')}` : ''
}

/**
 * 输入：任务 → 输出：是否单独显示状态说明。资料待补齐时状态说明就是「缺少：…」，与结构化的缺失字段提示重复，
 * 这时只显示后者；状态说明里还有别的内容（如来源变化）时照常显示。
 */
export function showStatusReason(o: Pick<OrderRow, 'status' | 'status_reason' | 'missing_fields'>): boolean {
  const reason = (o.status_reason ?? '').trim()
  if (!reason) return false
  if (!o.missing_fields?.length) return true
  const labels = o.missing_fields.map(fieldLabel)
  // 去掉「缺少」前缀与全部缺失字段名、分隔符后，没有剩下别的内容 → 重复
  let rest = reason.replace(/^缺少(执行所需字段)?[:：]?/, '')
  for (const l of labels) rest = rest.split(l).join('')
  return rest.replace(/[、，,；;。\s]/g, '') !== ''
}

// ── 事件摘要 ──────────────────────────────────────────────────────────────────

/** 事件 detail 里直接显示的文字键（按顺序） */
const EVENT_TEXT_KEYS = ['reason', 'cause', 'note', 'message', 'account_name', 'amazon_order_no'] as const
/** 摘要里另行处理、不再按「键：值」列出的键 */
const EVENT_HANDLED_KEYS = new Set<string>([
  ...EVENT_TEXT_KEYS, 'from', 'to', 'source', 'status', 'verdict', 'changes', 'fields', 'notes', 'error_code', 'code', 'reasons',
  'stage', 'continue', 'cancel_reason', 'cancel_code', 'detail',
])

/** 输入：事件里的来源描述 {kind, connection_id, record_id} / {kind, batch_id, row_no} → 输出：「飞书记录 recX」「文件批次 3 第 6 行」 */
export function eventSourceText(src: unknown): string {
  if (!src || typeof src !== 'object') return ''
  const o = src as Record<string, unknown>
  if (o.kind === 'feishu') return o.record_id ? `飞书记录 ${String(o.record_id)}` : '飞书记录'
  if (o.kind === 'file') {
    const batch = o.batch_id !== null && o.batch_id !== undefined ? `文件批次 ${String(o.batch_id)}` : '文件'
    return o.row_no !== null && o.row_no !== undefined ? `${batch} 第 ${String(o.row_no)} 行` : batch
  }
  return ''
}

/** 输入：{字段: [旧值, 新值]} → 输出：「数量 1 → 2；限价 $35.00 → $30.00」 */
function changesText(v: unknown): string {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return ''
  return Object.entries(v as Record<string, unknown>)
    .map(([k, pair]) => {
      const [a, b] = Array.isArray(pair) ? pair : [undefined, pair]
      const show = (x: unknown) => (MONEY_KEYS.has(k) ? formatMoney(x) : displayValue(x))
      return `${fieldLabel(k)} ${show(a)} → ${show(b)}`
    })
    .join('；')
}
const MONEY_KEYS = new Set(['unit_price', 'price_cap', 'sales_total'])

/**
 * 输入：事件 → 输出：时间线里的一行摘要：状态变化、来源、预览结论、字段变化、叫停原因（cancel_code 的中文）、错误码与护栏码中文、
 * 原因与备注；剩下的简单值按「字段：值」列出（对象不原样打印 JSON）。
 */
export function eventSummary(e: Pick<EventRecord, 'kind' | 'detail'>): string {
  const d = e.detail ?? {}
  const parts: string[] = []
  const push = (s: string) => {
    if (s && !parts.some((p) => p.includes(s))) parts.push(s)
  }
  const from = typeof d.from === 'string' ? d.from : null
  const to = typeof d.to === 'string' ? d.to : null
  if (from && to) push(`${labelOf(ORDER_STATUS_LABEL, from)} → ${labelOf(ORDER_STATUS_LABEL, to)}`)
  if (e.kind === 'attempt.progress') {
    if (typeof d.stage === 'string' && d.stage) push(`阶段 ${d.stage}`)
    const code = typeof d.cancel_code === 'string' && d.cancel_code ? d.cancel_code : null
    if (code || d.continue === false) {
      push(`服务端叫停：${code ? labelOf(PROGRESS_CANCEL_LABEL, code) : '未写原因'}`)
      if (typeof d.cancel_reason === 'string' && d.cancel_reason) push(d.cancel_reason)
    }
    return parts.join(' · ')
  }
  push(eventSourceText(d.source))
  if (typeof d.verdict === 'string' && d.verdict) push(labelOf(FEISHU_VERDICT_LABEL, d.verdict))
  if (!from && typeof d.status === 'string' && d.status) push(`状态：${labelOf(ORDER_STATUS_LABEL, d.status)}`)
  push(changesText(d.changes))
  push(changesText(d.fields))
  if (typeof d.error_code === 'string' && d.error_code) push(errorCodeLabel(d.error_code))
  if (e.kind === 'attempt.guard_blocked' && typeof d.code === 'string' && d.code) push(labelOf(GUARD_CODE_LABEL, d.code))
  for (const k of EVENT_TEXT_KEYS) {
    const v = d[k]
    if (typeof v === 'string' && v) push(v)
  }
  for (const k of ['notes', 'reasons']) {
    const v = d[k]
    if (Array.isArray(v)) push(v.filter((x) => typeof x === 'string' && x).join('；'))
  }
  if (!parts.length) {
    const rest = Object.entries(d).filter(
      ([k, v]) => !EVENT_HANDLED_KEYS.has(k) && v !== null && v !== undefined && v !== '' && typeof v !== 'object',
    )
    if (rest.length) parts.push(rest.map(([k, v]) => `${fieldLabel(k)}：${displayValue(v)}`).join('；'))
  }
  return parts.join(' · ')
}

// ── 核实结果（结果待核实）────────────────────────────────────────────────────

export const AMAZON_ORDER_NO_RE = /^\d{3}-\d{7}-\d{7}$/
/** 模拟尝试的单号（插件模拟成功时上报 SIM-<北京日期>-<尝试 ID>） */
export const SIM_ORDER_NO_RE = /^SIM-[A-Za-z0-9-]+$/

/** 输入：任务详情 → 输出：等待人工核实的尝试（详情里新的在前，取最新一条 status=unknown），没有为 null */
export function pendingUnknownAttempt(d: Pick<OrderDetail, 'attempts'> | null | undefined): Attempt | null {
  return d?.attempts?.find((a) => a.status === 'unknown') ?? null
}

/** 输入：事件（新的在前）、尝试 id → 输出：这笔尝试最近一条「插件迟到上报的结果」事件，没有为 null */
export function latestLateResult(events: readonly EventRecord[] | null | undefined, attemptId: number | null | undefined): EventRecord | null {
  return (events ?? []).find((e) => e.kind === 'attempt.late_result' && (attemptId === null || attemptId === undefined || e.entity_id === attemptId)) ?? null
}

export interface UnknownPrefill {
  amazon_order_no: string
  subtotal: string
  shipping: string
  tax: string
  total_before_tax: string
  total: string
  /** 待核实的尝试是不是模拟的（模拟单号以 SIM- 开头） */
  simulated: boolean
  /** 有没有从尝试上预填（迟到的回执补进了单号或金额） */
  prefilled: boolean
  /** 给人工核实的提示：最近一条迟到回执的说明；尝试带着单号但没有回执事件时给通用说明；都没有为空串 */
  hint: string
}

const AMOUNT_KEYS = ['subtotal', 'shipping', 'tax', 'total_before_tax', 'total'] as const

/** 输入：尝试上的金额（两位小数字符串）或 null → 输出：输入框里的值（原样；空或解析不了为 ""，不当作 0） */
function amountInput(v: unknown): string {
  if (typeof v === 'number') return Number.isFinite(v) ? String(v) : ''
  return typeof v === 'string' && parseAmount(v) !== null ? v.trim() : ''
}

/**
 * 输入：任务详情 → 输出：「核实采购结果」对话框的初始值。
 * 尝试已带着亚马逊订单号或金额（插件迟到上报的回执补进了空着的列）时预填，并附上最近一条 attempt.late_result 的说明，
 * 由人工与亚马逊订单页核对后再登记；没有的项留空。
 */
export function unknownPrefill(d: Pick<OrderDetail, 'attempts' | 'events'> | null | undefined): UnknownPrefill {
  const att = pendingUnknownAttempt(d)
  const out: UnknownPrefill = {
    amazon_order_no: att?.amazon_order_no?.trim() ?? '',
    subtotal: '',
    shipping: '',
    tax: '',
    total_before_tax: '',
    total: '',
    simulated: !!att?.simulated,
    prefilled: false,
    hint: '',
  }
  if (!att) return out
  for (const k of AMOUNT_KEYS) out[k] = amountInput(att[k])
  out.prefilled = !!out.amazon_order_no || AMOUNT_KEYS.some((k) => out[k] !== '')
  const late = latestLateResult(d?.events, att.id)
  const reason = late && typeof late.detail?.reason === 'string' ? late.detail.reason.trim() : ''
  if (reason) out.hint = reason
  else if (out.amazon_order_no) out.hint = `这笔尝试已带着亚马逊订单号 ${out.amazon_order_no}，请与亚马逊订单页核对`
  return out
}

/** 输入：填写的亚马逊订单号、待核实的尝试是否模拟 → 输出：不合格的原因，合格为空串 */
export function amazonOrderNoProblem(no: string | null | undefined, simulated: boolean): string {
  const s = (no ?? '').trim()
  if (!s) return '请填写亚马逊订单号'
  if (simulated) return SIM_ORDER_NO_RE.test(s) ? '' : '模拟尝试的单号以 SIM- 开头，如 SIM-20261105-27'
  if (s.toUpperCase().startsWith('SIM-')) return '真实尝试不能登记模拟单号'
  return AMAZON_ORDER_NO_RE.test(s) ? '' : '格式应为 123-1234567-1234567'
}

// ── 冲突（按种类给出处理方式）────────────────────────────────────────────────

export interface ConflictChoice {
  /** 按钮文字 */
  label: string
  /** 确认框标题 */
  title: string
  /** 确认框文案（「确定要……吗？」） */
  message: string
  /** 成功提示 */
  success: string
  danger?: boolean
}

export interface ConflictView {
  /** 冲突种类的中文名 */
  kindLabel: string
  /** 冲突说明（服务端的原因优先） */
  description: string
  keep: ConflictChoice
  /** 不能接受时为 null（如来源的 AMZ 订单号格式不对，只能保留） */
  accept: ConflictChoice | null
  /** 不能接受的原因 */
  acceptBlocked: string
}

/** 输入：任务状态 → 输出：冲突对这笔任务的影响（冲突说明的后半句） */
function conflictEffect(status: OrderStatus | string | null | undefined): string {
  if (status === 'executing') return '插件下一次上报进度时会在提交前中止这次采购。'
  if (status === 'exception') return '任务保持异常，处理冲突后再恢复。'
  return '任务暂停在「待核实」，处理前不会分配或执行。'
}

/**
 * 输入：任务上的冲突、任务状态 → 输出：冲突区的说明与两个按钮（文案按冲突种类，accept 的含义随种类而变，见契约 §2）。
 * 没有 kind 的旧数据按 fields 处理。
 */
export function conflictView(c: OrderConflict | null | undefined, status?: OrderStatus | string | null): ConflictView {
  const kind = typeof c?.kind === 'string' && c.kind ? c.kind : 'fields'
  const who = c?.source_kind ? `${labelOf(SOURCE_KIND_LABEL, c.source_kind)}来源` : '来源'
  const reason = typeof c?.reason === 'string' ? c.reason.trim() : ''
  const incoming = (c?.incoming ?? {}) as Record<string, unknown>
  const kindLabel = labelOf(CONFLICT_KIND_LABEL, kind)
  const desc = (fallback: string) => `${(reason || fallback).replace(/[。.]$/, '')}。${conflictEffect(status)}`
  switch (kind) {
    case 'review':
      return {
        kindLabel,
        description: desc(`${who}送来的人审结论是拒绝，与任务已有的通过结论不同`),
        keep: {
          label: '保留通过',
          title: '保留人审通过',
          message: '确定要保留人审通过吗？会记为控制台确认通过，之后同样的旧文件再上传不再转冲突。',
          success: '已保留人审通过',
        },
        accept: {
          label: '接受拒绝（回待确认）',
          title: '接受人审拒绝',
          message: '确定要接受来源的人审拒绝吗？任务回到「待确认」，不会下发采购。',
          success: '已接受人审拒绝',
          danger: true,
        },
        acceptBlocked: '',
      }
    case 'external_order_no': {
      const no = typeof incoming.external_amz_order_no === 'string' ? incoming.external_amz_order_no.trim() : ''
      const malformed = !!no && !AMAZON_ORDER_NO_RE.test(no)
      return {
        kindLabel,
        description: desc(no ? `${who}出现 AMZ 订单号 ${no}，需人工核实是否已在别处采购` : `${who}出现 AMZ 订单号，需人工核实`),
        keep: {
          label: '保留当前',
          title: '保留当前',
          message: '确定要保留当前、不按来源的 AMZ 订单号登记吗？任务按当前状态继续，可能会再次下单，请先确认没有在别处采购。',
          success: '已保留当前',
          danger: true,
        },
        accept: malformed
          ? null
          : {
              label: '登记为已采购',
              title: '登记为已采购',
              message: `确定要按来源的 AMZ 订单号${no ? ` ${no} ` : ''}登记为已采购吗？本系统不会再为这笔任务下单。`,
              success: '已登记为已采购',
            },
        acceptBlocked: malformed ? `来源的 AMZ 订单号「${no}」格式不对，不能登记为已采购，只能保留当前` : '',
      }
    }
    case 'admission':
      return {
        kindLabel,
        description: desc(`${who}的采购方或采购状态已变化，这笔可能已不归本系统采购`),
        keep: {
          label: '保留，继续采购',
          title: '继续采购',
          message: '确定要保留这笔任务、继续采购吗？请先确认它仍由本系统采购，没有在别处下单。',
          success: '已保留任务',
          danger: true,
        },
        accept: {
          label: '不再采购（取消任务）',
          title: '不再采购',
          message: '确定要取消这笔任务吗？来源已不归本系统采购，取消后不会再分配或执行。',
          success: '任务已取消',
          danger: true,
        },
        acceptBlocked: '',
      }
    case 'source_key_changed': {
      const no = typeof incoming.upstream_order_no === 'string' ? incoming.upstream_order_no : ''
      return {
        kindLabel,
        description: desc(`${who}记录的平台订单号变了${no ? `（新订单号 ${no}）` : ''}`),
        keep: {
          label: '保留原任务',
          title: '保留原任务',
          message: '确定要保留原任务吗？原任务按当前值继续，那条来源记录仍关联在原任务上，新订单号不会另建任务。',
          success: '已保留原任务',
        },
        accept: {
          label: '按新订单号接收（取消原任务）',
          title: '按新订单号接收',
          message: `确定要取消原任务、按新平台订单号${no ? ` ${no} ` : ''}接收吗？那条来源记录会与原任务解除关联，下次读取时按新订单号建任务。`,
          success: '已取消原任务',
          danger: true,
        },
        acceptBlocked: '',
      }
    }
    default:
      return {
        kindLabel,
        description: desc(`${who}送来的执行相关字段与当前不同`),
        keep: {
          label: '保留当前',
          title: '保留当前值',
          message: '确定要保留当前的执行值、忽略来源送来的新值吗？',
          success: '已保留当前值',
        },
        accept: {
          label: '接受来源新值',
          title: '接受来源新值',
          message: '确定要接受来源送来的新值吗？任务会按新值重新校验，控制台覆盖值不受影响。',
          success: '已接受来源新值',
        },
        acceptBlocked: '',
      }
  }
}
