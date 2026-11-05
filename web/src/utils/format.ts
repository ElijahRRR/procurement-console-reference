/**
 * 显示用的格式化函数（纯函数，tests/unit/format.test.ts 覆盖）。
 *
 * 规则：金额 $1,234.50；数字千分位；业务时刻按北京时间显示；空值（null / undefined / 解析不了）一律显示「—」，
 * 不当作 0。中文与英文、数字之间留半角空格。
 */

export const EMPTY = '—'
export const BJ_TZ = 'Asia/Shanghai'

/** 输入：任意值 → 输出：是否为空（null、undefined、空字符串） */
export function isBlank(v: unknown): v is null | undefined | '' {
  return v === null || v === undefined || (typeof v === 'string' && v.trim() === '')
}

/** 输入：金额字符串 / 数字 → 输出：有限数字或 null（"$1,234.50"、" 52 " 都能解析） */
export function parseAmount(v: unknown): number | null {
  if (isBlank(v)) return null
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  if (typeof v !== 'string') return null
  const s = v.replace(/[$,\s]/g, '').replace(/^US/i, '')
  if (!/^-?\d+(\.\d+)?$/.test(s)) return null
  const n = Number(s)
  return Number.isFinite(n) ? n : null
}

const moneyFmt = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const intFmt = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 })
const numFmt = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 })

/** 输入：金额（"1234.5"、1234.5、null）→ 输出："$1,234.50"；负数 "-$12.00"；空值 "—" */
export function formatMoney(v: unknown): string {
  const n = parseAmount(v)
  if (n === null) return EMPTY
  const abs = moneyFmt.format(Math.abs(n))
  return n < 0 ? `-$${abs}` : `$${abs}`
}

/** 输入：数字 → 输出：千分位整数 "1,284"；带小数时最多两位；空值 "—" */
export function formatNumber(v: unknown): string {
  const n = typeof v === 'string' ? parseAmount(v) : typeof v === 'number' && Number.isFinite(v) ? v : null
  if (n === null) return EMPTY
  return Number.isInteger(n) ? intFmt.format(n) : numFmt.format(n)
}

/** 输入：数字与单位 → 输出："1,284 笔"；空值 "—" */
export function formatCount(v: unknown, unit: string): string {
  const s = formatNumber(v)
  return s === EMPTY ? EMPTY : `${s} ${unit}`
}

/** 输入：区间下限、上限（上限为空表示不限）→ 输出："$20.00–$50.00" 或 "$20.00 起" */
export function formatAmountRange(min: unknown, max: unknown): string {
  const lo = parseAmount(min)
  const hi = parseAmount(max)
  if (lo === null && hi === null) return '不限'
  if (hi === null) return `${formatMoney(lo ?? 0)} 起`
  return `${formatMoney(lo ?? 0)}–${formatMoney(hi)}`
}

/** 输入：小数比例或百分数 → 输出："72%"；空值 "—" */
export function formatPercent(v: unknown, digits = 0): string {
  const n = typeof v === 'number' && Number.isFinite(v) ? v : null
  if (n === null) return EMPTY
  return `${n.toFixed(digits)}%`
}

function partsInTz(d: Date, tz: string): Record<string, string> {
  const fmt = new Intl.DateTimeFormat('en-CA', {
    timeZone: tz,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  })
  const out: Record<string, string> = {}
  for (const p of fmt.formatToParts(d)) out[p.type] = p.value
  return out
}

/** 输入：ISO 时刻 / Date → 输出：Date 或 null（解析不了为 null） */
export function toDate(v: unknown): Date | null {
  if (isBlank(v)) return null
  const d = v instanceof Date ? v : new Date(String(v))
  return Number.isNaN(d.getTime()) ? null : d
}

/** 输入：YYYY-MM-DD 或 ISO 时刻 → 输出："2026-10-31"（时刻按北京日期）；空值 "—" */
export function formatDate(v: unknown): string {
  if (isBlank(v)) return EMPTY
  const s = String(v)
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s
  const d = toDate(s)
  if (!d) return EMPTY
  const p = partsInTz(d, BJ_TZ)
  return `${p.year}-${p.month}-${p.day}`
}

/** 输入：ISO 时刻 → 输出：北京时间 "2026-10-31 21:31"（withSeconds 时带秒）；空值 "—" */
export function formatDateTime(v: unknown, withSeconds = false): string {
  const d = toDate(v)
  if (!d) return EMPTY
  const p = partsInTz(d, BJ_TZ)
  const base = `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`
  return withSeconds ? `${base}:${p.second}` : base
}

/** 输入：ISO 时刻 → 输出：北京时间 "21:31"；空值 "—" */
export function formatTime(v: unknown): string {
  const d = toDate(v)
  if (!d) return EMPTY
  const p = partsInTz(d, BJ_TZ)
  return `${p.hour}:${p.minute}`
}

/** 输入：ISO 时刻、当前时刻 → 输出："刚刚" / "5 分钟前" / "3 小时前" / "2 天前"，更早给日期；未来时刻给 "5 分钟后" */
export function formatRelative(v: unknown, now: Date = new Date()): string {
  const d = toDate(v)
  if (!d) return EMPTY
  const diff = now.getTime() - d.getTime()
  const future = diff < 0
  const sec = Math.abs(diff) / 1000
  const suffix = future ? '后' : '前'
  if (sec < 45) return future ? '即将' : '刚刚'
  if (sec < 3600) return `${Math.max(1, Math.round(sec / 60))} 分钟${suffix}`
  if (sec < 86400) return `${Math.round(sec / 3600)} 小时${suffix}`
  if (sec < 86400 * 7) return `${Math.round(sec / 86400)} 天${suffix}`
  return formatDate(d.toISOString())
}

/** 输入：两个 YYYY-MM-DD → 输出：相差天数（b − a），任一为空则 null */
export function daysBetween(a: string | null | undefined, b: string | null | undefined): number | null {
  if (!a || !b) return null
  const da = Date.parse(`${a}T00:00:00Z`)
  const db = Date.parse(`${b}T00:00:00Z`)
  if (Number.isNaN(da) || Number.isNaN(db)) return null
  return Math.round((db - da) / 86400000)
}

/** 输入：天数 → 输出："10 天"；空值 "—" */
export function formatDays(v: unknown): string {
  return formatCount(v, '天')
}

const CJK = '\\u2e80-\\u2eff\\u2f00-\\u2fdf\\u3040-\\u30ff\\u3100-\\u312f\\u3400-\\u4dbf\\u4e00-\\u9fff\\uf900-\\ufaff'
// 只在「中文 ↔ 字母 / 数字（含 $ 前缀、% 后缀）」的边界补空格；中文标点两侧不补
const CJK_THEN_LATIN = new RegExp(`([${CJK}])([A-Za-z0-9$])`, 'g')
const LATIN_THEN_CJK = new RegExp(`([A-Za-z0-9%])([${CJK}])`, 'g')

/** 输入：中文文本 → 输出：中文与英文、数字之间补半角空格（"共3笔API订单" → "共 3 笔 API 订单"） */
export function cnSpacing(text: string | null | undefined): string {
  if (!text) return ''
  return text.replace(CJK_THEN_LATIN, '$1 $2').replace(LATIN_THEN_CJK, '$1 $2')
}

/** 输入：动作名、错误（ApiError / Error / 字符串）→ 输出：「动作失败：原因」 */
export function failureText(action: string, err: unknown): string {
  const reason =
    err && typeof err === 'object' && 'message' in err && typeof (err as { message: unknown }).message === 'string'
      ? (err as { message: string }).message
      : typeof err === 'string'
        ? err
        : ''
  const clean = reason.trim().replace(/[。.]$/, '')
  return clean ? `${action}失败：${clean}` : `${action}失败`
}

/** 输入：字符串 → 输出：首尾空白去掉；空串为 null（提交表单用，空值不当作 0 或空串写入） */
export function blankToNull(v: string | null | undefined): string | null {
  if (v === null || v === undefined) return null
  const s = v.trim()
  return s === '' ? null : s
}

/** 输入：金额输入 → 输出：规范的两位小数字符串或 null（"12.5" → "12.50"；解析不了为 null） */
export function normalizeMoneyInput(v: string | number | null | undefined): string | null {
  const n = parseAmount(v)
  return n === null ? null : n.toFixed(2)
}

/** 输入：任意值 → 输出：快照 / 预览里展示用的短文本 */
export function displayValue(v: unknown): string {
  if (isBlank(v)) return EMPTY
  if (typeof v === 'boolean') return v ? '是' : '否'
  if (typeof v === 'number') return Number.isFinite(v) ? String(v) : EMPTY
  if (Array.isArray(v)) return v.map((x) => displayValue(x)).join('、')
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}
