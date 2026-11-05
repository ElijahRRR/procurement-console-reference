/**
 * 后端枚举值 → 中文标签与 el-tag 类型。每张表都必须覆盖 src/api/types.ts 里对应枚举的全部取值
 * （tests/unit/labels.test.ts 会逐一核对，并与 schema.sql、services/models.py 对照）。
 */
import type {
  ActorKind,
  AllocationTrigger,
  ApiErrorCode,
  AssignmentKind,
  AttemptKind,
  AttemptStatus,
  BatchStatus,
  EventEntity,
  FeishuVerdict,
  GuardCode,
  IdentityStatus,
  ImportVerdict,
  MemberRole,
  OrderAction,
  OrderStatus,
  OrderView,
  PluginErrorCode,
  PurchaseMode,
  Resolution,
  ReviewSource,
  ReviewStatus,
  RunEndReason,
  RunTrigger,
  SourceKind,
  SuggestedDateBasis,
  SyncTrigger,
  WritebackState,
  WritebackStatus,
} from '@/api/types'

export type TagType = 'primary' | 'success' | 'warning' | 'danger' | 'info'
export type DotColor = 'green' | 'yellow' | 'red' | 'blue' | 'gray'

// ── 任务状态 ──────────────────────────────────────────────────────────────────

export const ORDER_STATUS_LABEL: Record<OrderStatus, string> = {
  pending_review: '待确认',
  needs_info: '资料待补齐',
  conflict: '待核实',
  queued: '待分配',
  assigned: '已分配',
  executing: '采购中',
  result_unknown: '结果待核实',
  exception: '异常',
  purchased: '已采购',
  cancelled: '已取消',
  repurchase_pending: '待决定重采',
  closed: '已关闭',
}

export const ORDER_STATUS_TAG: Record<OrderStatus, TagType> = {
  pending_review: 'warning',
  needs_info: 'warning',
  conflict: 'danger',
  queued: 'info',
  assigned: 'primary',
  executing: 'primary',
  result_unknown: 'danger',
  exception: 'danger',
  purchased: 'success',
  cancelled: 'info',
  repurchase_pending: 'warning',
  closed: 'info',
}

export const ORDER_VIEW_LABEL: Record<OrderView, string> = {
  today: '今日可采购',
  future: '未来日期',
  unassigned: '待分配',
  pending_review: '待确认',
  needs_info: '资料待补齐',
  conflict: '待核实',
  executing: '采购中',
  result_unknown: '结果待核实',
  purchased: '已采购',
  exception: '异常',
  cancelled: '已取消',
  repurchase_pending: '待决定重采',
  closed: '已关闭',
  all: '全部',
}

// ── 采购尝试 ──────────────────────────────────────────────────────────────────

export const ATTEMPT_STATUS_LABEL: Record<AttemptStatus, string> = {
  claimed: '已领取',
  checking: '校验中',
  guard_passed: '护栏已通过',
  submitting: '已登记提交',
  verifying: '等待银行验证',
  succeeded: '成功',
  failed: '失败',
  unknown: '结果不明',
  aborted: '已中止',
}

export const ATTEMPT_STATUS_TAG: Record<AttemptStatus, TagType> = {
  claimed: 'primary',
  checking: 'primary',
  guard_passed: 'primary',
  submitting: 'warning',
  verifying: 'warning',
  succeeded: 'success',
  failed: 'danger',
  unknown: 'danger',
  aborted: 'info',
}

export const ATTEMPT_KIND_LABEL: Record<AttemptKind, string> = {
  auto: '自动执行',
  retry: '自动重试',
  repurchase: '人工重采',
  external: '来源已有单号',
}

export const RESOLUTION_LABEL: Record<Resolution, string> = {
  ordered: '人工核实：已下单',
  not_ordered: '人工核实：未下单',
  simulation_reset: '已清除模拟结果',
}

/** 护栏拦截码（契约 §4） */
export const GUARD_CODE_LABEL: Record<GuardCode, string> = {
  ITEM_MISMATCH: '结算商品或数量与任务不一致',
  ZIP_MISMATCH: '收货邮编与任务不一致',
  SUBTOTAL_OUT_OF_RANGE: '商品小计不在账号金额区间内',
  PRICE_OVER_CAP: '结算总额超过限价',
  DELIVERY_TOO_LONG: '预计送达超过配送上限',
  DATA_UNRELIABLE: '结算页金额或配送日期读取不可靠',
  CARD_MISMATCH: '支付卡尾号与账号设置不符',
}

/** 护栏检查项（guard_verdict.checks 的键或 name）→ 中文名 */
export const GUARD_CHECK_LABEL: Record<string, string> = {
  item: '商品',
  items: '商品',
  zip: '邮编',
  postal_code: '邮编',
  subtotal_range: '金额区间',
  subtotal: '金额区间',
  amount_range: '金额区间',
  price_cap: '限价',
  cap: '限价',
  delivery: '配送时长',
  delivery_days: '配送时长',
  data: '数据可靠',
  data_reliable: '数据可靠',
  card: '卡尾号',
  card_last4: '卡尾号',
  fulfillment: '配送方式',
  ...GUARD_CODE_LABEL,
}

/** 插件执行错误码（services/models.py ERROR_CODES） */
export const PLUGIN_ERROR_LABEL: Record<PluginErrorCode, string> = {
  OUT_OF_STOCK: '商品无库存',
  QUANTITY_UNAVAILABLE: '无法选择所需数量',
  ADDRESS_FAILED: '收货地址填写失败',
  CART_MISMATCH: '购物车内容与任务不一致',
  UNEXPECTED_PAGE: '页面结构与预期不符',
  PAGE_LOAD_FAILED: '提交前页面加载失败',
  LOGIN_REQUIRED: '买家账号登录失效',
  ACCOUNT_RESTRICTED: '买家账号受限',
  CARD_DECLINED: '银行卡被拒付',
  CAPTCHA_ENCOUNTERED: '遇到验证码或风控验证，需要人工处理',
  IDENTITY_MISMATCH: '浏览器登录的买家账号与绑定不符',
  ABORTED_UPSTREAM_CANCELLED: '上游已取消，已中止',
  ABORTED_BY_USER: '插件端人工中止',
}

/** 输入：尝试的 error_code（插件码或护栏码）→ 输出：中文说明，未知码原样返回 */
export function errorCodeLabel(code: string | null | undefined): string {
  if (!code) return ''
  return (
    (PLUGIN_ERROR_LABEL as Record<string, string>)[code] ?? (GUARD_CODE_LABEL as Record<string, string>)[code] ?? code
  )
}

// ── 统一错误码（§8） ──────────────────────────────────────────────────────────

export const API_ERROR_LABEL: Record<ApiErrorCode, string> = {
  UNAUTHORIZED: '未登录或登录已失效',
  FORBIDDEN: '没有权限',
  NOT_FOUND: '记录不存在',
  VALIDATION: '提交的内容不符合要求',
  CONFLICT: '与当前状态冲突',
  ACCOUNT_BUSY: '账号正在别处执行',
  ATTEMPT_IN_FLIGHT: '已有在途的采购尝试',
  IDENTITY_MISMATCH: '浏览器登录的买家账号与绑定不符',
  NOT_BOUND: '设备未绑定账号',
  ACCOUNT_PAUSED: '账号已暂停执行',
  REAL_PURCHASE_DISABLED: '本阶段只允许模拟执行',
  LEASE_LOST: '执行权已失效',
  FEISHU_NOT_CONFIGURED: '未配置飞书应用',
  FEISHU_ERROR: '飞书接口出错',
  INVALID_TRANSITION: '当前状态不允许此操作',
}

// ── 人审、来源、分配 ──────────────────────────────────────────────────────────

export const REVIEW_STATUS_LABEL: Record<ReviewStatus, string> = {
  approved: '人审通过',
  rejected: '人审拒绝',
  unknown: '缺人审结果',
}

export const REVIEW_STATUS_TAG: Record<ReviewStatus, TagType> = {
  approved: 'success',
  rejected: 'danger',
  unknown: 'warning',
}

export const REVIEW_SOURCE_LABEL: Record<ReviewSource, string> = {
  feishu: '飞书',
  file: '文件',
  console: '控制台',
}

export const SOURCE_KIND_LABEL: Record<SourceKind, string> = {
  feishu: '飞书',
  file: '文件',
}

export const ASSIGNMENT_KIND_LABEL: Record<AssignmentKind, string> = {
  auto: '自动分配',
  manual: '人工改派',
}

export const SUGGESTED_DATE_BASIS_LABEL: Record<SuggestedDateBasis, string> = {
  source: '来源给出',
  computed: '按下单日期 + 6 − 配送天数计算',
}

export const ALLOCATION_TRIGGER_LABEL: Record<AllocationTrigger, string> = {
  intake: '飞书读取后',
  import: '文件导入后',
  manual: '人工重新分配',
  account_change: '账号配置变化后',
  daily: '每日 00:01',
}

// ── 账号与设备 ────────────────────────────────────────────────────────────────

export const PURCHASE_MODE_LABEL: Record<PurchaseMode, string> = {
  manual: '手动执行',
  scheduled: '定时执行',
}

export const IDENTITY_STATUS_LABEL: Record<IdentityStatus, string> = {
  match: '身份一致',
  mismatch: '身份不符',
  signed_out: '未登录亚马逊',
  unknown: '身份未知',
}

export const IDENTITY_STATUS_TAG: Record<IdentityStatus, TagType> = {
  match: 'success',
  mismatch: 'danger',
  signed_out: 'warning',
  unknown: 'info',
}

export const IDENTITY_STATUS_DOT: Record<IdentityStatus, DotColor> = {
  match: 'green',
  mismatch: 'red',
  signed_out: 'yellow',
  unknown: 'gray',
}

export const RUN_TRIGGER_LABEL: Record<RunTrigger, string> = {
  manual: '手动触发',
  scheduled: '定时触发',
}

export const RUN_END_REASON_LABEL: Record<RunEndReason, string> = {
  finished: '正常结束',
  lease_expired: '执行权超时',
  account_paused: '账号已暂停',
  device_revoked: '设备已撤销',
}

export const MEMBER_ROLE_LABEL: Record<MemberRole, string> = {
  owner: '所有者',
  member: '成员',
}

// ── 来源导入与飞书 ────────────────────────────────────────────────────────────

export const IMPORT_VERDICT_LABEL: Record<ImportVerdict, string> = {
  new: '新单',
  update: '更新',
  unchanged: '无变化',
  conflict: '冲突',
  historical: '历史',
  invalid: '无效',
}

export const FEISHU_VERDICT_LABEL: Record<FeishuVerdict, string> = {
  ...IMPORT_VERDICT_LABEL,
  skipped: '跳过',
}

export const VERDICT_TAG: Record<FeishuVerdict, TagType> = {
  new: 'success',
  update: 'primary',
  unchanged: 'info',
  conflict: 'danger',
  historical: 'warning',
  invalid: 'danger',
  skipped: 'info',
}

export const BATCH_STATUS_LABEL: Record<BatchStatus, string> = {
  uploaded: '已上传',
  previewed: '已预览',
  committed: '已提交',
  discarded: '已放弃',
}

export const BATCH_STATUS_TAG: Record<BatchStatus, TagType> = {
  uploaded: 'info',
  previewed: 'warning',
  committed: 'success',
  discarded: 'info',
}

export const SYNC_TRIGGER_LABEL: Record<SyncTrigger, string> = {
  schedule: '定时读取',
  manual: '立即同步',
}

export const WRITEBACK_STATUS_LABEL: Record<WritebackStatus, string> = {
  pending: '待写回',
  done: '已写回',
  failed: '写回失败',
  superseded: '已被新结果取代',
}

export const WRITEBACK_STATUS_TAG: Record<WritebackStatus, TagType> = {
  pending: 'warning',
  done: 'success',
  failed: 'danger',
  superseded: 'info',
}

export const WRITEBACK_STATE_LABEL: Record<WritebackState, string> = {
  disabled: '未启用写回',
  unverified: '回填待验证',
  active: '写回已启用',
}

export const WRITEBACK_STATE_TAG: Record<WritebackState, TagType> = {
  disabled: 'info',
  unverified: 'warning',
  active: 'success',
}

/** 同步 / 预览统计键 → 中文（feishu_sync_runs.stats、预览 stats） */
export const SYNC_STAT_LABEL: Record<string, string> = {
  read: '读取',
  total: '合计',
  new: '新单',
  update: '更新',
  unchanged: '无变化',
  conflict: '冲突',
  historical: '历史',
  skipped: '跳过',
  invalid: '无效',
  pending_review: '待确认',
  needs_info: '资料待补齐',
}

// ── 事件 ──────────────────────────────────────────────────────────────────────

export const EVENT_ENTITY_LABEL: Record<EventEntity, string> = {
  order: '任务',
  attempt: '采购尝试',
  account: '账号',
  device: '设备',
  connection: '飞书连接',
  batch: '导入批次',
  settings: '设置',
  run: '执行批次',
}

export const ACTOR_KIND_LABEL: Record<ActorKind, string> = {
  user: '用户',
  device: '插件',
  system: '系统',
  source: '来源',
}

/**
 * 事件 kind 的中文名。契约没有列出 kind 的取值，这里收常见的写法，未知的原样显示（见 eventKindLabel）。
 */
export const EVENT_KIND_LABEL: Record<string, string> = {
  created: '创建任务',
  ingested: '来源导入',
  source_added: '追加来源',
  source_updated: '来源更新',
  source_changed: '来源内容变化',
  status_changed: '状态变化',
  transition: '状态变化',
  reviewed: '人审',
  review: '人审',
  assigned: '分配账号',
  auto_assigned: '自动分配',
  manual_assigned: '人工改派',
  unassigned: '解除分配',
  override: '修改执行参数',
  override_set: '修改执行参数',
  override_cleared: '清除覆盖值',
  resumed: '异常恢复',
  cancelled: '取消',
  upstream_cancelled: '上游取消',
  uncancelled: '撤销取消',
  conflict: '来源冲突',
  conflict_resolved: '冲突已处理',
  attempt_claimed: '插件领取',
  attempt_started: '开始采购',
  guard: '护栏裁决',
  guard_passed: '护栏通过',
  guard_blocked: '护栏拦截',
  submitting: '登记提交',
  verifying: '等待银行验证',
  attempt_result: '采购结果',
  succeeded: '采购成功',
  failed: '采购失败',
  result_unknown: '结果待核实',
  resolved_unknown: '人工核实结果',
  amazon_cancelled: '亚马逊取消',
  repurchase_approved: '同意重采',
  repurchase_declined: '不重采',
  attention: '需要人工留意',
  upstream_changed_after_start: '上游在采购开始后修改',
  simulation_reset: '清除模拟结果',
  writeback_queued: '写回入队',
  writeback_done: '写回完成',
  writeback_failed: '写回失败',
  paused: '暂停执行',
  account_paused: '暂停执行',
  account_resumed: '恢复执行',
}

/** 输入：事件 kind → 输出：中文名，未知的原样返回 */
export function eventKindLabel(kind: string): string {
  return EVENT_KIND_LABEL[kind] ?? kind
}

// ── 任务动作 ──────────────────────────────────────────────────────────────────

export const ORDER_ACTION_LABEL: Record<OrderAction, string> = {
  review: '人审',
  assign: '改派账号',
  unassign: '解除分配',
  override: '修改执行参数',
  resume: '恢复',
  cancel: '取消任务',
  uncancel: '撤销取消',
  resolve_unknown: '核实结果',
  mark_amazon_cancelled: '登记亚马逊取消',
  repurchase: '决定是否重采',
  resolve_conflict: '处理冲突',
}

// ── 字段名 ────────────────────────────────────────────────────────────────────

/** 任务字段键 → 中文（missing_fields、冲突字段、原值展示用） */
export const ORDER_FIELD_LABEL: Record<string, string> = {
  upstream_order_no: '平台订单号',
  order_line_id: '订单行 ID',
  store_name: '店铺',
  sales_po_no: '采购订单号',
  sku: 'SKU',
  asin: 'ASIN',
  quantity: '数量',
  unit_price: '采购单价',
  sales_total: '销售总额',
  price_cap: '限价',
  order_time: '下单时间',
  order_date_bj: '下单日期（北京）',
  ship_days: '配送天数',
  suggested_purchase_date: '建议采购日期',
  suggested_date_basis: '建议日期依据',
  ship_method: '配送方式',
  recipient_name: '收货人姓名',
  address1: '收货地址',
  address2: '地址 2',
  city: '城市',
  state: '州',
  postal_code: '邮编',
  phone: '收货人电话',
  country: '国家',
  district: '区',
  review_value: '人工审核',
  external_amz_order_no: 'AMZ 订单号',
}

/** 输入：字段键或中文名 → 输出：中文名（已是中文的原样返回） */
export function fieldLabel(key: string): string {
  return ORDER_FIELD_LABEL[key] ?? key
}

// ── 便捷函数 ──────────────────────────────────────────────────────────────────

/** 输入：标签表、取值 → 输出：中文标签；未知取值原样返回，空值返回 "—" */
export function labelOf<K extends string>(map: Record<K, string>, v: K | string | null | undefined): string {
  if (v === null || v === undefined || v === '') return '—'
  return (map as Record<string, string>)[v] ?? v
}

/** 输入：标签类型表、取值 → 输出：el-tag 类型；未知取值为 info */
export function tagOf<K extends string>(map: Record<K, TagType>, v: K | string | null | undefined): TagType {
  if (!v) return 'info'
  return (map as Record<string, TagType>)[v] ?? 'info'
}
