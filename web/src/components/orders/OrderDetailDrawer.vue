<script setup lang="ts">
/**
 * 任务详情抽屉：执行参数（有效值 vs 原值）、收件信息、分配、冲突、来源与快照（含回填目标）、采购尝试（含护栏逐项结果）、
 * 事件（更早的按页读取）。底部操作按钮只由后端给的 allowed_actions 决定，前端不自行推断哪些动作可做。
 */
import { computed, reactive, ref, watch } from 'vue'
import type { FormInstance, FormRules } from 'element-plus'
import type { Account, Attempt, EventRecord, OrderAction, OrderDetail, OrderSource } from '@/api/types'
import { ORDER_DETAIL_EVENT_LIMIT } from '@/api/types'
import * as ordersApi from '@/api/orders'
import * as accountsApi from '@/api/accounts'
import * as eventsApi from '@/api/events'
import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import OrderStatusTag from './OrderStatusTag.vue'
import {
  displayValue,
  formatAmountRange,
  formatDate,
  formatDateTime,
  formatDays,
  formatMoney,
  formatNumber,
  hasAtMostTwoDecimals,
  normalizeMoneyInput,
  parseAmount,
} from '@/utils/format'
import {
  ACTOR_KIND_LABEL,
  ASSIGNMENT_KIND_LABEL,
  ATTEMPT_KIND_LABEL,
  ATTEMPT_STATUS_LABEL,
  ATTEMPT_STATUS_TAG,
  GUARD_CODE_LABEL,
  ORDER_STATUS_LABEL,
  RESOLUTION_LABEL,
  REVIEW_STATUS_LABEL,
  REVIEW_STATUS_TAG,
  SOURCE_KIND_LABEL,
  SUGGESTED_DATE_BASIS_LABEL,
  errorCodeLabel,
  eventKindLabel,
  fieldLabel,
  labelOf,
  tagOf,
} from '@/utils/labels'
import { normalizeGuardChecks } from '@/utils/guard'
import { missingFieldsText, showStatusReason, statusAlertType } from '@/utils/orderDetail'
import { confirmAction, notifyFailure, notifySuccess, promptText } from '@/utils/feedback'

const props = defineProps<{ orderId: number | null }>()
const visible = defineModel<boolean>({ default: false })
const emit = defineEmits<{ changed: [order: OrderDetail | null] }>()

const detail = ref<OrderDetail | null>(null)
const loading = ref(false)
const loadError = ref('')
const busy = ref<string | null>(null)
/** 按页读到的更早事件（详情只随附最近的一部分） */
const olderEvents = ref<EventRecord[]>([])
/** GET /events 返回的全部条数；还没按页读过为 null */
const olderTotal = ref<number | null>(null)
const olderLoading = ref(false)

const drawerSize = computed(() => (typeof window !== 'undefined' && window.innerWidth < 800 ? '100%' : '760px'))
const can = (a: OrderAction) => !!detail.value?.allowed_actions?.includes(a)

/** 输入：新的任务详情 → 输出：无（替换详情，清掉按页读过的更早事件） */
function setDetail(d: OrderDetail | null) {
  detail.value = d
  olderEvents.value = []
  olderTotal.value = null
}

async function load() {
  if (!props.orderId) return
  loading.value = true
  loadError.value = ''
  try {
    setDetail(await ordersApi.getOrder(props.orderId))
  } catch (e) {
    detail.value = null
    loadError.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.orderId, visible.value] as const,
  ([id, open]) => {
    if (open && id) void load()
  },
  { immediate: true },
)

/** 输入：动作名、请求函数、成功提示 → 输出：无（成功后刷新详情并通知列表） */
async function run(action: string, fn: () => Promise<unknown>, success: string) {
  busy.value = action
  try {
    const res = await fn()
    if (res && typeof res === 'object' && 'allowed_actions' in res) setDetail(res as OrderDetail)
    else await load()
    notifySuccess(success)
    emit('changed', detail.value)
    return true
  } catch (e) {
    notifyFailure(action, e)
    return false
  } finally {
    busy.value = null
  }
}

// ── 执行参数 ──────────────────────────────────────────────────────────────────

interface ParamRow {
  key: string
  label: string
  effective: string
  original: string
  overridden: boolean
}

const params = computed<ParamRow[]>(() => {
  const d = detail.value
  if (!d) return []
  const o = d.original ?? ({} as OrderDetail['original'])
  const ov = d.override
  return [
    { key: 'asin', label: 'ASIN', effective: d.asin_eff ?? '—', original: o.asin ?? d.asin ?? '—', overridden: !!ov?.asin },
    {
      key: 'quantity',
      label: '数量',
      effective: formatNumber(d.quantity_eff),
      original: formatNumber(o.quantity ?? d.quantity),
      overridden: ov?.quantity !== null && ov?.quantity !== undefined,
    },
    {
      key: 'price_cap',
      label: '限价',
      effective: formatMoney(d.price_cap_eff),
      original: formatMoney(o.price_cap ?? d.price_cap),
      overridden: !!ov?.price_cap,
    },
    { key: 'unit_price', label: '采购单价', effective: formatMoney(d.unit_price), original: formatMoney(o.unit_price ?? d.unit_price), overridden: false },
    { key: 'subtotal', label: '商品小计', effective: formatMoney(d.subtotal_eff), original: '单价 × 数量', overridden: false },
    {
      key: 'suggested_purchase_date',
      label: '建议采购日期',
      effective: formatDate(d.suggested_purchase_date),
      original: o.suggested_date_basis ? labelOf(SUGGESTED_DATE_BASIS_LABEL, o.suggested_date_basis) : formatDate(o.suggested_purchase_date),
      overridden: false,
    },
    { key: 'ship_days', label: '配送天数', effective: formatDays(o.ship_days), original: '审核时采集', overridden: false },
    { key: 'order_time', label: '下单时间', effective: formatDateTime(o.order_time ?? d.order_time), original: '北京时间', overridden: false },
  ]
})

const addressRows = computed(() => {
  const a = detail.value?.address
  if (!a) return []
  return [
    ['收货人', a.recipient_name],
    ['电话', a.phone],
    ['地址 1', a.address1],
    ['地址 2', a.address2],
    ['城市', a.city],
    ['州', a.state],
    ['邮编', a.postal_code],
    ['区', a.district],
    ['国家', a.country],
  ] as [string, string | null][]
})

// ── 来源快照 ──────────────────────────────────────────────────────────────────

const snapshotSource = ref<OrderSource | null>(null)
const snapshotRows = computed(() =>
  Object.entries(snapshotSource.value?.snapshot ?? {}).map(([k, v]) => ({ key: k, value: displayValue(v) })),
)
function sourceTitle(s: OrderSource): string {
  if (s.kind === 'feishu') return `${s.connection_name || '飞书连接'} · 记录 ${s.record_id ?? '—'}`
  return `${s.filename || `批次 ${s.batch_id ?? '—'}`} · 第 ${s.row_no ?? '—'} 行`
}

const feishuSources = computed(() => (detail.value?.sources ?? []).filter((s) => s.kind === 'feishu'))
const hasWritebackTarget = computed(() => feishuSources.value.some((s) => s.is_writeback_target))

/** 输入：一条不是回填目标的飞书来源 → 输出：无（确认后设为这笔任务唯一的回填目标） */
async function setWritebackTarget(s: OrderSource) {
  if (!detail.value) return
  const ok = await confirmAction({
    title: '设为回填目标',
    message: `确定要把这条飞书记录设为回填目标吗？之后这笔任务的真实采购结果只写回「${sourceTitle(s)}」${hasWritebackTarget.value ? '，原来的回填目标不再写回' : ''}；已采购的真实结果会马上排队写回，模拟结果不写回。`,
  })
  if (!ok) return
  const id = detail.value.id
  await run('设为回填目标', () => ordersApi.setWritebackTarget(id, s.id), '已设为回填目标')
}

// ── 尝试与事件 ────────────────────────────────────────────────────────────────

function attemptAmounts(a: Attempt): [string, string][] {
  return [
    ['商品小计', formatMoney(a.subtotal)],
    ['运费', formatMoney(a.shipping)],
    ['税费', formatMoney(a.tax)],
    ['税前总额', formatMoney(a.total_before_tax)],
    ['结算总额', formatMoney(a.total)],
  ]
}

function eventSummary(e: EventRecord): string {
  const d = e.detail ?? {}
  const parts: string[] = []
  const from = typeof d.from === 'string' ? d.from : null
  const to = typeof d.to === 'string' ? d.to : null
  if (from && to) parts.push(`${labelOf(ORDER_STATUS_LABEL, from)} → ${labelOf(ORDER_STATUS_LABEL, to)}`)
  for (const k of ['reason', 'note', 'message', 'account_name', 'amazon_order_no']) {
    const v = d[k]
    if (typeof v === 'string' && v) parts.push(v)
  }
  if (!parts.length) {
    const rest = Object.entries(d).filter(([, v]) => v !== null && v !== undefined && v !== '')
    if (rest.length) parts.push(rest.map(([k, v]) => `${fieldLabel(k)}：${displayValue(v)}`).join('；'))
  }
  return parts.join(' · ')
}

function actorText(e: EventRecord): string {
  const kind = labelOf(ACTOR_KIND_LABEL, e.actor_kind)
  return e.actor_name ? `${kind} ${e.actor_name}` : kind
}

// 详情只随附最近 ORDER_DETAIL_EVENT_LIMIT 条事件；更早的用 GET /events?order_id= 按页读（同样新的在前）
const EVENT_PAGE = 100
const allEvents = computed(() => [...(detail.value?.events ?? []), ...olderEvents.value])
const hasOlderEvents = computed(() =>
  olderTotal.value === null ? (detail.value?.events?.length ?? 0) >= ORDER_DETAIL_EVENT_LIMIT : allEvents.value.length < olderTotal.value,
)

async function loadOlderEvents() {
  if (!detail.value) return
  olderLoading.value = true
  try {
    const page = Math.floor(allEvents.value.length / EVENT_PAGE) + 1
    const res = await eventsApi.listEvents({ order_id: detail.value.id, page, page_size: EVENT_PAGE })
    const seen = new Set(allEvents.value.map((e) => e.id))
    olderEvents.value = [...olderEvents.value, ...res.rows.filter((e) => !seen.has(e.id))]
    // 读到不满一页说明到底了
    olderTotal.value = res.rows.length < EVENT_PAGE ? allEvents.value.length : res.total
  } catch (e) {
    notifyFailure('读取更早的事件', e)
  } finally {
    olderLoading.value = false
  }
}

const MONEY_FIELDS = new Set(['unit_price', 'price_cap', 'sales_total'])
/** 输入：字段键、值 → 输出：冲突表里的展示文本（金额字段按 $1,234.50 显示） */
function conflictValue(key: string, v: unknown): string {
  return MONEY_FIELDS.has(key) ? formatMoney(v) : displayValue(v)
}
const conflictRows = computed(() =>
  Object.entries(detail.value?.conflict?.fields ?? {}).map(([k, pair]) => ({
    key: k,
    label: fieldLabel(k),
    current: conflictValue(k, Array.isArray(pair) ? pair[0] : pair),
    incoming: conflictValue(k, Array.isArray(pair) ? pair[1] : null),
  })),
)

// ── 人审 ──────────────────────────────────────────────────────────────────────

async function approve() {
  if (!detail.value) return
  const ok = await confirmAction({ message: '确定要将这笔任务标记为人审通过吗？通过后会进入资料校验与分配。' })
  if (!ok) return
  const id = detail.value.id
  await run('人审通过', () => ordersApi.reviewOrder(id, 'approve'), '已标记为人审通过')
}

async function reject() {
  if (!detail.value) return
  const note = await promptText({
    title: '人审拒绝',
    message: '确定要拒绝这笔任务吗？拒绝后不会下发采购。可填写原因。',
    placeholder: '原因（可选）',
    confirmText: '拒绝',
    danger: true,
  })
  if (note === null) return
  const id = detail.value.id
  await run('人审拒绝', () => ordersApi.reviewOrder(id, 'reject', note || undefined), '已拒绝')
}

// ── 改派 ──────────────────────────────────────────────────────────────────────

const assignOpen = ref(false)
const assignAccountId = ref<number | null>(null)
const accounts = ref<Account[]>([])
const accountsLoading = ref(false)

async function openAssign() {
  assignOpen.value = true
  assignAccountId.value = detail.value?.assigned_account?.id ?? null
  accountsLoading.value = true
  try {
    accounts.value = (await accountsApi.listAccounts()).rows
  } catch (e) {
    notifyFailure('读取账号', e)
  } finally {
    accountsLoading.value = false
  }
}

/** 输入：账号 → 输出：不可选的原因（未启用、金额不在区间），可选时为空串 */
function accountBlock(a: Account): string {
  if (!a.purchase_enabled) return '采购开关已关闭'
  const sub = parseAmount(detail.value?.subtotal_eff)
  const lo = parseAmount(a.min_amount) ?? 0
  const hi = parseAmount(a.max_amount)
  if (sub !== null && (sub < lo || (hi !== null && sub > hi))) return '金额不在区间内'
  return ''
}

async function submitAssign() {
  if (!detail.value || !assignAccountId.value) return
  const id = detail.value.id
  const accountId = assignAccountId.value
  const name = accounts.value.find((a) => a.id === accountId)?.name ?? ''
  const ok = await run('改派账号', () => ordersApi.assignOrder(id, accountId), `已改派到 ${name}`)
  if (ok) assignOpen.value = false
}

async function unassign() {
  if (!detail.value) return
  const ok = await confirmAction({ message: '确定要解除这笔任务的账号分配吗？任务会回到待分配。' })
  if (!ok) return
  const id = detail.value.id
  await run('解除分配', () => ordersApi.unassignOrder(id), '已解除分配')
}

// ── 修改执行参数 ──────────────────────────────────────────────────────────────

const overrideOpen = ref(false)
const overrideRef = ref<FormInstance>()
const overrideForm = reactive({ asin: '', quantity: undefined as number | undefined, price_cap: '', note: '' })
const overrideRules: FormRules = {
  asin: [{ pattern: /^[A-Za-z0-9]{10}$/, message: 'ASIN 为 10 位字母或数字', trigger: 'blur' }],
  price_cap: [
    {
      validator: (_r: unknown, v: string, cb: (e?: Error) => void) => {
        if (!v) return cb()
        const n = parseAmount(v)
        if (n === null || n <= 0) return cb(new Error('限价须为大于 0 的金额'))
        // 限价是上限：不替用户四舍五入（40.999 → 41.00 会多放行），超过两位小数让用户自己改
        if (!hasAtMostTwoDecimals(v)) return cb(new Error('限价最多两位小数，如 40.99'))
        cb()
      },
      trigger: 'blur',
    },
  ],
  note: [{ required: true, message: '请写明修改原因', trigger: 'blur' }],
}

function openOverride() {
  const ov = detail.value?.override
  overrideForm.asin = ov?.asin ?? ''
  overrideForm.quantity = ov?.quantity ?? undefined
  overrideForm.price_cap = ov?.price_cap ?? ''
  overrideForm.note = ''
  overrideOpen.value = true
}

async function submitOverride() {
  if (!detail.value) return
  if (!(await overrideRef.value?.validate().catch(() => false))) return
  const id = detail.value.id
  const body = {
    asin: overrideForm.asin.trim() ? overrideForm.asin.trim().toUpperCase() : null,
    quantity: overrideForm.quantity ?? null,
    price_cap: normalizeMoneyInput(overrideForm.price_cap),
    note: overrideForm.note.trim(),
  }
  const ok = await run('修改执行参数', () => ordersApi.overrideOrder(id, body), '执行参数已更新，只作用于采购，不回填来源')
  if (ok) overrideOpen.value = false
}

async function clearOverride() {
  if (!detail.value) return
  const ok = await confirmAction({ message: '确定要清除全部覆盖值、恢复使用来源原值吗？' })
  if (!ok) return
  const id = detail.value.id
  const note = overrideForm.note.trim() || '清除覆盖值'
  const done = await run(
    '清除覆盖值',
    () => ordersApi.overrideOrder(id, { asin: null, quantity: null, price_cap: null, note }),
    '已恢复使用来源原值',
  )
  if (done) overrideOpen.value = false
}

// ── 恢复、取消 ────────────────────────────────────────────────────────────────

async function resume() {
  if (!detail.value) return
  const note = await promptText({
    title: '恢复任务',
    message: '确定要恢复这笔任务吗？恢复后会按最新的账号配置与限价重新校验。',
    placeholder: '处理说明（可选）',
    confirmText: '恢复',
  })
  if (note === null) return
  const id = detail.value.id
  await run('恢复任务', () => ordersApi.resumeOrder(id, note || undefined), '任务已恢复并重新校验')
}

async function cancelOrder() {
  if (!detail.value) return
  const note = await promptText({
    title: '取消任务',
    message: '确定要取消这笔任务吗？取消后不会再分配或执行，可在之后撤销。',
    placeholder: '取消原因',
    required: true,
    confirmText: '取消任务',
    danger: true,
  })
  if (note === null) return
  const id = detail.value.id
  await run('取消任务', () => ordersApi.cancelOrder(id, note), '任务已取消')
}

async function uncancel() {
  if (!detail.value) return
  const ok = await confirmAction({ message: '确定要撤销取消吗？任务会按来源数据重新校验状态。' })
  if (!ok) return
  const id = detail.value.id
  await run('撤销取消', () => ordersApi.uncancelOrder(id), '已撤销取消')
}

// ── 结果待核实 ────────────────────────────────────────────────────────────────

const unknownOpen = ref(false)
const unknownRef = ref<FormInstance>()
const unknownForm = reactive({
  outcome: 'ordered' as 'ordered' | 'not_ordered',
  amazon_order_no: '',
  subtotal: '',
  shipping: '',
  tax: '',
  total_before_tax: '',
  total: '',
  note: '',
})
const moneyRule = {
  validator: (_r: unknown, v: string, cb: (e?: Error) => void) => {
    if (!v) return cb()
    if (normalizeMoneyInput(v) === null) return cb(new Error('请填写金额，如 44.49'))
    if (!hasAtMostTwoDecimals(v)) return cb(new Error('金额最多两位小数，与亚马逊订单页一致'))
    cb()
  },
  trigger: 'blur',
}
const unknownRules = computed<FormRules>(() => ({
  amazon_order_no:
    unknownForm.outcome === 'ordered'
      ? [
          { required: true, message: '请填写亚马逊订单号', trigger: 'blur' },
          { pattern: /^\d{3}-\d{7}-\d{7}$/, message: '格式应为 123-1234567-1234567', trigger: 'blur' },
        ]
      : [],
  total: unknownForm.outcome === 'ordered' ? [{ required: true, message: '请填写结算总额', trigger: 'blur' }, moneyRule] : [],
  subtotal: [moneyRule],
  shipping: [moneyRule],
  tax: [moneyRule],
  total_before_tax: [moneyRule],
  note: [{ required: true, message: '请写明核实依据', trigger: 'blur' }],
}))

function openUnknown() {
  Object.assign(unknownForm, {
    outcome: 'ordered',
    amazon_order_no: '',
    subtotal: '',
    shipping: '',
    tax: '',
    total_before_tax: '',
    total: '',
    note: '',
  })
  unknownOpen.value = true
}

async function submitUnknown() {
  if (!detail.value) return
  if (!(await unknownRef.value?.validate().catch(() => false))) return
  const id = detail.value.id
  const ordered = unknownForm.outcome === 'ordered'
  if (!ordered) {
    const ok = await confirmAction({
      message: '确定要登记为未下单吗？请先在亚马逊订单页核实这笔确实没有下单。确认后释放当天额度，任务回到待采购，可能被再次执行。',
      danger: true,
    })
    if (!ok) return
  }
  const body = ordered
    ? {
        outcome: 'ordered' as const,
        amazon_order_no: unknownForm.amazon_order_no.trim(),
        amounts: {
          subtotal: normalizeMoneyInput(unknownForm.subtotal),
          shipping: normalizeMoneyInput(unknownForm.shipping),
          tax: normalizeMoneyInput(unknownForm.tax),
          total_before_tax: normalizeMoneyInput(unknownForm.total_before_tax),
          total: normalizeMoneyInput(unknownForm.total),
        },
        note: unknownForm.note.trim(),
      }
    : { outcome: 'not_ordered' as const, note: unknownForm.note.trim() }
  const done = await run('核实结果', () => ordersApi.resolveUnknown(id, body), ordered ? '已登记为已下单' : '已登记为未下单')
  if (done) unknownOpen.value = false
}

// ── 亚马逊取消与重采 ──────────────────────────────────────────────────────────

async function markAmazonCancelled() {
  if (!detail.value) return
  const note = await promptText({
    title: '登记亚马逊取消',
    message: '确定要登记亚马逊已取消这笔采购吗？任务会进入「待决定重采」，不会自动重买。',
    placeholder: '取消原因或亚马逊通知内容',
    required: true,
    confirmText: '登记',
    danger: true,
  })
  if (note === null) return
  const id = detail.value.id
  await run('登记亚马逊取消', () => ordersApi.markAmazonCancelled(id, note), '已登记亚马逊取消')
}

async function repurchase(decision: 'approve' | 'decline') {
  if (!detail.value) return
  const approve = decision === 'approve'
  const note = await promptText({
    title: approve ? '同意重采' : '不重采',
    message: approve
      ? '确定要重新采购这笔任务吗？会重新校验限价、配送时长与账号额度后进入待采购，形成新的采购尝试。'
      : '确定要关闭这笔任务、不再重采吗？原采购与取消记录保留。',
    placeholder: '说明（可选）',
    confirmText: approve ? '同意重采' : '不重采',
    danger: !approve,
  })
  if (note === null) return
  const id = detail.value.id
  await run(approve ? '同意重采' : '关闭任务', () => ordersApi.decideRepurchase(id, decision, note || undefined), approve ? '已同意重采' : '任务已关闭')
}

// ── 冲突 ──────────────────────────────────────────────────────────────────────

async function resolveConflict(choice: 'keep' | 'accept') {
  if (!detail.value) return
  const keep = choice === 'keep'
  const note = await promptText({
    title: keep ? '保留当前值' : '接受来源新值',
    message: keep
      ? '确定要保留当前的执行值、忽略来源送来的新值吗？'
      : '确定要接受来源送来的新值吗？任务会按新值重新校验，控制台覆盖值不受影响。',
    placeholder: '核实说明（可选）',
    confirmText: keep ? '保留当前' : '接受新值',
  })
  if (note === null) return
  const id = detail.value.id
  await run('处理冲突', () => ordersApi.resolveConflict(id, choice, note || undefined), keep ? '已保留当前值' : '已接受来源新值')
}

// ── 需要留意 ──────────────────────────────────────────────────────────────────

async function clearAttention() {
  if (!detail.value) return
  const note = await promptText({
    title: '标记已处理',
    message: '确定要把这条提醒标记为已处理吗？请写明对相关亚马逊订单做了什么，原提醒会记进事件。',
    placeholder: '处理说明，如 已在亚马逊取消对应订单',
    required: true,
    confirmText: '标记已处理',
  })
  if (note === null) return
  const id = detail.value.id
  await run('标记已处理', () => ordersApi.clearAttention(id, note), '已标记为已处理')
}

/** 底部按钮栏里的动作（回填目标在来源列表里逐条操作，不算在内） */
const FOOTER_ACTIONS: readonly OrderAction[] = [
  'review', 'assign', 'unassign', 'override', 'resume', 'cancel', 'uncancel', 'resolve_unknown',
  'mark_amazon_cancelled', 'repurchase', 'resolve_conflict', 'clear_attention',
]
const hasActions = computed(() => FOOTER_ACTIONS.some((a) => can(a)))
</script>

<template>
  <el-drawer v-model="visible" :size="drawerSize" :with-header="true" destroy-on-close append-to-body>
    <template #header>
      <div class="flex min-w-0 flex-wrap items-center gap-2">
        <template v-if="detail">
          <span class="code text-base">{{ detail.upstream_order_no }}</span>
          <OrderStatusTag :status="detail.status" />
          <el-tag v-if="detail.has_override" type="warning" disable-transitions>已修改执行参数</el-tag>
        </template>
        <span v-else class="text-lg font-semibold text-gray-900 dark:text-white">任务详情</span>
      </div>
    </template>

    <div v-loading="loading" class="min-h-[12rem]">
      <EmptyState v-if="loadError && !detail" title="读取任务失败" :description="loadError" icon="exclamationCircle">
        <template #actions><el-button @click="load">重新读取</el-button></template>
      </EmptyState>

      <template v-if="detail">
        <div class="mb-4 space-y-2">
          <el-alert v-if="showStatusReason(detail)" :title="detail.status_reason ?? ''" :type="statusAlertType(detail.status)" :closable="false" show-icon />
          <el-alert
            v-if="detail.needs_attention"
            :title="detail.attention_note || '这笔任务需要人工留意'"
            type="error"
            :closable="false"
            show-icon
          >
            <template v-if="can('clear_attention')" #default>
              <div class="mt-1 flex flex-wrap items-center gap-2">
                <span>处理完相关的亚马逊订单后标记已处理，任务就不再出现在「需要留意」里。</span>
                <el-button size="small" :loading="busy === '标记已处理'" @click="clearAttention">标记已处理</el-button>
              </div>
            </template>
          </el-alert>
          <el-alert
            v-if="detail.missing_fields?.length"
            :title="missingFieldsText(detail)"
            type="warning"
            :closable="false"
            show-icon
          />
        </div>

        <section class="pc-section">
          <h3 class="pc-section-title">执行参数</h3>
          <el-table :data="params" size="small" class="eq-table-card">
            <el-table-column label="项目" prop="label" width="120" />
            <el-table-column label="执行值" min-width="160">
              <template #default="{ row }">
                <span class="font-medium text-gray-900 dark:text-white">{{ row.effective }}</span>
                <el-tag v-if="row.overridden" size="small" type="warning" class="ml-2" disable-transitions>控制台覆盖</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="来源原值 / 依据" min-width="180">
              <template #default="{ row }">
                <span :class="row.overridden ? 'text-gray-500 line-through dark:text-dark-400' : 'text-gray-500 dark:text-dark-400'">
                  {{ row.original }}
                </span>
              </template>
            </el-table-column>
          </el-table>
          <p v-if="detail.override" class="mt-2 text-xs text-gray-500 dark:text-dark-400">
            覆盖说明：{{ detail.override.note || '—' }} · {{ detail.override.by_name || '—' }} · {{ formatDateTime(detail.override.at) }}
          </p>
        </section>

        <section class="pc-section grid gap-6 md:grid-cols-2">
          <div>
            <h3 class="pc-section-title">收件信息</h3>
            <dl class="pc-kv">
              <template v-for="[k, v] in addressRows" :key="k">
                <dt>{{ k }}</dt>
                <dd>{{ v || '—' }}</dd>
              </template>
            </dl>
          </div>
          <div>
            <h3 class="pc-section-title">分配</h3>
            <dl class="pc-kv">
              <dt>买家账号</dt>
              <dd>{{ detail.assigned_account?.name || '未分配' }}</dd>
              <dt>分配方式</dt>
              <dd>{{ labelOf(ASSIGNMENT_KIND_LABEL, detail.assignment_kind) }}</dd>
              <dt>分配说明</dt>
              <dd>{{ detail.assignment_note || '—' }}</dd>
              <dt>人审</dt>
              <dd>
                <el-tag size="small" :type="tagOf(REVIEW_STATUS_TAG, detail.review_status)" disable-transitions>
                  {{ labelOf(REVIEW_STATUS_LABEL, detail.review_status) }}
                </el-tag>
                <span v-if="detail.original?.review_value" class="ml-2 text-gray-500 dark:text-dark-400">原文：{{ detail.original.review_value }}</span>
              </dd>
              <dt>主来源</dt>
              <dd>{{ labelOf(SOURCE_KIND_LABEL, detail.primary_source_kind) }}</dd>
              <dt>店铺 / SKU</dt>
              <dd>{{ detail.store_name || '—' }} / {{ detail.sku || '—' }}</dd>
            </dl>
          </div>
        </section>

        <section v-if="detail.conflict" class="pc-section">
          <h3 class="pc-section-title">来源冲突</h3>
          <p class="mb-2 text-sm text-gray-500 dark:text-dark-400">
            {{ labelOf(SOURCE_KIND_LABEL, detail.conflict.source_kind) }}来源送来的执行相关字段与当前不同（{{ formatDateTime(detail.conflict.at) }}），任务暂停在「待核实」。
          </p>
          <el-table :data="conflictRows" size="small" class="eq-table-card">
            <el-table-column label="字段" prop="label" width="130" />
            <el-table-column label="当前值" min-width="160">
              <template #default="{ row }"><span class="text-gray-900 dark:text-white">{{ row.current }}</span></template>
            </el-table-column>
            <el-table-column label="来源新值" min-width="160">
              <template #default="{ row }"><span class="font-medium text-amber-700 dark:text-amber-400">{{ row.incoming }}</span></template>
            </el-table-column>
          </el-table>
          <div v-if="can('resolve_conflict')" class="mt-3 flex flex-wrap gap-2">
            <el-button :loading="busy === '处理冲突'" @click="resolveConflict('keep')">保留当前</el-button>
            <el-button type="primary" :loading="busy === '处理冲突'" @click="resolveConflict('accept')">接受来源新值</el-button>
          </div>
        </section>

        <section class="pc-section">
          <h3 class="pc-section-title">来源</h3>
          <div v-if="!detail.sources?.length" class="text-sm text-gray-500 dark:text-dark-400">没有来源记录</div>
          <div class="space-y-2">
            <div
              v-for="s in detail.sources"
              :key="s.id"
              class="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-gray-100 px-4 py-3 dark:border-dark-700"
            >
              <div class="min-w-0">
                <div class="flex flex-wrap items-center gap-2 text-sm text-gray-900 dark:text-gray-100">
                  <el-tag size="small" :type="s.kind === 'feishu' ? 'primary' : 'info'" disable-transitions>{{ labelOf(SOURCE_KIND_LABEL, s.kind) }}</el-tag>
                  <span class="min-w-0 break-all">{{ sourceTitle(s) }}</span>
                  <el-tag v-if="s.is_writeback_target" size="small" type="success" disable-transitions>回填目标</el-tag>
                </div>
                <div class="mt-1 text-xs text-gray-500 dark:text-dark-400">
                  首次 {{ formatDateTime(s.first_seen_at) }} · 最近 {{ formatDateTime(s.last_seen_at) }}
                </div>
              </div>
              <div class="flex flex-wrap items-center gap-1">
                <el-button
                  v-if="s.kind === 'feishu' && !s.is_writeback_target && can('set_writeback_target')"
                  text
                  size="small"
                  type="primary"
                  :loading="busy === '设为回填目标'"
                  @click="setWritebackTarget(s)"
                >
                  设为回填目标
                </el-button>
                <el-button text size="small" @click="snapshotSource = s"><AppIcon name="eye" size="sm" class="mr-1" />查看快照</el-button>
              </div>
            </div>
          </div>
          <p v-if="feishuSources.length && !hasWritebackTarget" class="mt-2 text-xs text-gray-500 dark:text-dark-400">
            这笔任务还没有回填目标：采购结果不会写回飞书。需要写回时，在上面选一条飞书记录设为回填目标。
          </p>
          <p v-else-if="feishuSources.length > 1" class="mt-2 text-xs text-gray-500 dark:text-dark-400">
            同一平台订单号有多条飞书记录时，只有回填目标会收到采购结果。
          </p>
        </section>

        <section class="pc-section">
          <h3 class="pc-section-title">采购尝试</h3>
          <div v-if="!detail.attempts?.length" class="text-sm text-gray-500 dark:text-dark-400">还没有采购尝试</div>
          <el-timeline v-else>
            <el-timeline-item
              v-for="a in detail.attempts"
              :key="a.id"
              :timestamp="formatDateTime(a.claimed_at)"
              placement="top"
              :type="a.status === 'succeeded' ? 'success' : a.status === 'failed' || a.status === 'unknown' ? 'danger' : 'primary'"
            >
              <div class="rounded-xl border border-gray-100 p-4 dark:border-dark-700">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="text-sm font-semibold text-gray-900 dark:text-white">第 {{ a.attempt_no }} 次 · {{ labelOf(ATTEMPT_KIND_LABEL, a.kind) }}</span>
                  <el-tag size="small" :type="tagOf(ATTEMPT_STATUS_TAG, a.status)" disable-transitions>{{ labelOf(ATTEMPT_STATUS_LABEL, a.status) }}</el-tag>
                  <el-tag v-if="a.simulated" size="small" type="warning" disable-transitions>模拟</el-tag>
                  <el-tag v-if="a.submitted_at" size="small" type="danger" disable-transitions>已登记提交</el-tag>
                </div>
                <dl class="pc-kv mt-3">
                  <dt>账号 / 设备</dt>
                  <dd>{{ a.account_name || a.buyer_account_name || '—' }} / {{ a.device_name || '—' }}</dd>
                  <dt>额度日期</dt>
                  <dd>{{ formatDate(a.bj_date) }}</dd>
                  <template v-if="a.stage">
                    <dt>进度</dt>
                    <dd>{{ a.stage }}</dd>
                  </template>
                  <template v-if="a.submitted_at">
                    <dt>登记提交</dt>
                    <dd>{{ formatDateTime(a.submitted_at, true) }}</dd>
                  </template>
                  <dt>结束</dt>
                  <dd>{{ formatDateTime(a.finished_at) }}</dd>
                  <template v-if="a.amazon_order_no">
                    <dt>亚马逊订单号</dt>
                    <dd><span class="code">{{ a.amazon_order_no }}</span></dd>
                  </template>
                  <template v-if="a.card_last4">
                    <dt>卡尾号</dt>
                    <dd>{{ a.card_last4 }}</dd>
                  </template>
                  <template v-if="a.delivery_text || a.delivery_date">
                    <dt>预计送达</dt>
                    <dd>{{ a.delivery_text || formatDate(a.delivery_date) }}<template v-if="a.delivery_days !== null && a.delivery_days !== undefined">（{{ a.delivery_days }} 天）</template></dd>
                  </template>
                  <template v-if="a.error_code">
                    <dt>错误</dt>
                    <dd class="text-red-600 dark:text-red-400">{{ errorCodeLabel(a.error_code) }}<template v-if="a.error_detail">：{{ a.error_detail }}</template></dd>
                  </template>
                  <template v-if="a.resolution">
                    <dt>人工核实</dt>
                    <dd>
                      {{ labelOf(RESOLUTION_LABEL, a.resolution) }}<template v-if="a.resolution_note"> · {{ a.resolution_note }}</template>
                      <div class="text-xs text-gray-500 dark:text-dark-400">{{ a.resolved_by_name || '—' }} · {{ formatDateTime(a.resolved_at) }}</div>
                    </dd>
                  </template>
                </dl>
                <div v-if="a.total || a.subtotal" class="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-5">
                  <div v-for="[k, v] in attemptAmounts(a)" :key="k" class="rounded-lg bg-gray-50 px-3 py-2 dark:bg-dark-900/60">
                    <div class="text-xs text-gray-500 dark:text-dark-400">{{ k }}</div>
                    <div class="text-sm font-medium text-gray-900 dark:text-white">{{ v }}</div>
                  </div>
                </div>
                <div v-if="a.guard_verdict" class="mt-3">
                  <div class="mb-1 flex flex-wrap items-center gap-2 text-sm">
                    <span class="text-gray-500 dark:text-dark-400">护栏</span>
                    <el-tag size="small" :type="a.guard_verdict.verdict === 'pass' ? 'success' : 'danger'" disable-transitions>
                      {{ a.guard_verdict.verdict === 'pass' ? '通过' : '拦截' }}
                    </el-tag>
                    <span v-if="a.guard_verdict.code" class="text-red-600 dark:text-red-400">
                      {{ labelOf(GUARD_CODE_LABEL, a.guard_verdict.code) }}
                    </span>
                  </div>
                  <ul v-if="a.guard_verdict.reasons?.length" class="mb-2 list-disc pl-5 text-sm text-gray-700 dark:text-gray-300">
                    <li v-for="(r, i) in a.guard_verdict.reasons" :key="i">{{ r }}</li>
                  </ul>
                  <table v-if="normalizeGuardChecks(a.guard_verdict).length" class="pc-checks">
                    <tbody>
                      <tr v-for="c in normalizeGuardChecks(a.guard_verdict)" :key="c.key">
                        <td class="w-24 text-gray-500 dark:text-dark-400">{{ c.label }}</td>
                        <td class="w-20 whitespace-nowrap">
                          <span v-if="c.state === 'pass'" class="inline-flex items-center gap-1 text-emerald-700 dark:text-emerald-400"><AppIcon name="check" size="xs" />{{ c.stateText }}</span>
                          <span v-else-if="c.state === 'fail'" class="inline-flex items-center gap-1 text-red-600 dark:text-red-400"><AppIcon name="x" size="xs" />{{ c.stateText }}</span>
                          <span v-else-if="c.state === 'unknown'" class="text-amber-700 dark:text-amber-400">{{ c.stateText }}</span>
                          <span v-else class="text-gray-500 dark:text-dark-400">{{ c.stateText }}</span>
                        </td>
                        <td class="text-gray-700 dark:text-gray-300">{{ c.detail || '—' }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>
            </el-timeline-item>
          </el-timeline>
        </section>

        <section class="pc-section">
          <h3 class="pc-section-title">事件</h3>
          <div v-if="!allEvents.length" class="text-sm text-gray-500 dark:text-dark-400">没有事件</div>
          <el-timeline v-else>
            <el-timeline-item v-for="e in allEvents" :key="e.id" :timestamp="formatDateTime(e.at)" size="normal">
              <div class="text-sm">
                <span class="font-medium text-gray-900 dark:text-white">{{ eventKindLabel(e.kind) }}</span>
                <span class="ml-2 text-xs text-gray-500 dark:text-dark-400">{{ actorText(e) }}</span>
              </div>
              <div v-if="eventSummary(e)" class="mt-0.5 text-sm text-gray-600 dark:text-gray-300">{{ eventSummary(e) }}</div>
            </el-timeline-item>
          </el-timeline>
          <div v-if="hasOlderEvents" class="mt-2">
            <el-button size="small" :loading="olderLoading" @click="loadOlderEvents">显示更早的事件</el-button>
          </div>
          <p v-else-if="olderTotal !== null" class="mt-2 text-xs text-gray-500 dark:text-dark-400">共 {{ formatNumber(allEvents.length) }} 条事件，已全部显示</p>
        </section>
      </template>
    </div>

    <template v-if="detail && hasActions" #footer>
      <div class="flex flex-wrap items-center justify-end gap-2">
        <el-button v-if="can('cancel')" type="danger" plain :loading="busy === '取消任务'" @click="cancelOrder">取消任务</el-button>
        <el-button v-if="can('uncancel')" :loading="busy === '撤销取消'" @click="uncancel">撤销取消</el-button>
        <el-button v-if="can('mark_amazon_cancelled')" type="danger" plain :loading="busy === '登记亚马逊取消'" @click="markAmazonCancelled">
          登记亚马逊取消
        </el-button>
        <el-button v-if="can('unassign')" :loading="busy === '解除分配'" @click="unassign">解除分配</el-button>
        <el-button v-if="can('override')" @click="openOverride">修改执行参数</el-button>
        <el-button v-if="can('assign')" @click="openAssign">改派账号</el-button>
        <template v-if="can('review')">
          <el-button type="danger" plain :loading="busy === '人审拒绝'" @click="reject">拒绝</el-button>
          <el-button type="primary" :loading="busy === '人审通过'" @click="approve">人审通过</el-button>
        </template>
        <el-button v-if="can('resume')" type="primary" :loading="busy === '恢复任务'" @click="resume">恢复</el-button>
        <el-button v-if="can('resolve_unknown')" type="primary" @click="openUnknown">核实结果</el-button>
        <el-button v-if="can('clear_attention')" :loading="busy === '标记已处理'" @click="clearAttention">标记已处理</el-button>
        <template v-if="can('repurchase')">
          <el-button :loading="busy === '关闭任务'" @click="repurchase('decline')">不重采</el-button>
          <el-button type="primary" :loading="busy === '同意重采'" @click="repurchase('approve')">同意重采</el-button>
        </template>
        <template v-if="can('resolve_conflict') && !detail.conflict">
          <el-button :loading="busy === '处理冲突'" @click="resolveConflict('keep')">保留当前</el-button>
          <el-button type="primary" :loading="busy === '处理冲突'" @click="resolveConflict('accept')">接受来源新值</el-button>
        </template>
      </div>
    </template>

    <!-- 快照查看 -->
    <el-dialog :model-value="!!snapshotSource" title="来源快照" width="640px" append-to-body @close="snapshotSource = null">
      <p v-if="snapshotSource" class="mb-3 text-sm text-gray-500 dark:text-dark-400">{{ sourceTitle(snapshotSource) }}</p>
      <el-table :data="snapshotRows" size="small" max-height="420" class="eq-table-card">
        <el-table-column label="来源字段" prop="key" width="180" />
        <el-table-column label="值" prop="value" min-width="240" />
        <template #empty><span class="text-sm text-gray-500 dark:text-dark-400">快照为空</span></template>
      </el-table>
      <template #footer><el-button @click="snapshotSource = null">关闭</el-button></template>
    </el-dialog>

    <!-- 改派 -->
    <el-dialog v-model="assignOpen" title="改派账号" width="560px" append-to-body>
      <p class="mb-4 text-sm text-gray-500 dark:text-dark-400">
        人工改派后任务固定在该账号，不参与自动重算。目标账号须已开启采购、金额区间匹配（本单商品小计 {{ formatMoney(detail?.subtotal_eff) }}），且计划日期当天有剩余额度。
      </p>
      <el-form label-position="top">
        <el-form-item label="目标账号">
          <el-select v-model="assignAccountId" :loading="accountsLoading" placeholder="选择买家账号" class="w-full" filterable>
            <el-option v-for="a in accounts" :key="a.id" :value="a.id" :label="a.name" :disabled="!!accountBlock(a)">
              <div class="flex items-center justify-between gap-4">
                <span>{{ a.name }}</span>
                <span class="text-xs text-gray-500 dark:text-dark-400">
                  {{ formatAmountRange(a.min_amount, a.max_amount) }} · 今日剩余 {{ formatNumber(a.quota_today?.available) }}/{{ formatNumber(a.quota_today?.limit) }}
                  <template v-if="accountBlock(a)"> · {{ accountBlock(a) }}</template>
                </span>
              </div>
            </el-option>
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="assignOpen = false">取消</el-button>
        <el-button type="primary" :disabled="!assignAccountId" :loading="busy === '改派账号'" @click="submitAssign">改派</el-button>
      </template>
    </el-dialog>

    <!-- 修改执行参数 -->
    <el-dialog v-model="overrideOpen" title="修改执行参数" width="560px" append-to-body>
      <p class="mb-4 text-sm text-gray-500 dark:text-dark-400">
        覆盖值只作用于采购，不回填飞书或文件；之后的来源导入不会覆盖它。留空表示使用来源原值。
      </p>
      <el-form ref="overrideRef" :model="overrideForm" :rules="overrideRules" label-position="top">
        <div class="grid gap-x-6 sm:grid-cols-3">
          <el-form-item label="ASIN" prop="asin">
            <el-input v-model="overrideForm.asin" :placeholder="detail?.original?.asin || detail?.asin || 'B0XXXXXXXX'" maxlength="10" />
          </el-form-item>
          <el-form-item label="数量" prop="quantity">
            <el-input-number v-model="overrideForm.quantity" :min="1" :max="99" :controls="false" style="width: 100%" :placeholder="String(detail?.original?.quantity ?? detail?.quantity ?? '')" />
          </el-form-item>
          <el-form-item label="限价（USD）" prop="price_cap">
            <el-input v-model="overrideForm.price_cap" :placeholder="detail?.original?.price_cap ?? detail?.price_cap ?? '0.00'">
              <template #prefix>$</template>
            </el-input>
          </el-form-item>
        </div>
        <el-form-item label="修改原因" prop="note">
          <el-input v-model="overrideForm.note" type="textarea" :rows="2" placeholder="如 原 ASIN 已下架，改用同款替代" />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="flex items-center justify-between gap-2">
          <el-button v-if="detail?.has_override" type="danger" text :loading="busy === '清除覆盖值'" @click="clearOverride">清除覆盖值</el-button>
          <span v-else />
          <div>
            <el-button @click="overrideOpen = false">取消</el-button>
            <el-button type="primary" :loading="busy === '修改执行参数'" @click="submitOverride">保存</el-button>
          </div>
        </div>
      </template>
    </el-dialog>

    <!-- 结果待核实 -->
    <el-dialog v-model="unknownOpen" title="核实采购结果" width="600px" append-to-body>
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        class="mb-4"
        title="请先在买家账号的亚马逊订单页核实。已登记提交的尝试可能已经下单，确认未下单前不要重新采购。"
      />
      <el-form ref="unknownRef" :model="unknownForm" :rules="unknownRules" label-position="top">
        <el-form-item label="核实结论">
          <el-radio-group v-model="unknownForm.outcome">
            <el-radio-button value="ordered">已下单</el-radio-button>
            <el-radio-button value="not_ordered">未下单</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <template v-if="unknownForm.outcome === 'ordered'">
          <el-form-item label="亚马逊订单号" prop="amazon_order_no">
            <el-input v-model="unknownForm.amazon_order_no" placeholder="123-1234567-1234567" />
          </el-form-item>
          <div class="grid gap-x-6 sm:grid-cols-3">
            <el-form-item label="商品小计" prop="subtotal"><el-input v-model="unknownForm.subtotal" placeholder="0.00"><template #prefix>$</template></el-input></el-form-item>
            <el-form-item label="运费" prop="shipping"><el-input v-model="unknownForm.shipping" placeholder="0.00"><template #prefix>$</template></el-input></el-form-item>
            <el-form-item label="税费" prop="tax"><el-input v-model="unknownForm.tax" placeholder="0.00"><template #prefix>$</template></el-input></el-form-item>
            <el-form-item label="税前总额" prop="total_before_tax"><el-input v-model="unknownForm.total_before_tax" placeholder="0.00"><template #prefix>$</template></el-input></el-form-item>
            <el-form-item label="结算总额" prop="total"><el-input v-model="unknownForm.total" placeholder="0.00"><template #prefix>$</template></el-input></el-form-item>
          </div>
        </template>
        <el-form-item label="核实依据" prop="note">
          <el-input v-model="unknownForm.note" type="textarea" :rows="2" placeholder="如 已在订单页找到对应订单 / 订单页没有这笔订单" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="unknownOpen = false">取消</el-button>
        <el-button :type="unknownForm.outcome === 'ordered' ? 'primary' : 'danger'" :loading="busy === '核实结果'" @click="submitUnknown">
          {{ unknownForm.outcome === 'ordered' ? '登记已下单' : '登记未下单' }}
        </el-button>
      </template>
    </el-dialog>
  </el-drawer>
</template>
