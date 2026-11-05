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

export const RUN_END_REASONS = ['finished', 'lease_expired', 'account_paused', 'device_revoked'] as const
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
] as const
export type ApiErrorCode = (typeof API_ERROR_CODES)[number]

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

export interface OrderConflict {
  source_kind: SourceKind | string
  /** {字段: [当前值, 来源新值]} */
  fields: Record<string, [unknown, unknown]>
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
  first_seen_at: Timestamp
  last_seen_at: Timestamp
  snapshot: Record<string, unknown>
}

/** 护栏单项结果：后端可能用数组或对象表示，前端经 utils/guard.normalizeGuardChecks 统一 */
export interface GuardCheck {
  name?: string
  key?: string
  check?: string
  code?: string | null
  ok?: boolean
  passed?: boolean
  pass?: boolean
  detail?: string | null
  reason?: string | null
  message?: string | null
  expected?: unknown
  actual?: unknown
}

export interface GuardVerdict {
  verdict: 'pass' | 'block'
  code: string | null
  reasons: string[]
  checks?: GuardCheck[] | Record<string, GuardCheck | boolean>
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
  sources: OrderSource[]
  attempts: Attempt[]
  events: EventRecord[]
  allowed_actions: OrderAction[]
}

export interface OrderQuery extends PageQuery {
  view?: OrderView
  q?: string
  account_id?: number
  source?: SourceKind
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
}

export interface SimulationResetResult {
  attempts?: number
  orders?: number
  accounts?: number
  [k: string]: unknown
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
  field_map: Record<string, string>
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

export interface FeishuConnectionInput {
  name?: string
  link?: string
  app_token?: string
  source_table_id?: string
  source_view_id?: string | null
  result_table_id?: string
  field_map?: Record<string, string>
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
  suggested_field_map: Record<string, string>
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
  field_map: Record<string, string>
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
  row_count: number
  headers: string[]
}

export interface ImportTemplateRef {
  id: number
  name: string
}

export interface ImportTemplate extends ImportTemplateRef {
  mapping: Record<string, string | null>
  created_at?: Timestamp
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
  created_at: Timestamp
  committed_at: Timestamp | null
  uploaded_by_name: string | null
}

export interface ImportRow {
  row_no: number
  verdict: ImportVerdict
  issues: string[]
  upstream_order_no: string | null
  normalized: Record<string, unknown> | null
  raw: Record<string, unknown>
}

export interface ImportPreview {
  batch: ImportBatch
  stats: Partial<BatchStats>
  rows: Page<ImportRow>
}
