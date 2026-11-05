/**
 * 模拟后端：输入请求的方法、URL、JSON 体 → 输出 {status, body}。路由与形状按开发契约 §8、§10。
 * Playwright 截图脚本用 page.route() 把 /api/v1/** 交给 handleApi；评审阶段也可以直接复用。
 * 写操作只改内存里的一份 fixtures，刷新页面不保留。
 */
import type { OrderRow, OrderView } from '../../src/api/types.ts'
import { buildFixtures, type Fixtures } from './fixtures.ts'

export interface MockOptions {
  /** 飞书应用是否已配置（false 时订单来源显示「未配置飞书应用」） */
  feishuConfigured?: boolean
  /** 尚无用户（首次设置） */
  needsSetup?: boolean
  /** 是否已登录 */
  loggedIn?: boolean
}

export interface MockResponse {
  status: number
  body: unknown
}

const ok = (body: unknown): MockResponse => ({ status: 200, body })
const err = (status: number, code: string, message: string, detail: Record<string, unknown> = {}): MockResponse => ({
  status,
  body: { code, message, detail },
})

/** 输入：任务行、视图、北京今天 → 输出：是否属于该视图（与契约 §8 视图定义一致） */
export function inView(o: OrderRow, view: OrderView | string, today: string): boolean {
  switch (view) {
    case 'today':
      return o.status === 'assigned' && !!o.suggested_purchase_date && o.suggested_purchase_date <= today
    case 'future':
      return o.status === 'assigned' && !!o.suggested_purchase_date && o.suggested_purchase_date > today
    case 'unassigned':
      return o.status === 'queued'
    case 'all':
    case '':
      return true
    default:
      return o.status === view
  }
}

function page<T>(rows: T[], q: URLSearchParams) {
  const p = Math.max(1, Number(q.get('page') ?? 1))
  const size = Math.max(1, Number(q.get('page_size') ?? 20))
  return { total: rows.length, rows: rows.slice((p - 1) * size, p * size) }
}

/** 输入：选项与基准时刻 → 输出：带状态的请求处理函数 */
export function createMockApi(opts: MockOptions = {}, now: Date = new Date()) {
  const fx: Fixtures = buildFixtures(now)
  const state = { loggedIn: opts.loggedIn ?? true, needsSetup: opts.needsSetup ?? false }
  const feishuConfigured = opts.feishuConfigured ?? true
  fx.overview.feishu_configured = feishuConfigured

  function handle(method: string, url: URL, body: unknown): MockResponse {
    const path = url.pathname.replace(/^\/api\/v1/, '')
    const q = url.searchParams
    const m = method.toUpperCase()
    const b = (body ?? {}) as Record<string, unknown>
    let r: RegExpExecArray | null

    // ── 认证 ──
    if (path === '/auth/state') return ok({ needs_setup: state.needsSetup })
    if (path === '/auth/setup' && m === 'POST') {
      state.needsSetup = false
      state.loggedIn = true
      return ok(fx.me)
    }
    if (path === '/auth/login' && m === 'POST') {
      if (b.password === 'wrong') return err(401, 'UNAUTHORIZED', '用户名或密码不对')
      state.loggedIn = true
      return ok(fx.me)
    }
    if (path === '/auth/logout' && m === 'POST') {
      state.loggedIn = false
      return { status: 204, body: null }
    }
    if (state.needsSetup) return err(401, 'UNAUTHORIZED', '尚未完成首次设置', { needs_setup: true })
    if (!state.loggedIn) return err(401, 'UNAUTHORIZED', '请先登录')
    if (path === '/auth/me') return ok(fx.me)

    // ── 概览与分配 ──
    if (path === '/overview') return ok(fx.overview)
    if (path === '/allocation/last') return ok(fx.lastAllocation)
    if (path === '/allocation/run' && m === 'POST') return ok({ ...fx.lastAllocation, id: fx.lastAllocation.id + 1, trigger: 'manual', at: now.toISOString() })

    // ── 任务 ──
    if (path === '/orders' && m === 'GET') {
      const view = q.get('view') ?? 'all'
      const kw = (q.get('q') ?? '').trim().toLowerCase()
      let rows = fx.orders.filter((o) => inView(o, view, fx.bjToday))
      if (q.get('needs_attention') === 'true') rows = rows.filter((o) => o.needs_attention)
      if (kw) rows = rows.filter((o) => [o.upstream_order_no, o.asin_eff, o.recipient_name].some((x) => (x ?? '').toLowerCase().includes(kw)))
      const acc = q.get('account_id')
      if (acc) rows = rows.filter((o) => String(o.assigned_account?.id) === acc)
      return ok(page(rows, q))
    }
    if (path === '/orders/bulk-review' && m === 'POST') return ok({ updated: Array.isArray(b.ids) ? b.ids.length : 0 })
    if ((r = /^\/orders\/(\d+)$/.exec(path))) {
      const d = fx.details[Number(r[1])]
      return d ? ok(d) : err(404, 'NOT_FOUND', '任务不存在')
    }
    if ((r = /^\/orders\/(\d+)\/([a-z-]+)$/.exec(path)) && m === 'POST') {
      const d = fx.details[Number(r[1])]
      if (!d) return err(404, 'NOT_FOUND', '任务不存在')
      const action = r[2]
      if (action === 'assign') {
        const a = fx.accounts.find((x) => x.id === b.account_id)
        if (!a) return err(422, 'VALIDATION', '目标账号不存在')
        if (!a.purchase_enabled) return err(409, 'CONFLICT', '目标账号的采购开关已关闭')
        Object.assign(d, { status: 'assigned', assigned_account: { id: a.id, name: a.name }, assignment_kind: 'manual', assignment_note: '人工改派' })
      } else if (action === 'unassign') {
        Object.assign(d, { status: 'queued', assigned_account: null, assignment_kind: null, assignment_note: null })
      } else if (action === 'cancel') {
        Object.assign(d, { status: 'cancelled', status_reason: `人工取消：${String(b.note ?? '')}`, allowed_actions: ['uncancel'] })
      } else if (action === 'review') {
        Object.assign(d, { review_status: b.decision === 'approve' ? 'approved' : 'rejected' })
      }
      return ok(d)
    }

    // ── 账号与设备 ──
    if (path === '/accounts' && m === 'GET') return ok(page(fx.accounts, q))
    if (path === '/accounts' && m === 'POST') return ok({ ...fx.accounts[0], ...b, id: 99, quota_today: { limit: Number(b.daily_limit ?? 20), used: 0, planned: 0, available: Number(b.daily_limit ?? 20) }, devices: [], active_run: null })
    if ((r = /^\/accounts\/(\d+)$/.exec(path))) {
      const a = fx.accounts.find((x) => x.id === Number(r![1]))
      if (!a) return err(404, 'NOT_FOUND', '账号不存在')
      if (m === 'PATCH') Object.assign(a, b)
      return ok(a)
    }
    if ((r = /^\/accounts\/(\d+)\/(pause|resume|adopt-customer-id)$/.exec(path)) && m === 'POST') {
      const a = fx.accounts.find((x) => x.id === Number(r![1]))
      if (!a) return err(404, 'NOT_FOUND', '账号不存在')
      if (r[2] === 'pause') Object.assign(a, { execution_paused: true, pause_reason: String(b.reason ?? ''), paused_at: now.toISOString() })
      if (r[2] === 'resume') Object.assign(a, { execution_paused: false, pause_reason: null, paused_at: null })
      return ok(a)
    }
    if (path === '/devices' && m === 'GET') return ok(page(fx.devices, q))
    if (path === '/devices/pairing-codes' && m === 'POST') return ok({ code: 'DEMO7K2Q', expires_at: new Date(now.getTime() + 10 * 60_000).toISOString() })
    if ((r = /^\/devices\/(\d+)(\/revoke)?$/.exec(path))) {
      const d = fx.devices.find((x) => x.id === Number(r![1]))
      if (!d) return err(404, 'NOT_FOUND', '设备不存在')
      if (r[2] && m === 'POST') Object.assign(d, { revoked_at: now.toISOString(), online: false })
      if (!r[2] && m === 'PATCH') {
        if (typeof b.name === 'string') d.name = b.name
        if ('bound_account_id' in b) {
          const a = fx.accounts.find((x) => x.id === b.bound_account_id)
          d.bound_account = a ? { id: a.id, name: a.name } : null
        }
      }
      return ok(d)
    }

    // ── 设置与模拟 ──
    if (path === '/settings') {
      if (m === 'PATCH') Object.assign(fx.settings, b)
      return ok(fx.settings)
    }
    if (path === '/simulation/reset' && m === 'POST') return ok({ attempts: 9, orders: 7, accounts: 1 })

    // ── 飞书 ──
    if (path === '/feishu/status') return ok({ configured: feishuConfigured })
    if (path.startsWith('/feishu/') && !feishuConfigured && path !== '/feishu/connections') {
      return err(409, 'FEISHU_NOT_CONFIGURED', '未配置飞书应用')
    }
    if (path === '/feishu/system-fields') return ok(fx.systemFields)
    if (path === '/feishu/resolve-link' && m === 'POST') {
      if (!String(b.link ?? '').includes('feishu.cn')) return err(422, 'VALIDATION', '不是飞书多维表格链接')
      return ok(fx.resolvedLink)
    }
    if (path === '/feishu/fields' && m === 'POST') return ok(fx.feishuFields)
    if (path === '/feishu/preview' && m === 'POST') return ok(fx.feishuPreview)
    if (path === '/feishu/connections' && m === 'GET') return ok(page(feishuConfigured ? fx.connections : [], q))
    if (path === '/feishu/connections' && m === 'POST') return ok({ ...fx.connections[0], ...b, id: 3, writeback_state: b.writeback_enabled ? 'unverified' : 'disabled' })
    if ((r = /^\/feishu\/connections\/(\d+)(?:\/(sync|verify-writeback|runs))?$/.exec(path))) {
      const c = fx.connections.find((x) => x.id === Number(r![1]))
      if (!c) return err(404, 'NOT_FOUND', '连接不存在')
      if (r[2] === 'runs') return ok(page(fx.syncRuns[c.id] ?? [], q))
      if (r[2] === 'sync') return ok(fx.syncRuns[c.id]?.[0] ?? { id: 1, connection_id: c.id, trigger: 'manual', started_at: now.toISOString(), finished_at: now.toISOString(), ok: true, stats: { read: 0 }, error: null })
      if (r[2] === 'verify-writeback') {
        Object.assign(c, { writeback_verified_at: now.toISOString(), writeback_state: 'active', writeback_test_record: String(b.record_id ?? '') })
        return ok(c)
      }
      if (m === 'PATCH') Object.assign(c, b)
      return ok(c)
    }

    // ── 写回 ──
    if (path === '/writebacks' && m === 'GET') {
      const st = q.get('status')
      return ok(page(st ? fx.writebacks.filter((w) => w.status === st) : fx.writebacks, q))
    }
    if ((r = /^\/writebacks\/(\d+)\/retry$/.exec(path)) && m === 'POST') {
      const w = fx.writebacks.find((x) => x.id === Number(r![1]))
      if (!w) return err(404, 'NOT_FOUND', '写回记录不存在')
      Object.assign(w, { status: 'pending', next_try_at: now.toISOString() })
      return ok(w)
    }

    // ── 文件导入 ──
    if (path === '/imports' && m === 'GET') return ok(page(fx.batches, q))
    if (path === '/imports' && m === 'POST') return ok(fx.upload)
    if ((r = /^\/imports\/(\d+)\/preview$/.exec(path)) && m === 'POST') return ok(fx.importPreview)
    if ((r = /^\/imports\/(\d+)\/rows$/.exec(path))) {
      const v = q.get('verdict')
      return ok(page(v ? fx.importRows.filter((x) => x.verdict === v) : fx.importRows, q))
    }
    if ((r = /^\/imports\/(\d+)\/(commit|discard)$/.exec(path)) && m === 'POST') {
      return ok({ ...fx.importPreview.batch, status: r[2] === 'commit' ? 'committed' : 'discarded', committed_at: r[2] === 'commit' ? now.toISOString() : null })
    }
    if (path === '/import-templates' && m === 'GET') return ok(page(fx.templates, q))
    if (path === '/import-templates' && m === 'POST') return ok({ id: 9, name: String(b.name ?? ''), mapping: b.mapping ?? {} })
    if ((r = /^\/import-templates\/(\d+)$/.exec(path)) && m === 'DELETE') return { status: 204, body: null }

    // ── 事件 ──
    if (path === '/events') {
      const id = Number(q.get('entity_id'))
      const d = fx.details[id]
      return ok(page(d?.events ?? [], q))
    }

    return err(404, 'NOT_FOUND', `模拟后端没有 ${m} ${path}`)
  }

  return { fixtures: fx, handle, state }
}
