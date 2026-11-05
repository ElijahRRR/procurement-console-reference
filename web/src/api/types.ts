/**
 * 控制台 HTTP 接口的数据形状（docs/development/开发契约-v2.md §8、§10）。
 *
 * 约定：金额是两位小数的字符串（"52.00"），可空；日期 YYYY-MM-DD；时刻 ISO 8601 带时区；
 * 字段缺失与 null 等价，所以凡是后端可能不给的字段都写成可选或可空。
 */

// ── 枚举（与 schema.sql 的 CHECK 约束、services/models.py 一致；tests/unit/labels.test.ts 会核对）──

export const ORDER_STATUSES = [
  'pending_review',
  'needs_info',
  'conflict',
  'queued',
  'assigned',
  'executing',
  'result_unknown',
  'exception',
  'purchased',
  'cancelled',
  'repurchase_pending',
  'closed',
] as const
export type OrderStatus = (typeof ORDER_STATUSES)[number]

export const ORDER_VIEWS = [
  'today',
  'future',
  'unassigned',
  'pending_review',
  'needs_info',
  'conflict',
  'executing',
  'result_unknown',
  'purchased',
  'exception',
  'cancelled',
  'repurchase_pending',
  'closed',
  'all',
] as const
export type OrderView = (typeof ORDER_VIEWS)[number]

export const ATTEMPT_STATUSES = [
  'claimed',
  'checking',
  'guard_passed',
  'submitting',
  'verifying',
  'succeeded',
  'failed',
  'unknown',
  'aborted',
] as const
export type AttemptStatus = (typeof ATTEMPT_STATUSES)[number]

export const ATTEMPT_KINDS = ['auto', 'retry', 'repurchase', 'external'] as const
export type AttemptKind = (typeof ATTEMPT_KINDS)[number]

export const REVIEW_STATUSES = ['approved', 'rejected', 'unknown'] as const
export type ReviewStatus = (typeof REVIEW_STATUSES)[number]

export const REVIEW_SOURCES = ['feishu', 'file', 'console'] as const
export type ReviewSource = (typeof REVIEW_SOURCES)[number]

export const SOURCE_KINDS = ['feishu', 'file'] as const
export type SourceKind = (typeof SOURCE_KINDS)[number]

export const ASSIGNMENT_KINDS = ['auto', 'manual'] as const
export type AssignmentKind = (typeof ASSIGNMENT_KINDS)[number]

export const SUGGESTED_DATE_BASES = ['source', 'computed'] as const
export type SuggestedDateBasis = (typeof SUGGESTED_DATE_BASES)[number]

export const IDENTITY_STATUSES = ['unknown', 'match', 'mismatch', 'signed_out'] as const
export type IdentityStatus = (typeof IDENTITY_STATUSES)[number]

export const PURCHASE_MODES = ['manual', 'scheduled'] as const
export type PurchaseMode = (typeof PURCHASE_MODES)[number]

export const WRITEBACK_STATUSES = ['pending', 'done', 'failed', 'superseded'] as const
export type WritebackStatus = (typeof WRITEBACK_STATUSES)[number]

export const WRITEBACK_STATES = ['disabled', 'unverified', 'active'] as const
export type WritebackState = (typeof WRITEBACK_STATES)[number]

export const IMPORT_VERDICTS = ['new', 'update', 'unchanged', 'conflict', 'historical', 'invalid'] as const
export type ImportVerdict = (typeof IMPORT_VERDICTS)[number]

export const FEISHU_VERDICTS = ['new', 'update', 'unchanged', 'historical', 'skipped', 'conflict', 'invalid'] as const
export type FeishuVerdict = (typeof FEISHU_VERDICTS)[number]

export const BATCH_STATUSES = ['uploaded', 'previewed', 'committed', 'discarded'] as const
export type BatchStatus = (typeof BATCH_STATUSES)[number]

export const SYNC_TRIGGERS = ['schedule', 'manual'] as const
export type SyncTrigger = (typeof SYNC_TRIGGERS)[number]

export const RUN_TRIGGERS = ['manual', 'scheduled'] as const
export type RunTrigger = (typeof RUN_TRIGGERS)[number]

export const RUN_END_REASONS = ['finished', 'lease_expired', 'device_revoked', 'simulation_reset'] as const
export type RunEndReason = (typeof RUN_END_REASONS)[number]

export const ALLOCATION_TRIGGERS = ['intake', 'import', 'manual', 'account_change', 'daily'] as const
export type AllocationTrigger = (typeof ALLOCATION_TRIGGERS)[number]

export const EVENT_ENTITIES = ['order', 'attempt', 'account', 'device', 'connection', 'batch', 'settings', 'run'] as const
export type EventEntity = (typeof EVENT_ENTITIES)[number]

export const ACTOR_KINDS = ['user', 'device', 'system', 'source'] as const
export type ActorKind = (typeof ACTOR_KINDS)[number]

export const RESOLUTIONS = ['ordered', 'not_ordered', 'simulation_reset'] as const
export type Resolution = (typeof RESOLUTIONS)[number]

export const MEMBER_ROLES = ['owner', 'member'] as const
export type MemberRole = (typeof MEMBER_ROLES)[number]

/** allowed_actions 的全集（§10 任务详情） */
export const ORDER_ACTIONS = [
  'review',
  'assign',
  'unassign',
  'override',
  'resume',
  'cancel',
  'uncancel',
  'resolve_unknown',
  'mark_amazon_cancelled',
  'repurchase',
  'resolve_conflict',
  'clear_attention',
  'set_writeback_target',
] as const
export type OrderAction = (typeof ORDER_ACTIONS)[number]

/** 护栏拦截码（§4） */
export const GUARD_CODES = [
  'ITEM_MISMATCH',
  'ZIP_MISMATCH',
  'SUBTOTAL_OUT_OF_RANGE',
  'PRICE_OVER_CAP',
  'DELIVERY_TOO_LONG',
  'DATA_UNRELIABLE',
  'CARD_MISMATCH',
  'GUARD_BLOCKED',
] as const
export type GuardCode = (typeof GUARD_CODES)[number]

/** 插件上报的执行错误码（services/models.py ERROR_CODES，不含护栏码） */
export const PLUGIN_ERROR_CODES = [
  'OUT_OF_STOCK',
  'QUANTITY_UNAVAILABLE',
  'ADDRESS_FAILED',
  'CART_MISMATCH',
  'UNEXPECTED_PAGE',
  'PAGE_LOAD_FAILED',
  'LOGIN_REQUIRED',
  'ACCOUNT_RESTRICTED',
  'CARD_DECLINED',
  'CAPTCHA_ENCOUNTERED',
  'IDENTITY_MISMATCH',
  'ABORTED_UPSTREAM_CANCELLED',
  'ABORTED_BY_USER',
  'SUBMIT_REJECTED',
  'PLUGIN_SIMULATION_ONLY',
  'ABORTED_BY_SERVER',
] as const
export type PluginErrorCode = (typeof PLUGIN_ERROR_CODES)[number]

/** 统一错误码（§8） */
export const API_ERROR_CODES = [
  'UNAUTHORIZED',
  'FORBIDDEN',
  'NOT_FOUND',
  'VALIDATION',
  'CONFLICT',
  'ACCOUNT_BUSY',
  'ATTEMPT_IN_FLIGHT',
  'IDENTITY_MISMATCH',
  'NOT_BOUND',
  'ACCOUNT_PAUSED',
  'REAL_PURCHASE_DISABLED',
  'LEASE_LOST',
  'FEISHU_NOT_CONFIGURED',
  'FEISHU_ERROR',
  'INVALID_TRANSITION',
  'SOURCE_CHANGED',
  'GUARD_BLOCKED',
  'RATE_LIMITED',
  'METHOD_NOT_ALLOWED',
  'INTERNAL',
] as const
export type ApiErrorCode = (typeof API_ERROR_CODES)[number]

/**
 * 插件进度上报被叫停的原因（POST /plugin/v1/attempts/{id}/progress 的 cancel_code，services/execution.CANCEL_CODES）。
 * attempt.progress 事件的 detail 里也记着它。
 */
export const PROGRESS_CANCEL_CODES = [
  'UPSTREAM_CANCELLED',
  'ACCOUNT_ARCHIVED',
  'ACCOUNT_PAUSED',
  'IDENTITY_MISMATCH',
  'IDENTITY_STALE',
  'SOURCE_HOLD',
  'SOURCE_CHANGED',
  'ATTEMPT_FINISHED',
] as const
export type ProgressCancelCode = (typeof PROGRESS_CANCEL_CODES)[number]

/** 任务上冲突的种类（purchase_orders.conflict.kind，services/orders._CONFLICT_RANK） */
export const CONFLICT_KINDS = ['fields', 'review', 'external_order_no', 'source_key_changed', 'admission'] as const
export type ConflictKind = (typeof CONFLICT_KINDS)[number]

/** 账号暂停的起因（账号输出的 pause_cause，services/accounts.PAUSE_CAUSES；未暂停为 null） */
export const PAUSE_CAUSES = ['result_unknown', 'account_error', 'manual'] as const
export type PauseCause = (typeof PAUSE_CAUSES)[number]

// ── 通用 ──────────────────────────────────────────────────────────────────────

/** 金额：两位小数字符串，可空 */
export type Money = string | null
/** 日期 YYYY-MM-DD */
export type DateStr = string
/** ISO 8601 时刻 */
export type Timestamp = string

export interface Page<T> {
  total: number
  rows: T[]
}

export interface PageQuery {
  page?: number
  page_size?: number
}

export interface Ref {
  id: number
  name: string
}

// ── 认证 ──────────────────────────────────────────────────────────────────────

export interface AuthState {
  needs_setup: boolean
}

export interface Me {
  user: { id: number; username: string; display_name: string | null }
  workspace: { id: number; name: string }
  role: MemberRole | string
}

// ── 任务 ──────────────────────────────────────────────────────────────────────

export interface LatestAttempt {
  id: number
  status: AttemptStatus
  amazon_order_no: string | null
  total: Money
  simulated: boolean
  error_code: string | null
  finished_at: Timestamp | null
}

export interface OrderRow {
  id: number
  upstream_order_no: string
  store_name?: string | null
  sku?: string | null
  asin: string | null
  asin_eff: string | null
  quantity: number | null
  quantity_eff: number | null
  unit_price: Money
  subtotal_eff: Money
  price_cap: Money
  price_cap_eff: Money
  has_override: boolean
  suggested_purchase_date: DateStr | null
  order_time: Timestamp | null
  recipient_name: string | null
  state: string | null
  postal_code: string | null
  status: OrderStatus
  status_zh?: string | null
  status_reason: string | null
  missing_fields: string[]
  review_status: ReviewStatus
  needs_attention: boolean
  attention_note: string | null
  assigned_account: Ref | null
  assignment_kind: AssignmentKind | null
  assignment_note: string | null
  primary_source_kind: SourceKind
  source_kinds: SourceKind[]
  latest_attempt: LatestAttempt | null
  updated_at: Timestamp
}

export interface Address {
  recipient_name: string | null
  address1: string | null
  address2: string | null
  city: string | null
  state: string | null
  postal_code: string | null
  phone: string | null
  country: string | null
  district: string | null
}

export interface OrderOriginal extends Partial<Address> {
  asin: string | null
  quantity: number | null
  price_cap: Money
  unit_price: Money
  sales_total?: Money
  ship_days: number | null
  order_time: Timestamp | null
  order_date_bj?: DateStr | null
  suggested_purchase_date: DateStr | null
  suggested_date_basis: SuggestedDateBasis | null
  ship_method?: string | null
  store_name?: string | null
  sales_po_no?: string | null
  sku?: string | null
  order_line_id?: string | null
  review_value?: string | null
  external_amz_order_no?: string | null
}

export interface OrderOverride {
  asin: string | null
  quantity: number | null
  price_cap: Money
  note: string | null
  by_name: string | null
  at: Timestamp | null
}

/**
 * 任务上的冲突（契约 §2）。kind 决定 resolve-conflict 的 accept 做什么：fields / review → 写入来源新值；
 * external_order_no → 登记为已采购（单号格式不对或仍属别的任务时服务端 422，只能保留）；admission → 取消任务；
 * source_key_changed → 取消原任务并解除那条来源关联。
 */
export interface OrderConflict {
  /** 旧数据可能没有（按 fields 处理） */
  kind?: ConflictKind | string | null
  source_kind: SourceKind | string
  /** 中文原因 */
  reason?: string | null
  /** {字段: [当前值, 来源新值]} */
  fields: Record<string, [unknown, unknown]>
  /** 接受时写入的来源新值 {字段: 值} */
  incoming?: Record<string, unknown> | null
  /** 送来冲突的来源 {kind, connection_id, record_id} 或 {kind, batch_id, row_no} */
  source?: Record<string, unknown> | null
  at: Timestamp | null
}

export interface OrderSource {
  id: number
  kind: SourceKind
  connection_id: number | null
  connection_name: string | null
  record_id: string | null
  batch_id: number | null
  row_no: number | null
  filename: string | null
  is_writeback_target: boolean
  /** 这条来源最近一次记录说上游已取消（只由文件快照说取消、没有飞书来源仍说取消时，控制台可以撤销取消） */
  upstream_cancelled?: boolean
  first_seen_at: Timestamp
  last_seen_at: Timestamp
  snapshot: Record<string, unknown>
}

/** 护栏检查项（services/guard.CHECK_ORDER 的顺序） */
export const GUARD_CHECK_KEYS = ['item', 'zip', 'subtotal', 'price_cap', 'delivery', 'data', 'card'] as const
export type GuardCheckKey = (typeof GUARD_CHECK_KEYS)[number]

/** 护栏单项结果（services/guard.evaluate）：ok 为 true 通过 / false 不通过 / null 缺数据无法判定 */
export interface GuardCheck {
  ok: boolean | null
  expected: unknown
  actual: unknown
}

/** 尝试的 guard_verdict（services/guard.evaluate 的完整输出） */
export interface GuardVerdict {
  verdict: 'pass' | 'block'
  code: string | null
  reasons: string[]
  checks?: Partial<Record<GuardCheckKey, GuardCheck>> & Record<string, GuardCheck>
  /** 判定用的配送天数（无法得出为 null） */
  delivery_days?: number | null
  /** 与限价比较的金额（结算总额 + 礼品卡抵扣） */
  total_for_cap?: Money
  /** 配送方式（只记录，不拦截） */
  fulfillment?: string | null
}

export interface Attempt {
  id: number
  attempt_no: number
  kind: AttemptKind
  account_id: number | null
  account_name?: string | null
  device_id: number | null
  device_name?: string | null
  run_id: number | null
  simulated: boolean
  status: AttemptStatus
  bj_date: DateStr
  stage: string | null
  claimed_at: Timestamp
  heartbeat_at?: Timestamp | null
  submitted_at: Timestamp | null
  finished_at: Timestamp | null
  task_snapshot?: Record<string, unknown> | null
  guard_input?: Record<string, unknown> | null
  guard_verdict: GuardVerdict | null
  amazon_order_no: string | null
  buyer_account_name?: string | null
  subtotal: Money
  shipping: Money
  tax: Money
  total_before_tax: Money
  total: Money
  currency?: string
  card_last4: string | null
  delivery_text?: string | null
  delivery_date?: DateStr | null
  delivery_days?: number | null
  error_code: string | null
  error_detail: string | null
  resolution?: Resolution | null
  resolution_note?: string | null
  resolved_by?: number | null
  resolved_by_name?: string | null
  resolved_at?: Timestamp | null
}

export interface EventRecord {
  id: number
  entity: EventEntity
  entity_id: number | null
  order_id: number | null
  at: Timestamp
  actor_kind: ActorKind
  actor_id: number | null
  actor_name: string | null
  kind: string
  detail: Record<string, unknown>
}

export interface OrderDetail extends OrderRow {
  original: OrderOriginal
  override: OrderOverride | null
  address: Address
  conflict: OrderConflict | null
  upstream_cancelled?: boolean
  upstream_cancel_note?: string | null
  review_source?: ReviewSource | null
  order_date_bj?: DateStr | null
  result_version?: number
  sources: OrderSource[]
  attempts: Attempt[]
  /** 最近的事件（新的在前，最多 ORDER_DETAIL_EVENT_LIMIT 条；更早的用 GET /events?order_id= 分页读） */
  events: EventRecord[]
  allowed_actions: OrderAction[]
}

/** 任务详情里随附的事件条数上限（services/orders.get_detail） */
export const ORDER_DETAIL_EVENT_LIMIT = 200

export interface OrderQuery extends PageQuery {
  view?: OrderView
  q?: string
  account_id?: number
  source?: SourceKind
  /** true 只看需要留意的任务，false 只看不需要的；不给 = 不限 */
  needs_attention?: boolean
}

/** POST /orders/bulk-review 的结果：已处理的任务 id 与跳过的任务（含原因） */
export interface BulkReviewResult {
  updated: number[]
  skipped: { id: number; reason: string }[]
}

export interface ResolveUnknownAmounts {
  subtotal?: string | null
  shipping?: string | null
  tax?: string | null
  total_before_tax?: string | null
  total?: string | null
}

// ── 分配 ──────────────────────────────────────────────────────────────────────

export interface AllocationSummary {
  id: number
  bj_date: DateStr
  at: Timestamp
  trigger: AllocationTrigger | string
  stats: { considered: number; assigned: number; coverage: number; fill: number; unassigned: number }
  uncovered: { account_id: number; name: string; date: DateStr; reason: string }[]
  unassigned: { order_id: number; upstream_order_no: string; reason: string }[]
}

// ── 概览 ──────────────────────────────────────────────────────────────────────

export interface Overview {
  bj_date: DateStr
  counts: Partial<Record<OrderStatus, number>>
  /** 置了「需要留意」提醒的任务数（不论状态） */
  needs_attention?: number
  today_executable: number
  future_assigned: number
  accounts: { total: number; enabled: number; paused: number }
  devices: { total: number; online: number }
  recent_exceptions: OrderRow[]
  last_allocation: AllocationSummary | null
  simulation_only: boolean
  feishu_configured: boolean
}

// ── 账号与设备 ────────────────────────────────────────────────────────────────

export interface QuotaToday {
  limit: number
  used: number
  planned: number
  available: number
}

export interface AccountDevice {
  id: number
  name: string
  online: boolean
  identity_status: IdentityStatus
  last_seen_at: Timestamp | null
}

export interface ActiveRun {
  id: number
  device_id: number
  device_name: string
  started_at: Timestamp
}

export interface Account {
  id: number
  name: string
  amazon_customer_id: string | null
  site: string
  min_amount: Money
  max_amount: Money
  daily_limit: number
  priority: number
  max_delivery_days: number | null
  effective_max_delivery_days: number
  purchase_enabled: boolean
  sync_enabled: boolean
  execution_paused: boolean
  pause_reason: string | null
  paused_at: Timestamp | null
  /** 当前暂停的起因；未暂停为 null */
  pause_cause?: PauseCause | null
  /** 这个账号当前处在「结果待核实」的任务数 */
  open_result_unknown?: number
  purchase_mode: PurchaseMode
  schedule_times: string[]
  default_device_id: number | null
  expected_card_last4: string | null
  notes: string | null
  quota_today: QuotaToday
  devices: AccountDevice[]
  active_run: ActiveRun | null
  created_at: Timestamp
  updated_at: Timestamp
}

/** 新建 / 修改账号的请求体（PATCH 时全部可选） */
export interface AccountInput {
  name?: string
  amazon_customer_id?: string | null
  min_amount?: string
  max_amount?: string | null
  daily_limit?: number
  priority?: number
  max_delivery_days?: number | null
  purchase_enabled?: boolean
  sync_enabled?: boolean
  purchase_mode?: PurchaseMode
  schedule_times?: string[]
  default_device_id?: number | null
  expected_card_last4?: string | null
  notes?: string | null
}

export interface DeviceIdentity {
  signed_in: boolean
  customer_id: string | null
  site: string | null
  page_url: string | null
  at: Timestamp | null
}

export interface Device {
  id: number
  name: string
  browser: string | null
  ext_version: string | null
  bound_account: Ref | null
  online: boolean
  last_seen_at: Timestamp | null
  identity: DeviceIdentity | null
  identity_status: IdentityStatus
  created_at: Timestamp
  revoked_at: Timestamp | null
}

export interface PairingCode {
  code: string
  expires_at: Timestamp
}

// ── 设置 ──────────────────────────────────────────────────────────────────────

export interface Settings {
  default_max_delivery_days: number
  simulation_only: boolean
  logistics_sync_time: string
  real_purchase_available: boolean
  /** 文件导入用的完整准入规则（缺省值 + 工作区改过的项；文件预览不另传规则时就用它） */
  file_admission?: FeishuAdmission
}

/** PATCH /settings 的请求体（file_admission 给出的项覆盖缺省，null = 全部恢复缺省） */
export interface SettingsPatch {
  default_max_delivery_days?: number
  logistics_sync_time?: string
  file_admission?: Partial<FeishuAdmission> | null
}

/** POST /simulation/reset 的结果（services/simulation.reset） */
export interface SimulationResetResult {
  /** 结束的模拟执行批次 id */
  runs_ended: number[]
  /** 标为已中止的模拟尝试条数 */
  attempts_reset: number
  /** 回到采购开始前状态的任务 id */
  orders_restored: number[]
  /** 没能还原的任务与原因 */
  orders_skipped: { order_id: number; reason: string }[]
  /** 恢复执行的账号 id */
  accounts_resumed: number[]
}

// ── 飞书 ──────────────────────────────────────────────────────────────────────

export interface FeishuStatus {
  configured: boolean
}

export interface FeishuAdmission {
  approved_values: string[]
  rejected_values: string[]
  purchaser_values: string[]
  skip_purchase_status_values: string[]
  cancel_after_sale_values: string[]
  import_historical: boolean
}

export interface FeishuConnection {
  id: number
  name: string
  link: string
  app_token: string
  source_table_id: string
  source_view_id: string | null
  result_table_id: string
  field_map: FeishuFieldMap
  admission: Partial<FeishuAdmission>
  writeback_map: Record<string, string>
  writeback_enabled: boolean
  writeback_verified_at: Timestamp | null
  writeback_state: WritebackState
  writeback_test_record: string | null
  poll_minutes: number
  enabled: boolean
  last_sync_at: Timestamp | null
  last_sync_ok_at: Timestamp | null
  last_error: string | null
  last_stats: Record<string, number> | null
  created_at: Timestamp
  updated_at: Timestamp
}

/**
 * 飞书字段对应 {系统字段: 列名 | [候选列名]}：给列表时按顺序回退（前一列为空才读下一列），
 * 如 asin: ['ASIN', '来源码']（services/feishu_fields.DEFAULT_FIELD_MAP）。
 */
export type FeishuFieldMap = Record<string, string | string[]>

export interface FeishuConnectionInput {
  name?: string
  link?: string
  app_token?: string
  source_table_id?: string
  source_view_id?: string | null
  result_table_id?: string
  field_map?: FeishuFieldMap
  admission?: Partial<FeishuAdmission>
  writeback_map?: Record<string, string>
  writeback_enabled?: boolean
  poll_minutes?: number
  enabled?: boolean
}

export interface FeishuTable {
  table_id: string
  name: string
}

export interface ResolvedLink {
  app_token: string
  table_id: string | null
  view_id: string | null
  tables: FeishuTable[]
}

export interface FeishuField {
  field_id: string
  field_name: string
  type: number
  ui_type: string | null
  is_primary: boolean
  writable: boolean
}

export interface FeishuFieldsResult {
  fields: FeishuField[]
  suggested_field_map: FeishuFieldMap
  suggested_writeback_map: Record<string, string>
}

export interface SystemField {
  key: string
  label: string
  required?: boolean
}

export interface SystemFields {
  source_fields: SystemField[]
  result_fields: SystemField[]
}

export interface FeishuPreviewRecord {
  record_id: string
  verdict: FeishuVerdict
  reason: string | null
  record: Record<string, unknown> | null
  issues: string[]
}

export interface FeishuPreview {
  stats: Record<string, number>
  records: FeishuPreviewRecord[]
}

export interface FeishuPreviewInput {
  app_token: string
  table_id: string
  view_id: string | null
  field_map: FeishuFieldMap
  admission: Partial<FeishuAdmission>
  limit: number
}

export interface SyncRun {
  id: number
  connection_id: number
  trigger: SyncTrigger
  started_at: Timestamp
  finished_at: Timestamp | null
  ok: boolean | null
  stats: Record<string, number> | null
  error: string | null
}

/** 读取中有问题的记录样本（跳过、无效、待核实） */
export interface SyncProblem {
  record_id: string
  upstream_order_no: string | null
  verdict: FeishuVerdict
  reason: string | null
}

/** POST /feishu/connections/{id}/sync 的结果（services/feishu_intake.run_connection） */
export interface FeishuSyncResult {
  read: number
  new: number
  update: number
  unchanged: number
  conflict: number
  historical: number
  skipped: number
  invalid: number
  run_id: number
  problems: SyncProblem[]
  /** 有新单、更新、待核实或历史时随后分配一次的摘要；没有分配为 null */
  allocation: AllocationSummary | null
}

/** 写回验收中读回值与写入值不一致的列 */
export interface WritebackMismatch {
  result_field: string | null
  column: string
  expected: unknown
  actual: unknown
}

/** POST /feishu/connections/{id}/verify-writeback 的结果（services/feishu_connections.verify_writeback） */
export interface VerifyWritebackResult {
  /** 全部一致才为 true（此时记下 writeback_verified_at） */
  ok: boolean
  mismatches: WritebackMismatch[]
  /** 写入的测试值 {列名: 值} */
  written: Record<string, unknown>
  /** 测试记录的原值是否已写回 */
  restored: boolean
  /** 恢复原值时飞书的报错 */
  restore_error: string | null
  /** 写入 / 读回时飞书的报错 */
  error: string | null
  connection: FeishuConnection
}

// ── 写回 ──────────────────────────────────────────────────────────────────────

export interface Writeback {
  id: number
  purchase_order_id: number
  upstream_order_no: string
  connection_id: number
  connection_name: string | null
  record_id: string
  result_version: number
  fields: Record<string, unknown>
  status: WritebackStatus
  tries: number
  last_error: string | null
  next_try_at: Timestamp | null
  created_at: Timestamp
  done_at: Timestamp | null
}

// ── 文件导入 ──────────────────────────────────────────────────────────────────

export interface ImportSheet {
  name: string
  /** 数据行数（从批次列表继续导入时可能不知道，为 null） */
  row_count: number | null
  headers: string[]
}

export interface ImportTemplateRef {
  id: number
  name: string
}

/** 工作区共用的导入模板（同名保存覆盖对应关系） */
export interface ImportTemplate extends ImportTemplateRef {
  mapping: Record<string, string | null>
  created_at?: Timestamp
  /** 最初创建它的人（显示名，没有显示名用用户名）；用户已删除时为 null */
  created_by_name?: string | null
}

/** 同一工作区里内容完全相同的最近一次上传（未放弃的） */
export interface PreviousUpload {
  id: number
  status: BatchStatus
  created_at: Timestamp
}

export interface UploadResult {
  batch_id: number
  filename: string
  sheets: ImportSheet[]
  sheet: string
  headers: string[]
  suggested_mapping: Record<string, string | null>
  system_fields: SystemField[]
  templates: ImportTemplateRef[]
  /** 表头覆盖了模板全部对应列的工作区模板（对应列最多的那个），没有为 null */
  matched_template_id?: number | null
  /** 这个文件已经上传过时的那次批次，没有为 null */
  previous_upload?: PreviousUpload | null
}

export interface BatchStats {
  total: number
  new: number
  update: number
  unchanged: number
  conflict: number
  historical: number
  invalid: number
  pending_review: number
  needs_info: number
}

export interface ImportBatch {
  id: number
  filename: string
  sheet_name: string | null
  status: BatchStatus
  stats: Partial<BatchStats> | null
  /** 最近一次（建议或预览时）用的字段对应 {表头: 系统字段 | null}，键为该工作表的全部表头 */
  mapping?: Record<string, string | null> | null
  /** 原文件字节数（已放弃的批次为 0） */
  size?: number | null
  /** GET /imports/{id}：原文件的全部工作表（读不到原文件时为空列表） */
  sheets?: ImportSheet[]
  /** GET /imports/{id}：批次所选工作表的表头（按原文件顺序；读不到原文件时为空列表） */
  headers?: string[]
  created_at: Timestamp
  committed_at: Timestamp | null
  uploaded_by_name: string | null
}

export interface ImportRow {
  row_no: number
  verdict: ImportVerdict
  verdict_zh?: string | null
  issues: string[]
  /** 结论原因（如「与第 3 行的平台订单号重复」） */
  reason?: string | null
  /** 预计（提交后为实际）任务状态 */
  status?: OrderStatus | null
  suggested_purchase_date?: DateStr | null
  suggested_date_basis?: SuggestedDateBasis | null
  upstream_order_no: string | null
  normalized: Record<string, unknown> | null
  raw: Record<string, unknown>
  purchase_order_id?: number | null
}

export interface ImportPreview {
  batch: ImportBatch
  stats: Partial<BatchStats>
  rows: Page<ImportRow>
}

/** POST /imports/{id}/commit 的结果（services/imports.commit） */
export interface ImportCommitResult {
  batch: ImportBatch
  /** 提交后的实际统计 */
  stats: Partial<BatchStats>
  /** 提交后随即分配一次的摘要 */
  allocation: AllocationSummary | null
}
