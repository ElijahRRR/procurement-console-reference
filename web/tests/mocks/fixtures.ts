/**
 * 控制台接口的模拟数据（全部虚构：DEMO 名称、假地址、假单号），形状严格按开发契约 §10。
 * 供 tests/visual/screenshots.ts（Playwright 截图）与评审阶段复用；也被 vue-tsc 按 src/api/types.ts 做类型检查。
 *
 * 时间相对于传入的 now 生成，所以「在线」「x 分钟前」在任何时候运行都合理。
 * 只用类型导入（Node 直接运行 .ts 时会去掉），不引入 src 里的运行时代码。
 */
import type {
  Account,
  AllocationSummary,
  Attempt,
  Device,
  EventRecord,
  FeishuConnection,
  FeishuFieldsResult,
  FeishuPreview,
  ImportBatch,
  ImportPreview,
  ImportRow,
  ImportTemplate,
  Me,
  OrderAction,
  OrderDetail,
  OrderRow,
  OrderStatus,
  Overview,
  ResolvedLink,
  Settings,
  SyncRun,
  SystemFields,
  UploadResult,
  Writeback,
} from '../../src/api/types.ts'

export interface Fixtures {
  now: Date
  bjToday: string
  me: Me
  settings: Settings
  accounts: Account[]
  devices: Device[]
  orders: OrderRow[]
  details: Record<number, OrderDetail>
  lastAllocation: AllocationSummary
  overview: Overview
  connections: FeishuConnection[]
  syncRuns: Record<number, SyncRun[]>
  systemFields: SystemFields
  resolvedLink: ResolvedLink
  feishuFields: FeishuFieldsResult
  feishuPreview: FeishuPreview
  writebacks: Writeback[]
  batches: ImportBatch[]
  templates: ImportTemplate[]
  upload: UploadResult
  importRows: ImportRow[]
  importPreview: ImportPreview
}

/** 输入：基准时刻 → 输出：一整套互相引用一致的模拟数据 */
export function buildFixtures(now: Date = new Date()): Fixtures {
  const iso = (d: Date) => d.toISOString().replace(/\.\d{3}Z$/, '+00:00')
  const ago = (minutes: number) => iso(new Date(now.getTime() - minutes * 60_000))
  const bjDate = (offsetDays = 0) => {
    const d = new Date(now.getTime() + 8 * 3600_000 + offsetDays * 86400_000)
    return d.toISOString().slice(0, 10)
  }
  const bjToday = bjDate(0)
  const simNo = (id: number) => `SIM-${bjToday.replace(/-/g, '')}-${id}`

  const me: Me = {
    user: { id: 1, username: 'owner', display_name: 'DEMO 所有者' },
    workspace: { id: 1, name: 'DEMO 采购工作区' },
    role: 'owner',
  }

  const settings: Settings = {
    default_max_delivery_days: 10,
    simulation_only: true,
    logistics_sync_time: '08:30',
    real_purchase_available: false,
    file_admission: {
      approved_values: ['通过'],
      rejected_values: ['拒绝', '建议拒绝', '钓鱼'],
      purchaser_values: [],
      skip_purchase_status_values: ['已拍单', '拿回'],
      cancel_after_sale_values: ['已取消'],
      import_historical: true,
    },
  }

  // ── 设备与账号 ──────────────────────────────────────────────────────────────

  const devices: Device[] = [
    {
      id: 5,
      name: 'Mac-AdsPower-DEMO-01',
      browser: 'adspower',
      ext_version: '0.1.0',
      bound_account: { id: 3, name: '环境-DEMO-01' },
      online: true,
      last_seen_at: ago(0.5),
      identity: { signed_in: true, customer_id: 'A1DEMO00001', site: 'US', page_url: 'https://www.amazon.com/gp/css/homepage.html', at: ago(1) },
      identity_status: 'match',
      created_at: ago(60 * 24 * 6),
      revoked_at: null,
    },
    {
      id: 6,
      name: 'Mac-Chrome-DEMO-02',
      browser: 'chrome',
      ext_version: '0.1.0',
      bound_account: { id: 4, name: '环境-DEMO-02' },
      online: false,
      last_seen_at: ago(60 * 5),
      identity: { signed_in: true, customer_id: 'A1DEMO00002', site: 'US', page_url: 'https://www.amazon.com/', at: ago(60 * 5) },
      identity_status: 'match',
      created_at: ago(60 * 24 * 4),
      revoked_at: null,
    },
    {
      id: 7,
      name: 'Win-AdsPower-DEMO-03',
      browser: 'adspower',
      ext_version: '0.1.0',
      bound_account: { id: 7, name: '环境-DEMO-03' },
      online: true,
      last_seen_at: ago(1),
      identity: { signed_in: true, customer_id: 'A9OTHER0099', site: 'US', page_url: 'https://www.amazon.com/', at: ago(2) },
      identity_status: 'mismatch',
      created_at: ago(60 * 24 * 2),
      revoked_at: null,
    },
    {
      id: 8,
      name: 'Mac-Chrome-DEMO-旧环境',
      browser: 'chrome',
      ext_version: '0.0.9',
      bound_account: null,
      online: false,
      last_seen_at: ago(60 * 24 * 9),
      identity: null,
      identity_status: 'unknown',
      created_at: ago(60 * 24 * 20),
      revoked_at: ago(60 * 24 * 8),
    },
    {
      id: 9,
      name: 'Mac-Chrome-DEMO-新装',
      browser: 'chrome',
      ext_version: '0.1.0',
      bound_account: null,
      online: true,
      last_seen_at: ago(0.2),
      identity: { signed_in: false, customer_id: null, site: 'US', page_url: 'https://www.amazon.com/ap/signin', at: ago(0.3) },
      identity_status: 'signed_out',
      created_at: ago(12),
      revoked_at: null,
    },
  ]

  const accounts: Account[] = [
    {
      id: 3,
      name: '环境-DEMO-01',
      amazon_customer_id: 'A1DEMO00001',
      site: 'US',
      min_amount: '20.00',
      max_amount: '30.00',
      daily_limit: 20,
      priority: 5,
      max_delivery_days: null,
      effective_max_delivery_days: 10,
      purchase_enabled: true,
      sync_enabled: true,
      execution_paused: false,
      pause_reason: null,
      paused_at: null,
      pause_cause: null,
      open_result_unknown: 0,
      purchase_mode: 'scheduled',
      schedule_times: ['14:00', '16:30'],
      default_device_id: 5,
      expected_card_last4: '1234',
      notes: null,
      quota_today: { limit: 20, used: 3, planned: 5, available: 12 },
      devices: [{ id: 5, name: 'Mac-AdsPower-DEMO-01', online: true, identity_status: 'match', last_seen_at: ago(0.5) }],
      active_run: { id: 9, device_id: 5, device_name: 'Mac-AdsPower-DEMO-01', started_at: ago(6) },
      created_at: ago(60 * 24 * 10),
      updated_at: ago(60 * 3),
    },
    {
      id: 4,
      name: '环境-DEMO-02',
      amazon_customer_id: 'A1DEMO00002',
      site: 'US',
      min_amount: '20.00',
      max_amount: '50.00',
      daily_limit: 15,
      priority: 2,
      max_delivery_days: 7,
      effective_max_delivery_days: 7,
      purchase_enabled: true,
      sync_enabled: true,
      execution_paused: false,
      pause_reason: null,
      paused_at: null,
      pause_cause: null,
      open_result_unknown: 0,
      purchase_mode: 'manual',
      schedule_times: [],
      default_device_id: null,
      expected_card_last4: null,
      notes: '只接 FBA 商品',
      quota_today: { limit: 15, used: 15, planned: 0, available: 0 },
      devices: [{ id: 6, name: 'Mac-Chrome-DEMO-02', online: false, identity_status: 'match', last_seen_at: ago(60 * 5) }],
      active_run: null,
      created_at: ago(60 * 24 * 9),
      updated_at: ago(60 * 20),
    },
    {
      id: 7,
      name: '环境-DEMO-03',
      amazon_customer_id: 'A1DEMO00003',
      site: 'US',
      min_amount: '30.00',
      max_amount: null,
      daily_limit: 10,
      priority: 2,
      max_delivery_days: null,
      effective_max_delivery_days: 10,
      purchase_enabled: true,
      sync_enabled: false,
      execution_paused: true,
      pause_reason: '结果待核实：PO-DEMO-RESULT-UNKNOWN',
      paused_at: ago(95),
      // 执行模块因结果待核实暂停（services/accounts.pause_cause）；下面那笔结果待核实的任务在这个账号上
      pause_cause: 'result_unknown',
      open_result_unknown: 1,
      purchase_mode: 'scheduled',
      schedule_times: ['15:00'],
      default_device_id: 7,
      expected_card_last4: null,
      notes: null,
      quota_today: { limit: 10, used: 1, planned: 2, available: 7 },
      devices: [{ id: 7, name: 'Win-AdsPower-DEMO-03', online: true, identity_status: 'mismatch', last_seen_at: ago(1) }],
      active_run: null,
      created_at: ago(60 * 24 * 8),
      updated_at: ago(95),
    },
    {
      id: 8,
      name: '环境-DEMO-04',
      amazon_customer_id: null,
      site: 'US',
      min_amount: '0.00',
      max_amount: '20.00',
      daily_limit: 20,
      priority: 1,
      max_delivery_days: 14,
      effective_max_delivery_days: 14,
      purchase_enabled: false,
      sync_enabled: true,
      execution_paused: false,
      pause_reason: null,
      paused_at: null,
      pause_cause: null,
      open_result_unknown: 0,
      purchase_mode: 'manual',
      schedule_times: [],
      default_device_id: null,
      expected_card_last4: null,
      notes: '新账号，暂不接单',
      quota_today: { limit: 20, used: 0, planned: 0, available: 20 },
      devices: [],
      active_run: null,
      created_at: ago(60 * 24),
      updated_at: ago(60 * 24),
    },
  ]

  // ── 任务 ────────────────────────────────────────────────────────────────────

  const acc = (id: number) => {
    const a = accounts.find((x) => x.id === id)!
    return { id: a.id, name: a.name }
  }

  let seq = 0
  const recipients = [
    ['Demo Buyer', 'FL', '32960'],
    ['Alex Sample', 'TX', '75001'],
    ['Jamie Example', 'CA', '94016'],
    ['Riley Placeholder', 'NY', '10001'],
    ['Casey Fixture', 'WA', '98101'],
    ['Morgan Mock', 'IL', '60601'],
  ] as const

  interface RowSpec {
    status: OrderStatus
    unit: string | null
    qty: number | null
    cap: string | null
    date: number | null
    account?: number
    kind?: 'auto' | 'manual'
    note?: string | null
    reason?: string | null
    missing?: string[]
    review?: 'approved' | 'rejected' | 'unknown'
    sources?: ('feishu' | 'file')[]
    attention?: string | null
    attempt?: OrderRow['latest_attempt']
    override?: { asin?: string; quantity?: number; cap?: string }
  }

  function row(spec: RowSpec): OrderRow {
    seq += 1
    const id = 100 + seq
    const [name, state, zip] = recipients[seq % recipients.length]
    const asin = `B0DEMO${String(seq).padStart(4, '0')}`
    const qtyEff = spec.override?.quantity ?? spec.qty
    const unit = spec.unit
    const subtotal = unit !== null && qtyEff !== null ? (Number(unit) * qtyEff).toFixed(2) : null
    const sources = spec.sources ?? ['feishu']
    return {
      id,
      upstream_order_no: `PO-DEMO-${String(20261100 + seq)}-SKU${String(seq).padStart(2, '0')}`,
      store_name: seq % 2 ? 'STORE-DEMO-A' : 'STORE-DEMO-B',
      sku: `SKU-DEMO-${String(seq).padStart(3, '0')}`,
      asin: spec.missing?.includes('asin') ? null : asin,
      asin_eff: spec.override?.asin ?? (spec.missing?.includes('asin') ? null : asin),
      quantity: spec.qty,
      quantity_eff: qtyEff,
      unit_price: unit,
      subtotal_eff: subtotal,
      price_cap: spec.cap,
      price_cap_eff: spec.override?.cap ?? spec.cap,
      has_override: !!spec.override,
      suggested_purchase_date: spec.date === null ? null : bjDate(spec.date),
      order_time: ago(60 * 24 * (3 - (spec.date ?? 0)) + seq * 7),
      recipient_name: name,
      state,
      postal_code: zip,
      status: spec.status,
      status_zh: null,
      status_reason: spec.reason ?? null,
      missing_fields: spec.missing ?? [],
      review_status: spec.review ?? 'approved',
      needs_attention: !!spec.attention,
      attention_note: spec.attention ?? null,
      assigned_account: spec.account ? acc(spec.account) : null,
      assignment_kind: spec.account ? (spec.kind ?? 'auto') : null,
      assignment_note: spec.note ?? null,
      primary_source_kind: sources[0],
      source_kinds: sources,
      latest_attempt: spec.attempt ?? null,
      updated_at: ago(seq * 11),
    }
  }

  const orders: OrderRow[] = [
    row({ status: 'assigned', unit: '24.99', qty: 1, cap: '31.80', date: 0, account: 3, note: '保底：当天首单' }),
    row({ status: 'assigned', unit: '41.97', qty: 1, cap: '52.69', date: -1, account: 4, note: '按优先级填充' }),
    row({ status: 'assigned', unit: '12.50', qty: 2, cap: '32.40', date: 0, account: 3, kind: 'manual', note: '人工改派', sources: ['feishu', 'file'] }),
    row({ status: 'assigned', unit: '35.00', qty: 1, cap: '44.10', date: 2, account: 7, note: '保底：当天首单' }),
    row({ status: 'assigned', unit: '28.40', qty: 1, cap: '36.00', date: 1, account: 3, note: '按优先级填充' }),
    row({ status: 'queued', unit: '64.00', qty: 1, cap: '79.90', date: 0, reason: '金额 $64.00 不在任何启用账号的区间内' }),
    row({ status: 'queued', unit: '45.20', qty: 1, cap: '57.00', date: 0, reason: '匹配账号当天额度已满' }),
    row({ status: 'pending_review', unit: '22.10', qty: 1, cap: '29.00', date: 0, review: 'unknown', sources: ['file'], reason: '缺少人审结果' }),
    row({ status: 'pending_review', unit: '33.33', qty: 1, cap: '41.00', date: 1, review: 'unknown', sources: ['file'] }),
    row({ status: 'needs_info', unit: null, qty: 1, cap: '38.00', date: null, missing: ['unit_price', 'suggested_purchase_date', 'phone'], sources: ['file'] }),
    row({ status: 'needs_info', unit: '19.99', qty: null, cap: null, date: 0, missing: ['quantity', 'price_cap'] }),
    row({ status: 'conflict', unit: '27.50', qty: 1, cap: '35.00', date: 0, sources: ['feishu', 'file'], reason: '文件来源的数量、限价与飞书记录不同' }),
    row({ status: 'conflict', unit: '33.10', qty: 1, cap: '42.00', date: 0, reason: '来源的 AMZ 订单号「114-DEMO」格式不对，需人工核实（不当作已采购）' }),
    row({
      status: 'executing',
      unit: '26.80',
      qty: 1,
      cap: '34.00',
      date: 0,
      account: 3,
      note: '保底：当天首单',
      attempt: { id: 31, status: 'checking', amazon_order_no: null, total: null, simulated: true, error_code: null, finished_at: null },
    }),
    row({
      status: 'purchased',
      unit: '21.49',
      qty: 1,
      cap: '28.00',
      date: -1,
      account: 3,
      note: '保底：当天首单',
      attempt: { id: 21, status: 'succeeded', amazon_order_no: simNo(21), total: '23.78', simulated: true, error_code: null, finished_at: ago(60 * 20) },
    }),
    row({
      status: 'purchased',
      unit: '39.00',
      qty: 1,
      cap: '49.90',
      date: -2,
      account: 4,
      note: '按优先级填充',
      attempt: { id: 18, status: 'succeeded', amazon_order_no: simNo(18), total: '42.12', simulated: true, error_code: null, finished_at: ago(60 * 26) },
    }),
    row({
      status: 'result_unknown',
      unit: '47.50',
      qty: 1,
      cap: '60.00',
      date: 0,
      account: 7,
      reason: '已登记提交，插件失联后未回报结果；插件迟到上报的结果：成功，请据此核实',
      attention: '插件迟到上报的结果：成功，请据此核实',
      // 清扫判为待核实后插件才送到成功回执：单号与金额补进了尝试（services/execution._late_receipt）
      attempt: { id: 27, status: 'unknown', amazon_order_no: simNo(27), total: '50.37', simulated: true, error_code: null, finished_at: ago(95) },
    }),
    row({
      status: 'exception',
      unit: '48.00',
      qty: 1,
      cap: '49.00',
      date: 0,
      account: 4,
      reason: '结算总额超过限价',
      sources: ['feishu', 'file'],
      override: { cap: '50.00' },
      attempt: { id: 25, status: 'failed', amazon_order_no: null, total: '52.00', simulated: true, error_code: 'PRICE_OVER_CAP', finished_at: ago(40) },
    }),
    row({
      status: 'exception',
      unit: '23.75',
      qty: 2,
      cap: '58.00',
      date: -1,
      account: 3,
      reason: '商品无库存',
      attempt: { id: 22, status: 'failed', amazon_order_no: null, total: null, simulated: true, error_code: 'OUT_OF_STOCK', finished_at: ago(60 * 2) },
    }),
    row({
      status: 'repurchase_pending',
      unit: '29.90',
      qty: 1,
      cap: '38.00',
      date: -3,
      account: 3,
      reason: '亚马逊取消了采购订单',
      attempt: { id: 12, status: 'succeeded', amazon_order_no: simNo(12), total: '32.85', simulated: true, error_code: null, finished_at: ago(60 * 50) },
    }),
    row({
      status: 'purchased',
      unit: '18.20',
      qty: 1,
      cap: '25.00',
      date: -2,
      account: 3,
      attention: '上游在采购开始后取消，请处理对应的亚马逊订单',
      attempt: { id: 15, status: 'succeeded', amazon_order_no: simNo(15), total: '19.95', simulated: true, error_code: null, finished_at: ago(60 * 30) },
    }),
    row({
      status: 'cancelled',
      unit: '31.00',
      qty: 1,
      cap: '39.00',
      date: 0,
      reason: '上游取消（售后状态：已退款）',
      // 执行中上游取消：进度上报得到 continue=false、cancel_code=UPSTREAM_CANCELLED，插件未提交前中止
      attempt: { id: 33, status: 'aborted', amazon_order_no: null, total: null, simulated: true, error_code: 'ABORTED_UPSTREAM_CANCELLED', finished_at: ago(70) },
    }),
    row({ status: 'closed', unit: '26.00', qty: 1, cap: '33.00', date: -4, reason: '人工决定不重采' }),
  ]

  // 执行模块暂停账号时写「结果待核实：<平台订单号>」
  const unknownOrder = orders.find((o) => o.status === 'result_unknown')
  const pausedByUnknown = accounts.find((a) => a.pause_cause === 'result_unknown')
  if (unknownOrder && pausedByUnknown) pausedByUnknown.pause_reason = `结果待核实：${unknownOrder.upstream_order_no}`

  // ── 详情 ────────────────────────────────────────────────────────────────────

  const ACTIONS: Record<OrderStatus, OrderAction[]> = {
    pending_review: ['review', 'override', 'cancel'],
    needs_info: ['override', 'cancel'],
    conflict: ['resolve_conflict', 'cancel'],
    queued: ['assign', 'override', 'cancel'],
    assigned: ['assign', 'unassign', 'override', 'cancel'],
    executing: [],
    result_unknown: ['resolve_unknown'],
    exception: ['resume', 'assign', 'override', 'cancel'],
    purchased: ['mark_amazon_cancelled'],
    cancelled: ['uncancel'],
    repurchase_pending: ['repurchase'],
    closed: [],
  }

  function attemptsFor(r: OrderRow): Attempt[] {
    const la = r.latest_attempt
    if (!la) return []
    const base: Attempt = {
      id: la.id,
      attempt_no: 1,
      kind: 'auto',
      account_id: r.assigned_account?.id ?? null,
      account_name: r.assigned_account?.name ?? null,
      device_id: 5,
      device_name: 'Mac-AdsPower-DEMO-01',
      run_id: 9,
      simulated: la.simulated,
      status: la.status,
      bj_date: bjToday,
      claimed_at: ago(60),
      submitted_at: la.status === 'succeeded' || la.status === 'unknown' ? ago(55) : null,
      stage: la.status === 'checking' ? '结算页核对' : la.status === 'aborted' ? '结算页核对' : null,
      finished_at: la.finished_at,
      guard_verdict: null,
      amazon_order_no: la.amazon_order_no,
      subtotal: r.subtotal_eff,
      shipping: la.total ? '0.00' : null,
      tax: la.total && r.subtotal_eff ? (Number(la.total) - Number(r.subtotal_eff)).toFixed(2) : null,
      total_before_tax: la.total ? r.subtotal_eff : null,
      total: la.total,
      card_last4: la.total ? '1234' : null,
      delivery_text: la.total ? 'Arriving Nov 9' : null,
      delivery_date: la.total ? bjDate(4) : null,
      delivery_days: la.total ? 4 : null,
      error_code: la.error_code,
      error_detail: la.error_code === 'OUT_OF_STOCK' ? '商品页显示 Currently unavailable' : null,
      resolution: null,
    }
    if (la.status === 'succeeded') {
      // 形状同 services/guard.evaluate：checks 以检查名为键，ok 为 true / false / null
      base.guard_verdict = {
        verdict: 'pass',
        code: null,
        reasons: [],
        checks: {
          item: { ok: true, expected: { asin: r.asin_eff, quantity: r.quantity_eff }, actual: { asin: r.asin_eff, quantity: r.quantity_eff } },
          zip: { ok: true, expected: r.postal_code, actual: r.postal_code },
          subtotal: { ok: true, expected: { min: '20.00', max: '50.00' }, actual: r.subtotal_eff },
          price_cap: { ok: true, expected: r.price_cap_eff, actual: la.total },
          delivery: { ok: true, expected: 10, actual: 4 },
          data: { ok: true, expected: 'USD；小计、总额可解析；配送天数可得', actual: [] },
          card: { ok: true, expected: '1234', actual: '1234' },
        },
        delivery_days: 4,
        total_for_cap: la.total,
        fulfillment: 'amazon',
      }
    }
    if (la.error_code === 'PRICE_OVER_CAP') {
      base.guard_input = { subtotal: '48.00', shipping: '0.00', tax: '4.00', total: '52.00', currency: 'USD' }
      base.guard_verdict = {
        verdict: 'block',
        code: 'PRICE_OVER_CAP',
        reasons: ['结算总额 $52.00 超过限价 $50.00'],
        checks: {
          item: { ok: true, expected: { asin: r.asin_eff, quantity: 1 }, actual: { asin: r.asin_eff, quantity: 1 } },
          zip: { ok: true, expected: r.postal_code, actual: r.postal_code },
          subtotal: { ok: true, expected: { min: '20.00', max: '50.00' }, actual: '48.00' },
          price_cap: { ok: false, expected: '50.00', actual: '52.00' },
          delivery: { ok: true, expected: 7, actual: 5 },
          data: { ok: true, expected: 'USD；小计、总额可解析；配送天数可得', actual: [] },
          card: { ok: true, expected: null, actual: '1234' },
        },
        delivery_days: 5,
        total_for_cap: '52.00',
        fulfillment: 'amazon',
      }
      base.shipping = '0.00'
      base.tax = '4.00'
      base.total_before_tax = '48.00'
      base.delivery_text = 'Arriving Nov 10'
      base.delivery_date = bjDate(5)
      base.delivery_days = 5
      base.account_name = '环境-DEMO-02'
      base.device_name = 'Mac-Chrome-DEMO-02'
      base.device_id = 6
      base.attempt_no = 2
      base.kind = 'retry'
      return [
        base,
        {
          ...base,
          id: la.id - 3,
          attempt_no: 1,
          kind: 'auto',
          status: 'failed',
          claimed_at: ago(180),
          finished_at: ago(178),
          error_code: 'PAGE_LOAD_FAILED',
          error_detail: '结算页 30 秒未加载完成，自动重试一次',
          guard_verdict: null,
          guard_input: null,
          subtotal: null,
          shipping: null,
          tax: null,
          total_before_tax: null,
          total: null,
          card_last4: null,
          delivery_text: null,
          delivery_date: null,
          delivery_days: null,
        },
      ]
    }
    if (la.status === 'unknown') {
      base.error_code = null
      base.error_detail = '登记提交后 3 分钟未收到结果，执行权超时'
    }
    if (la.status === 'aborted') base.error_detail = '上游已取消（售后状态：已退款），未提交前中止'
    return [base]
  }

  function eventsFor(r: OrderRow): EventRecord[] {
    const ev = (
      id: number,
      minutes: number,
      kind: string,
      actor: EventRecord['actor_kind'],
      name: string | null,
      detail: Record<string, unknown>,
      attemptId?: number,
    ): EventRecord => ({
      id,
      entity: attemptId ? 'attempt' : 'order',
      entity_id: attemptId ?? r.id,
      order_id: r.id,
      at: ago(minutes),
      actor_kind: actor,
      actor_id: actor === 'user' ? 1 : null,
      actor_name: name,
      kind,
      detail,
    })
    // kind 与服务端 events.record 的取值一致（order.* / attempt.*）；操作者名由服务端按用户或设备填 actor_name
    const list: EventRecord[] = [ev(r.id * 10 + 1, 60 * 30, 'order.created', 'source', null, { from: null, to: 'queued' })]
    if (r.assigned_account) list.push(ev(r.id * 10 + 2, 60 * 29, 'order.assigned', 'system', null, { account_name: r.assigned_account.name, reason: r.assignment_note, kind: 'auto' }))
    if (r.has_override) list.push(ev(r.id * 10 + 3, 60 * 4, 'order.override_set', 'user', 'DEMO 所有者', { note: '上游限价与结算口径不一致，按 $50.00 执行' }))
    const la = r.latest_attempt
    if (la) list.push(ev(r.id * 10 + 4, 60, 'attempt.claimed', 'device', 'Mac-AdsPower-DEMO-01', { run_id: 9, attempt_no: 1, kind: 'auto', simulated: la.simulated }, la.id))
    if (la?.status === 'aborted') {
      list.push(ev(r.id * 10 + 8, 71, 'attempt.progress', 'device', 'Mac-AdsPower-DEMO-01', {
        stage: '结算页核对', detail: null, continue: false, cancel_reason: '上游已取消（售后状态：已退款）', cancel_code: 'UPSTREAM_CANCELLED',
      }, la.id))
      list.push(ev(r.id * 10 + 9, 70, 'attempt.aborted', 'device', 'Mac-AdsPower-DEMO-01', { error_code: la.error_code, reason: '上游已取消（售后状态：已退款），未提交前中止' }, la.id))
    }
    if (la?.status === 'unknown' && la.amazon_order_no) {
      list.push(ev(r.id * 10 + 8, 50, 'attempt.late_result', 'device', 'Win-AdsPower-DEMO-03', {
        outcome: 'succeeded', amazon_order_no: la.amazon_order_no, error_code: null, error_detail: null,
        amounts: { subtotal: r.subtotal_eff, shipping: '0.00', tax: la.total && r.subtotal_eff ? (Number(la.total) - Number(r.subtotal_eff)).toFixed(2) : null, total_before_tax: r.subtotal_eff, total: la.total },
        reason: `插件迟到上报的结果：成功，亚马逊订单号 ${la.amazon_order_no}，请据此核实`,
      }, la.id))
    }
    if (r.status === 'exception') list.push(ev(r.id * 10 + 5, 40, 'order.status_changed', 'device', 'Mac-AdsPower-DEMO-01', { from: 'executing', to: 'exception', reason: r.status_reason }))
    if (r.status === 'conflict') list.push(ev(r.id * 10 + 6, 30, 'order.source_conflict', 'source', null, { reason: r.status_reason }))
    // 新的在前（与服务端一致）：按时间排序后再倒过来
    list.sort((a, b) => a.at.localeCompare(b.at))
    if (r.needs_attention) list.push(ev(r.id * 10 + 7, 20, 'order.upstream_changed_after_start', 'source', null, { note: r.attention_note }))
    return list.reverse()
  }

  function detailFor(r: OrderRow): OrderDetail {
    const sources: OrderDetail['sources'] = r.source_kinds.map((k, i) =>
      k === 'feishu'
        ? {
            id: r.id * 10 + i,
            kind: 'feishu',
            connection_id: 1,
            connection_name: '订单中心 · 采购订单（DEMO）',
            record_id: `recDEMO${String(r.id).padStart(6, '0')}`,
            batch_id: null,
            row_no: null,
            filename: null,
            // 文件为主的任务不自动写回飞书，需人工把飞书记录设为回填目标（v1 §6.2）
            is_writeback_target: r.primary_source_kind === 'feishu',
            first_seen_at: ago(60 * 30),
            last_seen_at: ago(12),
            snapshot: {
              平台订单号: r.upstream_order_no,
              ASIN: r.asin,
              数量: r.quantity,
              采购单价: r.unit_price,
              限价: r.price_cap,
              人工审核: '通过',
              采购方: '自采购1',
              建议采购日期: r.suggested_purchase_date,
              收货人姓名: r.recipient_name,
              邮编: r.postal_code,
            },
          }
        : {
            id: r.id * 10 + i,
            kind: 'file',
            connection_id: null,
            connection_name: null,
            record_id: null,
            batch_id: 41,
            row_no: 3 + (r.id % 17),
            filename: 'orders-demo-1105.xlsx',
            is_writeback_target: false,
            first_seen_at: ago(60 * 5),
            last_seen_at: ago(60 * 5),
            snapshot: {
              平台订单号: r.upstream_order_no,
              ASIN: r.asin,
              数量: r.status === 'conflict' ? 2 : r.quantity,
              限价: r.status === 'conflict' ? '30.00' : r.price_cap,
              收货人姓名: r.recipient_name,
            },
          },
    )
    return {
      ...r,
      original: {
        asin: r.asin,
        quantity: r.quantity,
        price_cap: r.price_cap,
        unit_price: r.unit_price,
        sales_total: r.unit_price ? (Number(r.unit_price) * 1.6).toFixed(2) : null,
        ship_days: 4,
        order_time: r.order_time,
        order_date_bj: r.order_time ? bjDate(-3) : null,
        suggested_purchase_date: r.suggested_purchase_date,
        suggested_date_basis: r.suggested_purchase_date ? (r.id % 2 ? 'source' : 'computed') : null,
        review_value: r.review_status === 'approved' ? '通过' : null,
        store_name: r.store_name,
        sku: r.sku,
      },
      override: r.has_override
        ? { asin: null, quantity: null, price_cap: r.price_cap_eff, note: '上游限价与结算口径不一致，按 $50.00 执行', by_name: 'DEMO 所有者', at: ago(60 * 4) }
        : null,
      address: {
        recipient_name: r.recipient_name,
        address1: `${100 + r.id} Demo Street`,
        address2: r.id % 3 ? null : 'Apt 4B',
        city: 'Sampletown',
        state: r.state,
        postal_code: r.postal_code,
        phone: r.missing_fields.includes('phone') ? null : '555-0100',
        country: 'US',
        district: null,
      },
      // 形状同 services/orders 的冲突：{kind, source_kind, reason, fields, incoming, source, at}
      conflict:
        r.status !== 'conflict'
          ? null
          : (r.status_reason ?? '').includes('AMZ')
            ? {
                kind: 'external_order_no',
                source_kind: 'feishu',
                reason: r.status_reason,
                fields: { external_amz_order_no: [null, '114-DEMO'] },
                incoming: { external_amz_order_no: '114-DEMO', external_buyer_account: '环境-DEMO-02' },
                source: { kind: 'feishu', connection_id: 1, record_id: `recDEMO${String(r.id).padStart(6, '0')}` },
                at: ago(30),
              }
            : {
                kind: 'fields',
                source_kind: 'file',
                reason: r.status_reason,
                fields: { quantity: [1, 2], price_cap: ['35.00', '30.00'] },
                incoming: { quantity: 2, price_cap: '30.00' },
                source: { kind: 'file', batch_id: 41, row_no: 3 + (r.id % 17) },
                at: ago(30),
              },
      sources,
      attempts: attemptsFor(r),
      events: eventsFor(r),
      allowed_actions: [
        ...ACTIONS[r.status],
        ...(r.needs_attention ? (['clear_attention'] as OrderAction[]) : []),
        ...(r.primary_source_kind !== 'feishu' && r.source_kinds.includes('feishu') ? (['set_writeback_target'] as OrderAction[]) : []),
      ],
    }
  }

  const details: Record<number, OrderDetail> = Object.fromEntries(orders.map((r) => [r.id, detailFor(r)]))

  // ── 分配与概览 ──────────────────────────────────────────────────────────────

  const queued = orders.filter((o) => o.status === 'queued')
  const lastAllocation: AllocationSummary = {
    id: 57,
    bj_date: bjToday,
    at: ago(18),
    trigger: 'intake',
    stats: { considered: 9, assigned: 7, coverage: 3, fill: 4, unassigned: queued.length },
    uncovered: [
      { account_id: 8, name: '环境-DEMO-04', date: bjToday, reason: '没有金额匹配的订单' },
      { account_id: 4, name: '环境-DEMO-02', date: bjToday, reason: '当天额度已用完' },
    ],
    unassigned: queued.map((o) => ({ order_id: o.id, upstream_order_no: o.upstream_order_no, reason: o.status_reason ?? '' })),
  }

  const counts: Partial<Record<OrderStatus, number>> = {}
  for (const o of orders) counts[o.status] = (counts[o.status] ?? 0) + 1
  const assigned = orders.filter((o) => o.status === 'assigned')
  const overview: Overview = {
    bj_date: bjToday,
    counts,
    today_executable: assigned.filter((o) => (o.suggested_purchase_date ?? '9999') <= bjToday).length,
    future_assigned: assigned.filter((o) => (o.suggested_purchase_date ?? '') > bjToday).length,
    accounts: { total: accounts.length, enabled: accounts.filter((a) => a.purchase_enabled).length, paused: accounts.filter((a) => a.execution_paused).length },
    devices: { total: devices.filter((d) => !d.revoked_at).length, online: devices.filter((d) => !d.revoked_at && d.online).length },
    needs_attention: orders.filter((o) => o.needs_attention).length,
    // 同服务端 overview：最近变为「异常」的任务（结果待核实单独提示）
    recent_exceptions: orders.filter((o) => o.status === 'exception'),
    last_allocation: lastAllocation,
    simulation_only: true,
    feishu_configured: true,
  }

  // ── 飞书 ────────────────────────────────────────────────────────────────────

  const connections: FeishuConnection[] = [
    {
      id: 1,
      name: '订单中心 · 采购订单（DEMO）',
      link: 'https://demo-tenant.feishu.cn/base/bascnDEMO0000000000000001?table=tblDEMOsource01&view=vewDEMO0001',
      app_token: 'bascnDEMO0000000000000001',
      source_table_id: 'tblDEMOsource01',
      source_view_id: 'vewDEMO0001',
      result_table_id: 'tblDEMOsource01',
      // 多个候选列按顺序回退（services/feishu_fields.DEFAULT_FIELD_MAP）
      field_map: { upstream_order_no: '平台订单号', asin: ['ASIN', '来源码'], quantity: ['数量', '数量1'], price_cap: '限价', review_value: '人工审核' },
      admission: {
        approved_values: ['通过'],
        rejected_values: ['拒绝', '建议拒绝', '钓鱼'],
        purchaser_values: ['自采购1'],
        skip_purchase_status_values: ['已拍单', '拿回'],
        cancel_after_sale_values: ['已退款'],
        import_historical: false,
      },
      writeback_map: { buyer_account: '买家号', amazon_order_no: 'AMZ订单号', purchase_status: '采购状态', total: '税前总额USD' },
      writeback_enabled: true,
      writeback_verified_at: null,
      writeback_state: 'unverified',
      writeback_test_record: null,
      poll_minutes: 10,
      enabled: true,
      last_sync_at: ago(4),
      last_sync_ok_at: ago(4),
      last_error: null,
      last_stats: { read: 128, new: 6, update: 3, unchanged: 112, conflict: 1, historical: 4, skipped: 2, invalid: 0 },
      created_at: ago(60 * 24 * 7),
      updated_at: ago(60 * 24),
    },
    {
      id: 2,
      name: '备用订单表（DEMO）',
      link: 'https://demo-tenant.feishu.cn/base/bascnDEMO0000000000000002?table=tblDEMOsource02',
      app_token: 'bascnDEMO0000000000000002',
      source_table_id: 'tblDEMOsource02',
      source_view_id: null,
      result_table_id: 'tblDEMOresult02',
      field_map: { upstream_order_no: '平台订单号' },
      admission: { approved_values: ['通过'] },
      writeback_map: {},
      writeback_enabled: false,
      writeback_verified_at: null,
      writeback_state: 'disabled',
      writeback_test_record: null,
      poll_minutes: 30,
      enabled: true,
      last_sync_at: ago(25),
      last_sync_ok_at: ago(60 * 26),
      last_error: '飞书接口返回 91403：应用没有该数据表的读取权限，请在多维表格里重新添加文档应用',
      last_stats: null,
      created_at: ago(60 * 24 * 3),
      updated_at: ago(25),
    },
  ]

  const syncRuns: Record<number, SyncRun[]> = {
    1: [0, 10, 20, 30].map((m, i) => ({
      id: 300 - i,
      connection_id: 1,
      trigger: i === 1 ? 'manual' : 'schedule',
      started_at: ago(4 + m),
      finished_at: ago(4 + m - 0.2),
      ok: true,
      stats: { read: 128, new: i === 0 ? 6 : 0, update: i === 0 ? 3 : 1, unchanged: 118, conflict: i === 0 ? 1 : 0 },
      error: null,
    })),
    2: [
      { id: 210, connection_id: 2, trigger: 'schedule', started_at: ago(25), finished_at: ago(24.9), ok: false, stats: null, error: '飞书接口返回 91403：应用没有该数据表的读取权限' },
    ],
  }

  const systemFields: SystemFields = {
    source_fields: [
      { key: 'upstream_order_no', label: '平台订单号', required: true },
      { key: 'order_line_id', label: '订单行 ID' },
      { key: 'store_name', label: '店铺' },
      { key: 'sales_po_no', label: '采购订单号' },
      { key: 'sku', label: 'SKU' },
      { key: 'asin', label: 'ASIN' },
      { key: 'quantity', label: '数量' },
      { key: 'unit_price', label: '采购单价' },
      { key: 'sales_total', label: '销售总额' },
      { key: 'price_cap', label: '限价' },
      { key: 'order_time', label: '下单时间' },
      { key: 'ship_days', label: '配送天数' },
      { key: 'suggested_purchase_date', label: '建议采购日期' },
      { key: 'recipient_name', label: '收货人姓名' },
      { key: 'address1', label: '收货人地址' },
      { key: 'address2', label: '地址2' },
      { key: 'city', label: '收货人城市' },
      { key: 'state', label: '收货人州/省' },
      { key: 'postal_code', label: '邮编' },
      { key: 'phone', label: '收货人电话' },
      { key: 'country', label: '收货人国家' },
      { key: 'review_value', label: '人工审核' },
      { key: 'purchaser', label: '采购方' },
      { key: 'purchase_status_value', label: '采购状态' },
      { key: 'after_sale_value', label: '售后状态' },
      { key: 'external_amz_order_no', label: 'AMZ订单号' },
      { key: 'external_buyer_account', label: '买家号' },
    ],
    result_fields: [
      { key: 'buyer_account', label: '买家号' },
      { key: 'buyer_customer_id', label: '买家号 ID' },
      { key: 'amazon_order_no', label: 'AMZ 订单号' },
      { key: 'purchase_status', label: '采购状态' },
      { key: 'total_before_tax', label: '税前总额' },
      { key: 'shipping', label: '运费' },
      { key: 'tax', label: '税费' },
      { key: 'total', label: '总额' },
      { key: 'card_last4', label: '卡尾号' },
    ],
  }

  const resolvedLink: ResolvedLink = {
    app_token: 'bascnDEMO0000000000000003',
    table_id: 'tblDEMOsource03',
    view_id: 'vewDEMO0003',
    tables: [
      { table_id: 'tblDEMOsource03', name: '采购订单（DEMO）' },
      { table_id: 'tblDEMOresult03', name: '采购结果（DEMO）' },
    ],
  }

  const fieldNames: [string, number, string, boolean][] = [
    ['平台订单号', 1, 'Text', true],
    ['ASIN', 1, 'Text', true],
    ['数量', 2, 'Number', true],
    ['采购单价', 2, 'Number', true],
    ['限价', 2, 'Number', true],
    ['下单时间', 5, 'DateTime', true],
    ['建议采购日期', 20, 'Formula', false],
    ['人工审核', 3, 'SingleSelect', true],
    ['采购方', 3, 'SingleSelect', true],
    ['采购状态', 3, 'SingleSelect', true],
    ['收货人姓名', 1, 'Text', true],
    ['邮编', 1, 'Text', true],
    ['买家号', 1, 'Text', true],
    ['AMZ订单号', 1, 'Text', true],
    ['税前总额USD', 2, 'Number', true],
    ['店铺名称', 19, 'Lookup', false],
  ]
  const feishuFields: FeishuFieldsResult = {
    fields: fieldNames.map(([n, type, ui, writable], i) => ({ field_id: `fldDEMO${String(i).padStart(4, '0')}`, field_name: n, type, ui_type: ui, is_primary: i === 0, writable })),
    suggested_field_map: { upstream_order_no: '平台订单号', asin: ['ASIN', '来源码'], quantity: ['数量', '数量1'], unit_price: '采购单价', price_cap: '限价', order_time: '下单时间', suggested_purchase_date: '建议采购日期', review_value: '人工审核', purchaser: '采购方', purchase_status_value: '采购状态', recipient_name: '收货人姓名', postal_code: '邮编' },
    suggested_writeback_map: { buyer_account: '买家号', amazon_order_no: 'AMZ订单号', purchase_status: '采购状态', total_before_tax: '税前总额USD' },
  }

  const feishuPreview: FeishuPreview = {
    stats: { read: 20, new: 12, update: 2, unchanged: 3, historical: 2, skipped: 1, conflict: 0, invalid: 0 },
    records: orders.slice(0, 8).map((o, i) => ({
      record_id: `recDEMO${String(9000 + i)}`,
      verdict: (['new', 'new', 'update', 'unchanged', 'historical', 'skipped', 'new', 'new'] as const)[i],
      reason: i === 4 ? '已有 AMZ 订单号，记为历史采购' : i === 5 ? '采购方不是本系统负责的取值' : null,
      record: { upstream_order_no: o.upstream_order_no, asin: o.asin, quantity: o.quantity, price_cap: o.price_cap },
      issues: i === 7 ? ['缺少收货人电话'] : [],
    })),
  }

  // ── 写回 ────────────────────────────────────────────────────────────────────

  const purchased = orders.filter((o) => o.status === 'purchased')
  const writebacks: Writeback[] = [
    {
      id: 71,
      purchase_order_id: purchased[0].id,
      upstream_order_no: purchased[0].upstream_order_no,
      connection_id: 1,
      connection_name: '订单中心 · 采购订单（DEMO）',
      record_id: `recDEMO${String(purchased[0].id).padStart(6, '0')}`,
      result_version: 1,
      fields: { 买家号: '环境-DEMO-01', AMZ订单号: '111-0000000-0000001', 采购状态: '已拍单', 税前总额USD: 21.49 },
      status: 'pending',
      tries: 0,
      last_error: null,
      next_try_at: ago(-1),
      created_at: ago(60 * 20),
      done_at: null,
    },
    {
      id: 70,
      purchase_order_id: purchased[1].id,
      upstream_order_no: purchased[1].upstream_order_no,
      connection_id: 1,
      connection_name: '订单中心 · 采购订单（DEMO）',
      record_id: `recDEMO${String(purchased[1].id).padStart(6, '0')}`,
      result_version: 2,
      fields: { 买家号: '环境-DEMO-02', AMZ订单号: '111-0000000-0000002', 采购状态: '已拍单' },
      status: 'failed',
      tries: 3,
      last_error: '飞书接口返回 1254045：字段「AMZ订单号」不存在',
      next_try_at: ago(-5),
      created_at: ago(60 * 26),
      done_at: null,
    },
    {
      id: 64,
      purchase_order_id: purchased[1].id,
      upstream_order_no: purchased[1].upstream_order_no,
      connection_id: 1,
      connection_name: '订单中心 · 采购订单（DEMO）',
      record_id: `recDEMO${String(purchased[1].id).padStart(6, '0')}`,
      result_version: 1,
      fields: { 买家号: '环境-DEMO-02' },
      status: 'superseded',
      tries: 1,
      last_error: null,
      next_try_at: null,
      created_at: ago(60 * 27),
      done_at: null,
    },
    {
      id: 60,
      purchase_order_id: 90,
      upstream_order_no: 'PO-DEMO-20261090-SKU90',
      connection_id: 1,
      connection_name: '订单中心 · 采购订单（DEMO）',
      record_id: 'recDEMO000090',
      result_version: 1,
      fields: { 买家号: '环境-DEMO-01', AMZ订单号: '111-0000000-0000090', 采购状态: '已拍单' },
      status: 'done',
      tries: 1,
      last_error: null,
      next_try_at: null,
      created_at: ago(60 * 48),
      done_at: ago(60 * 47.9),
    },
  ]

  // ── 文件导入 ────────────────────────────────────────────────────────────────

  const batches: ImportBatch[] = [
    {
      id: 41,
      filename: 'orders-demo-1105.xlsx',
      sheet_name: '采购订单',
      status: 'committed',
      stats: { total: 36, new: 24, update: 5, unchanged: 3, conflict: 1, historical: 2, invalid: 1, pending_review: 4, needs_info: 2 },
      created_at: ago(60 * 5),
      committed_at: ago(60 * 5 - 3),
      uploaded_by_name: 'DEMO 所有者',
    },
    {
      // 上传后没有提交也没有放弃：批次列表里可以「继续」或「放弃」
      id: 43,
      filename: 'orders-demo-draft.xlsx',
      sheet_name: '采购订单',
      status: 'uploaded',
      stats: null,
      // jsonb 不保证键的顺序；GET /imports/{id} 另给原文件顺序的 headers 与全部工作表
      mapping: { 限价: 'price_cap', 平台订单号: 'upstream_order_no', 备注: null, ASIN: 'asin', 数量: 'quantity' },
      size: 18_240,
      sheets: [
        { name: '采购订单', row_count: 12, headers: ['平台订单号', 'ASIN', '数量', '限价', '备注'] },
        { name: '说明', row_count: 2, headers: ['说明'] },
      ],
      headers: ['平台订单号', 'ASIN', '数量', '限价', '备注'],
      created_at: ago(30),
      committed_at: null,
      uploaded_by_name: 'DEMO 所有者',
    },
    {
      id: 40,
      filename: 'orders-demo-1104.csv',
      sheet_name: null,
      status: 'discarded',
      stats: { total: 12, new: 0, update: 0, unchanged: 0, conflict: 0, historical: 0, invalid: 12, pending_review: 0, needs_info: 0 },
      created_at: ago(60 * 26),
      committed_at: null,
      uploaded_by_name: 'DEMO 所有者',
    },
    {
      id: 39,
      filename: '小蜜蜂导出-DEMO.xlsx',
      sheet_name: 'Sheet1',
      status: 'committed',
      stats: { total: 18, new: 18, update: 0, unchanged: 0, conflict: 0, historical: 0, invalid: 0, pending_review: 18, needs_info: 0 },
      created_at: ago(60 * 50),
      committed_at: ago(60 * 50 - 2),
      uploaded_by_name: 'DEMO 所有者',
    },
  ]

  const templates: ImportTemplate[] = [
    { id: 1, name: '小蜜蜂导出', mapping: { 平台订单号: 'upstream_order_no', 买家号1: 'external_buyer_account', 数量1: 'quantity' }, created_at: ago(60 * 24 * 3), created_by_name: 'DEMO 所有者' },
  ]

  const headers = ['平台订单号', 'ASIN', '数量', '采购单价', '限价', '下单时间', '配送天数', '人工审核', '收货人姓名', '收货人地址', '收货人城市', '收货人州/省', '邮编', '收货人电话', '备注']
  const suggested: Record<string, string | null> = {
    平台订单号: 'upstream_order_no',
    ASIN: 'asin',
    数量: 'quantity',
    采购单价: 'unit_price',
    限价: 'price_cap',
    下单时间: 'order_time',
    配送天数: 'ship_days',
    人工审核: 'review_value',
    收货人姓名: 'recipient_name',
    收货人地址: 'address1',
    收货人城市: 'city',
    '收货人州/省': 'state',
    邮编: 'postal_code',
    收货人电话: 'phone',
    备注: null,
  }
  const upload: UploadResult = {
    batch_id: 42,
    filename: 'orders-demo-today.xlsx',
    sheets: [
      { name: '采购订单', row_count: 28, headers },
      { name: '说明', row_count: 4, headers: ['说明'] },
    ],
    sheet: '采购订单',
    headers,
    suggested_mapping: suggested,
    system_fields: systemFields.source_fields,
    templates: templates.map((t) => ({ id: t.id, name: t.name })),
    matched_template_id: null,
    previous_upload: { id: 41, status: 'committed', created_at: ago(60 * 5) },
  }

  const verdicts: ImportRow['verdict'][] = ['new', 'new', 'update', 'new', 'unchanged', 'historical', 'invalid', 'new', 'conflict', 'new']
  const importRows: ImportRow[] = verdicts.map((v, i) => ({
    row_no: i + 2,
    verdict: v,
    issues:
      v === 'invalid'
        ? ['同批次重复的平台订单号，只取第一行']
        : v === 'conflict'
          ? ['与飞书来源的数量不同']
          : i === 3
            ? ['缺少人审结果 → 待确认']
            : i === 7
              ? ['缺少收货人电话 → 资料待补齐']
              : [],
    reason: v === 'invalid' ? '与第 2 行的平台订单号重复，同一批次只导入第一行' : v === 'historical' ? '带 AMZ 订单号，按历史采购登记' : null,
    status: v === 'invalid' ? null : v === 'historical' ? 'purchased' : i === 3 ? 'pending_review' : i === 7 ? 'needs_info' : 'queued',
    upstream_order_no: `PO-DEMO-${20261200 + (v === 'invalid' ? 1 : i + 1)}-SKU${String(i + 1).padStart(2, '0')}`,
    normalized: { asin: `B0DEMO${String(200 + i).padStart(4, '0')}`, quantity: 1 + (i % 2), price_cap: (25 + i * 3.5).toFixed(2) },
    raw: { 平台订单号: `PO-DEMO-${20261200 + i + 1}`, 数量: String(1 + (i % 2)) },
  }))

  const importPreview: ImportPreview = {
    batch: {
      id: 42,
      filename: upload.filename,
      sheet_name: '采购订单',
      status: 'previewed',
      stats: { total: 28, new: 19, update: 3, unchanged: 2, conflict: 1, historical: 2, invalid: 1, pending_review: 3, needs_info: 2 },
      created_at: ago(1),
      committed_at: null,
      uploaded_by_name: 'DEMO 所有者',
    },
    stats: { total: 28, new: 19, update: 3, unchanged: 2, conflict: 1, historical: 2, invalid: 1, pending_review: 3, needs_info: 2 },
    rows: { total: 28, rows: importRows },
  }

  return {
    now,
    bjToday,
    me,
    settings,
    accounts,
    devices,
    orders,
    details,
    lastAllocation,
    overview,
    connections,
    syncRuns,
    systemFields,
    resolvedLink,
    feishuFields,
    feishuPreview,
    writebacks,
    batches,
    templates,
    upload,
    importRows,
    importPreview,
  }
}
