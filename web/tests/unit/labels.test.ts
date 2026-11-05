/**
 * 标签表覆盖每一个后端枚举值：先核对前端枚举与 schema.sql 的 CHECK 约束、services/models.py、开发契约一致，
 * 再核对每张标签表的键恰好等于对应枚举。
 */
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import * as T from '@/api/types'
import * as L from '@/utils/labels'

const repo = resolve(process.cwd(), '..')
const schemaPath = resolve(repo, 'backend/refdata/schema.sql')
const modelsPath = resolve(repo, 'backend/services/models.py')
const contractPath = resolve(repo, 'docs/development/开发契约-v2.md')
const servicesDir = resolve(repo, 'backend/services')
const ordersPath = resolve(servicesDir, 'orders.py')
const importsPath = resolve(servicesDir, 'imports.py')
const executionPath = resolve(servicesDir, 'execution.py')
const accountsPath = resolve(servicesDir, 'accounts.py')
const haveRepo = [schemaPath, modelsPath, contractPath, ordersPath, executionPath, accountsPath].every((p) => existsSync(p))

/** 输入：目录 → 输出：其下（含子目录，跳过 __pycache__ 与 tests）的全部 .py 文件 */
function pyFiles(dir: string): string[] {
  if (!existsSync(dir)) return []
  return readdirSync(dir).flatMap((f) => {
    const p = resolve(dir, f)
    if (f === '__pycache__' || f === 'tests') return []
    if (statSync(p).isDirectory()) return pyFiles(p)
    return f.endsWith('.py') ? [p] : []
  })
}

/** 输入：无 → 输出：services、server、workflows 里写进事件的全部 kind（「对象.动作」形式的字面量） */
function backendEventKinds(): string[] {
  const files = ['services', 'server', 'workflows'].flatMap((d) => pyFiles(resolve(repo, 'backend', d)))
  const prefixes = ['order', 'attempt', 'account', 'device', 'run', 'connection', 'batch', 'settings', 'simulation', 'feishu', 'import_template']
  const re = new RegExp(`"((?:${prefixes.join('|')})\\.[a-z_]+)"`, 'g')
  const kinds = new Set<string>()
  for (const f of files) for (const m of readFileSync(f, 'utf8').matchAll(re)) kinds.add(m[1])
  // 同样形状但不是事件的字面量（飞书域名）
  for (const notKind of ['feishu.cn']) kinds.delete(notKind)
  return [...kinds].sort()
}

/** 输入：schema.sql 文本、表名、列名 → 输出：该列 CHECK (col IN (...)) 的取值 */
function checkValues(sql: string, table: string, column: string): string[] {
  const start = sql.indexOf(`CREATE TABLE IF NOT EXISTS procure.${table} (`)
  if (start < 0) throw new Error(`schema.sql 里没有表 ${table}`)
  const end = sql.indexOf('\n);', start)
  const body = sql.slice(start, end)
  const re = new RegExp(`CHECK \\(${column} IN \\(([\\s\\S]*?)\\)\\)`)
  const m = re.exec(body)
  if (!m) throw new Error(`${table}.${column} 没有 IN 约束`)
  return [...m[1].matchAll(/'([^']+)'/g)].map((x) => x[1])
}

/** 输入：Python 源文件文本、元组名 → 输出：元组里的字符串 */
function pyTuple(src: string, name: string): string[] {
  const m = new RegExp(`^${name} = \\(([\\s\\S]*?)\\)\\n`, 'm').exec(src)
  if (!m) throw new Error(`没有元组 ${name}`)
  return [...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1])
}

const sorted = (xs: readonly string[]) => [...xs].sort()

describe.skipIf(!haveRepo)('前端枚举与后端定义一致', () => {
  const sql = haveRepo ? readFileSync(schemaPath, 'utf8') : ''
  const models = haveRepo ? readFileSync(modelsPath, 'utf8') : ''
  const contract = haveRepo ? readFileSync(contractPath, 'utf8') : ''

  it.each([
    ['purchase_orders', 'status', T.ORDER_STATUSES],
    ['purchase_orders', 'review_status', T.REVIEW_STATUSES],
    ['purchase_orders', 'review_source', T.REVIEW_SOURCES],
    ['purchase_orders', 'primary_source_kind', T.SOURCE_KINDS],
    ['purchase_orders', 'assignment_kind', T.ASSIGNMENT_KINDS],
    ['purchase_orders', 'suggested_date_basis', T.SUGGESTED_DATE_BASES],
    ['purchase_attempts', 'status', T.ATTEMPT_STATUSES],
    ['purchase_attempts', 'kind', T.ATTEMPT_KINDS],
    ['order_sources', 'kind', T.SOURCE_KINDS],
    ['devices', 'identity_status', T.IDENTITY_STATUSES],
    ['buyer_accounts', 'purchase_mode', T.PURCHASE_MODES],
    ['writebacks', 'status', T.WRITEBACK_STATUSES],
    ['import_rows', 'verdict', T.IMPORT_VERDICTS],
    ['import_batches', 'status', T.BATCH_STATUSES],
    ['feishu_sync_runs', 'trigger', T.SYNC_TRIGGERS],
    ['execution_runs', 'trigger', T.RUN_TRIGGERS],
    ['events', 'entity', T.EVENT_ENTITIES],
    ['events', 'actor_kind', T.ACTOR_KINDS],
    ['workspace_members', 'role', T.MEMBER_ROLES],
  ] as const)('schema.sql %s.%s', (table, column, values) => {
    expect(sorted(values)).toEqual(sorted(checkValues(sql, table, column)))
  })

  it('任务状态与 models.ORDER_STATUSES、中文名与 ORDER_STATUS_ZH 一致', () => {
    expect(sorted(T.ORDER_STATUSES)).toEqual(sorted(pyTuple(models, 'ORDER_STATUSES')))
    for (const [k, zh] of models.matchAll(/^\s+"(\w+)": "([^"]+)",$/gm)) {
      if ((T.ORDER_STATUSES as readonly string[]).includes(k)) expect(L.ORDER_STATUS_LABEL[k as T.OrderStatus]).toBe(zh)
    }
  })

  it('尝试状态与 models.ATTEMPT_STATUSES 一致', () => {
    expect(sorted(T.ATTEMPT_STATUSES)).toEqual(sorted(pyTuple(models, 'ATTEMPT_STATUSES')))
  })

  it('插件错误码 + 护栏码 = models.ERROR_CODES，中文说明一致', () => {
    const codes = [...models.matchAll(/ErrorCode\("(\w+)", "(\w+)", "([^"]+)"\)/g)]
    expect(codes.length).toBeGreaterThan(0)
    const guard = codes.filter((c) => c[2] === 'guard').map((c) => c[1])
    const plugin = codes.filter((c) => c[2] !== 'guard').map((c) => c[1])
    expect(sorted(T.GUARD_CODES)).toEqual(sorted(guard))
    expect(sorted(T.PLUGIN_ERROR_CODES)).toEqual(sorted(plugin))
    for (const [, code, , zh] of codes) expect(L.errorCodeLabel(code)).toBe(zh)
  })

  it('统一错误码与契约 §8 一致', () => {
    const line = contract.split('\n').find((l) => l.startsWith('统一错误码'))
    expect(line).toBeTruthy()
    const codes = [...(line ?? '').matchAll(/`([A-Z_]+)`/g)].map((m) => m[1])
    // 顺序也一致（前端错误码表按契约的顺序排）
    expect([...T.API_ERROR_CODES]).toEqual(codes)
  })

  it('服务端会返回的错误码都在统一错误码里（services、server、api 里的 DomainError / code= / error_response）', () => {
    const files = ['services', 'server', 'api'].flatMap((d) => pyFiles(resolve(repo, 'backend', d)))
    const used = new Set<string>()
    const re = /(?:code=|DomainError\(|error_response\(|error_body\()"([A-Z_]+)"/g
    for (const f of files) {
      const text = readFileSync(f, 'utf8')
      for (const m of text.matchAll(re)) used.add(m[1])
      const http = /_HTTP_CODES = \{([^}]*)\}/.exec(text)?.[1]
      if (http) for (const m of http.matchAll(/"([A-Z_]+)"/g)) used.add(m[1])
    }
    expect(used.size).toBeGreaterThan(10)
    const missing = [...used].filter((c) => !(T.API_ERROR_CODES as readonly string[]).includes(c))
    expect(missing).toEqual([])
  })

  it('任务列表视图与契约 §8 一致', () => {
    const line = contract.split('\n').find((l) => l.includes('GET /orders?view='))
    const views = [...(line ?? '').split('视图：')[1].split('；')[0].matchAll(/`(\w+)`/g)].map((m) => m[1])
    expect(sorted(T.ORDER_VIEWS)).toEqual(sorted(views))
  })

  it('allowed_actions 与 services/orders.allowed_actions 能给出的动作一致', () => {
    // 只取 allowed_actions 函数体里 append 的取值
    const src = readFileSync(ordersPath, 'utf8')
    const body = /def allowed_actions\([\s\S]*?\n    return acts\n/.exec(src)?.[0] ?? ''
    const actions = [...body.matchAll(/acts\.append\("(\w+)"\)/g)].map((m) => m[1])
    expect(actions.length).toBeGreaterThan(0)
    expect(sorted(T.ORDER_ACTIONS)).toEqual(sorted([...new Set(actions)]))
  })

  it('allowed_actions 与契约 §10 任务详情列出的动作一致', () => {
    const line = contract.split('\n').find((l) => l.startsWith('**任务详情**') && l.includes('`allowed_actions`'))
    expect(line).toBeTruthy()
    const list = (line ?? '').split('`allowed_actions`（')[1]?.split('的子集')[0] ?? ''
    const actions = [...list.matchAll(/`(\w+)`/g)].map((m) => m[1])
    expect(actions.length).toBeGreaterThan(0)
    expect(sorted(T.ORDER_ACTIONS)).toEqual(sorted(actions))
  })

  it('进度叫停码与 services/execution.CANCEL_CODES 一致', () => {
    expect(sorted(T.PROGRESS_CANCEL_CODES)).toEqual(sorted(pyTuple(readFileSync(executionPath, 'utf8'), 'CANCEL_CODES')))
  })

  it('账号暂停起因与 services/accounts.PAUSE_CAUSES 一致', () => {
    expect(sorted(T.PAUSE_CAUSES)).toEqual(sorted(pyTuple(readFileSync(accountsPath, 'utf8'), 'PAUSE_CAUSES')))
  })

  it('冲突种类与 services/orders._CONFLICT_RANK 一致', () => {
    const body = /^_CONFLICT_RANK = \{([^}]*)\}/m.exec(readFileSync(ordersPath, 'utf8'))?.[1] ?? ''
    const kinds = [...body.matchAll(/"(\w+)":/g)].map((m) => m[1])
    expect(kinds.length).toBeGreaterThan(0)
    expect(sorted(T.CONFLICT_KINDS)).toEqual(sorted(kinds))
  })

  it('每个事件 kind 都有中文名（services 里 events.record 的全部取值）', () => {
    const kinds = backendEventKinds()
    expect(kinds.length).toBeGreaterThan(40)
    const missing = kinds.filter((k) => !(k in L.EVENT_KIND_LABEL))
    expect(missing).toEqual([])
    for (const k of kinds) expect(L.eventKindLabel(k)).not.toBe(k)
  })

  it('预览结论的中文名与 services/imports.VERDICT_ZH 一致', () => {
    const src = readFileSync(importsPath, 'utf8')
    const body = /VERDICT_ZH = \{([\s\S]*?)\}/.exec(src)?.[1] ?? ''
    const zh = Object.fromEntries([...body.matchAll(/"(\w+)": "([^"]+)"/g)].map((m) => [m[1], m[2]]))
    expect(Object.keys(zh).sort()).toEqual(sorted(T.IMPORT_VERDICTS))
    for (const v of T.IMPORT_VERDICTS) expect(L.IMPORT_VERDICT_LABEL[v]).toBe(zh[v])
  })

  it('执行所需字段的中文名与 models.REQUIRED_FOR_EXECUTION 一致（与状态说明「缺少：…」用同一套字）', () => {
    const body = /REQUIRED_FOR_EXECUTION = \(([\s\S]*?)\n\)/.exec(models)?.[1] ?? ''
    const pairs = [...body.matchAll(/\("(\w+)", "([^"]+)"\)/g)].map((m) => [m[1], m[2]])
    expect(pairs.length).toBe(11)
    for (const [k, zh] of pairs) expect(L.fieldLabel(k)).toBe(zh)
  })

  it('飞书预览结论与契约 §10 一致', () => {
    const line = contract.split('\n').find((l) => l.includes('POST /feishu/preview` →'))
    const verdicts = [...(line ?? '').split('`verdict` 取')[1].matchAll(/`(\w+)`/g)].map((m) => m[1])
    expect(sorted(T.FEISHU_VERDICTS)).toEqual(sorted(verdicts))
  })

  it('写回状态 writeback_state 与契约 §10 一致', () => {
    const line = contract.split('\n').find((l) => l.includes('writeback_state: '))
    const states = [...(line ?? '').split('writeback_state:')[1].split(',')[0].matchAll(/"(\w+)"/g)].map((m) => m[1])
    expect(sorted(T.WRITEBACK_STATES)).toEqual(sorted(states))
  })

  it('分配触发方式与 schema.sql 注释一致', () => {
    const m = /trigger\s+text NOT NULL,\s+-- ([^\n]+)/.exec(sql.slice(sql.indexOf('procure.allocation_runs')))
    const triggers = (m?.[1] ?? '').split('/').map((s) => s.trim())
    expect(sorted(T.ALLOCATION_TRIGGERS)).toEqual(sorted(triggers))
  })

  it('执行批次结束原因与 schema.sql 注释一致', () => {
    const m = /end_reason\s+text,\s+-- ([^\n]+)/.exec(sql)
    const reasons = (m?.[1] ?? '').split('/').map((s) => s.trim())
    expect(sorted(T.RUN_END_REASONS)).toEqual(sorted(reasons))
  })

  it('人工核实结论与 schema.sql 注释一致', () => {
    const m = /resolution\s+text,\s+-- 人工核实：([^\n]+)/.exec(sql)
    const values = (m?.[1] ?? '').split('/').map((s) => s.trim())
    expect(sorted(T.RESOLUTIONS)).toEqual(sorted(values))
  })
})

describe('每张标签表恰好覆盖对应枚举', () => {
  const cases: [string, readonly string[], Record<string, string>][] = [
    ['ORDER_STATUS_LABEL', T.ORDER_STATUSES, L.ORDER_STATUS_LABEL],
    ['ORDER_STATUS_TAG', T.ORDER_STATUSES, L.ORDER_STATUS_TAG],
    ['ORDER_VIEW_LABEL', T.ORDER_VIEWS, L.ORDER_VIEW_LABEL],
    ['ATTEMPT_STATUS_LABEL', T.ATTEMPT_STATUSES, L.ATTEMPT_STATUS_LABEL],
    ['ATTEMPT_STATUS_TAG', T.ATTEMPT_STATUSES, L.ATTEMPT_STATUS_TAG],
    ['ATTEMPT_KIND_LABEL', T.ATTEMPT_KINDS, L.ATTEMPT_KIND_LABEL],
    ['RESOLUTION_LABEL', T.RESOLUTIONS, L.RESOLUTION_LABEL],
    ['GUARD_CODE_LABEL', T.GUARD_CODES, L.GUARD_CODE_LABEL],
    ['PLUGIN_ERROR_LABEL', T.PLUGIN_ERROR_CODES, L.PLUGIN_ERROR_LABEL],
    ['PROGRESS_CANCEL_LABEL', T.PROGRESS_CANCEL_CODES, L.PROGRESS_CANCEL_LABEL],
    ['CONFLICT_KIND_LABEL', T.CONFLICT_KINDS, L.CONFLICT_KIND_LABEL],
    ['PAUSE_CAUSE_LABEL', T.PAUSE_CAUSES, L.PAUSE_CAUSE_LABEL],
    ['API_ERROR_LABEL', T.API_ERROR_CODES, L.API_ERROR_LABEL],
    ['REVIEW_STATUS_LABEL', T.REVIEW_STATUSES, L.REVIEW_STATUS_LABEL],
    ['REVIEW_STATUS_TAG', T.REVIEW_STATUSES, L.REVIEW_STATUS_TAG],
    ['REVIEW_SOURCE_LABEL', T.REVIEW_SOURCES, L.REVIEW_SOURCE_LABEL],
    ['SOURCE_KIND_LABEL', T.SOURCE_KINDS, L.SOURCE_KIND_LABEL],
    ['ASSIGNMENT_KIND_LABEL', T.ASSIGNMENT_KINDS, L.ASSIGNMENT_KIND_LABEL],
    ['SUGGESTED_DATE_BASIS_LABEL', T.SUGGESTED_DATE_BASES, L.SUGGESTED_DATE_BASIS_LABEL],
    ['ALLOCATION_TRIGGER_LABEL', T.ALLOCATION_TRIGGERS, L.ALLOCATION_TRIGGER_LABEL],
    ['PURCHASE_MODE_LABEL', T.PURCHASE_MODES, L.PURCHASE_MODE_LABEL],
    ['IDENTITY_STATUS_LABEL', T.IDENTITY_STATUSES, L.IDENTITY_STATUS_LABEL],
    ['IDENTITY_STATUS_TAG', T.IDENTITY_STATUSES, L.IDENTITY_STATUS_TAG],
    ['IDENTITY_STATUS_DOT', T.IDENTITY_STATUSES, L.IDENTITY_STATUS_DOT],
    ['RUN_TRIGGER_LABEL', T.RUN_TRIGGERS, L.RUN_TRIGGER_LABEL],
    ['RUN_END_REASON_LABEL', T.RUN_END_REASONS, L.RUN_END_REASON_LABEL],
    ['MEMBER_ROLE_LABEL', T.MEMBER_ROLES, L.MEMBER_ROLE_LABEL],
    ['IMPORT_VERDICT_LABEL', T.IMPORT_VERDICTS, L.IMPORT_VERDICT_LABEL],
    ['FEISHU_VERDICT_LABEL', T.FEISHU_VERDICTS, L.FEISHU_VERDICT_LABEL],
    ['VERDICT_TAG', T.FEISHU_VERDICTS, L.VERDICT_TAG],
    ['BATCH_STATUS_LABEL', T.BATCH_STATUSES, L.BATCH_STATUS_LABEL],
    ['BATCH_STATUS_TAG', T.BATCH_STATUSES, L.BATCH_STATUS_TAG],
    ['SYNC_TRIGGER_LABEL', T.SYNC_TRIGGERS, L.SYNC_TRIGGER_LABEL],
    ['WRITEBACK_STATUS_LABEL', T.WRITEBACK_STATUSES, L.WRITEBACK_STATUS_LABEL],
    ['WRITEBACK_STATUS_TAG', T.WRITEBACK_STATUSES, L.WRITEBACK_STATUS_TAG],
    ['WRITEBACK_STATE_LABEL', T.WRITEBACK_STATES, L.WRITEBACK_STATE_LABEL],
    ['WRITEBACK_STATE_TAG', T.WRITEBACK_STATES, L.WRITEBACK_STATE_TAG],
    ['EVENT_ENTITY_LABEL', T.EVENT_ENTITIES, L.EVENT_ENTITY_LABEL],
    ['ACTOR_KIND_LABEL', T.ACTOR_KINDS, L.ACTOR_KIND_LABEL],
    ['ORDER_ACTION_LABEL', T.ORDER_ACTIONS, L.ORDER_ACTION_LABEL],
    ['GUARD_CHECK_LABEL', [...T.GUARD_CHECK_KEYS, 'fulfillment'], L.GUARD_CHECK_LABEL],
  ]
  it.each(cases)('%s', (_name, values, map) => {
    expect(sorted(Object.keys(map))).toEqual(sorted(values))
    for (const v of values) {
      expect(typeof map[v]).toBe('string')
      expect(map[v].length).toBeGreaterThan(0)
    }
  })

  it('中文标签不含感叹号与 emoji', () => {
    const all = [...cases.flatMap(([, , map]) => Object.values(map)), ...Object.values(L.EVENT_KIND_LABEL)]
    for (const s of all) {
      expect(s).not.toMatch(/[!！]/)
      expect(s).not.toMatch(/\p{Extended_Pictographic}/u)
    }
  })

  it('el-tag 类型只用 Element Plus 认的五种', () => {
    const tagMaps = [L.ORDER_STATUS_TAG, L.ATTEMPT_STATUS_TAG, L.REVIEW_STATUS_TAG, L.IDENTITY_STATUS_TAG, L.VERDICT_TAG, L.BATCH_STATUS_TAG, L.WRITEBACK_STATUS_TAG, L.WRITEBACK_STATE_TAG]
    for (const m of tagMaps) for (const t of Object.values(m)) expect(['primary', 'success', 'warning', 'danger', 'info']).toContain(t)
  })

  it('执行所需字段都有中文名（missing_fields 展示）', () => {
    for (const k of ['asin', 'quantity', 'unit_price', 'price_cap', 'suggested_purchase_date', 'recipient_name', 'address1', 'city', 'state', 'postal_code', 'phone']) {
      expect(L.fieldLabel(k)).not.toBe(k)
    }
    expect(L.fieldLabel('ASIN')).toBe('ASIN')
  })

  it('未知取值原样显示、空值显示「—」', () => {
    expect(L.labelOf(L.ORDER_STATUS_LABEL, 'something_new')).toBe('something_new')
    expect(L.labelOf(L.ORDER_STATUS_LABEL, null)).toBe('—')
    expect(L.tagOf(L.ORDER_STATUS_TAG, 'something_new')).toBe('info')
    expect(L.errorCodeLabel('NEW_CODE')).toBe('NEW_CODE')
    expect(L.eventKindLabel('custom_kind')).toBe('custom_kind')
  })
})
