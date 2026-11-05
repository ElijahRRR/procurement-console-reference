/**
 * 控制台 HTTP 客户端：fetch + 同源 Cookie 会话。
 *
 * - 前缀 /api/v1；写请求带 X-Requested-With: pc-console（契约 §8 防跨站）。
 * - 错误统一转成 ApiError（后端 JSON 为 {code, message, detail}）。
 * - 401 交给 onUnauthorized（路由层设为跳转登录页）；needs_setup 交给 onNeedsSetup（跳转首次设置）。
 */
import type { Page } from './types'

export const API_BASE = '/api/v1'

export class ApiError extends Error {
  readonly code: string
  readonly status: number
  readonly detail: Record<string, unknown>

  constructor(code: string, message: string, status: number, detail?: Record<string, unknown> | null) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.detail = detail ?? {}
  }
}

export type QueryValue = string | number | boolean | null | undefined
export type Query = Record<string, QueryValue | QueryValue[]>

export interface RequestOptions {
  query?: Query
  /** JSON 请求体（GET 不允许） */
  body?: unknown
  /** multipart 请求体（文件上传） */
  form?: FormData
  signal?: AbortSignal
  /** 为 true 时 401 不触发跳转（用于启动时探测登录状态） */
  silent?: boolean
}

interface Handlers {
  onUnauthorized: (err: ApiError) => void
  onNeedsSetup: (err: ApiError) => void
}

const handlers: Handlers = {
  onUnauthorized: () => {},
  onNeedsSetup: () => {},
}

/** 输入：401 / 需要首次设置时的处理函数 → 输出：无（替换全局处理函数，路由初始化时调用一次） */
export function setAuthHandlers(next: Partial<Handlers>): void {
  Object.assign(handlers, next)
}

/** 输入：路径与查询参数 → 输出：带前缀的 URL；空值（null、undefined、空字符串）不进查询串 */
export function buildUrl(path: string, query?: Query): string {
  const base = path.startsWith('/api/') ? path : `${API_BASE}${path.startsWith('/') ? '' : '/'}${path}`
  if (!query) return base
  const params = new URLSearchParams()
  for (const [key, raw] of Object.entries(query)) {
    const values = Array.isArray(raw) ? raw : [raw]
    for (const v of values) {
      if (v === null || v === undefined || v === '') continue
      params.append(key, String(v))
    }
  }
  const qs = params.toString()
  return qs ? `${base}?${qs}` : base
}

/** 输入：任意值 → 输出：是否像后端的错误体 {code, message} */
function isErrorBody(x: unknown): x is { code: string; message?: string; detail?: Record<string, unknown> | null } {
  return !!x && typeof x === 'object' && typeof (x as { code?: unknown }).code === 'string'
}

/** 输入：HTTP 状态码 → 输出：无错误体时的兜底中文说明 */
export function fallbackMessage(status: number): string {
  if (status === 401) return '登录已失效，请重新登录'
  if (status === 403) return '没有权限执行此操作'
  if (status === 404) return '请求的记录不存在'
  if (status === 409) return '操作与当前状态冲突，请刷新后重试'
  if (status === 413) return '文件太大，请控制在 10 MB 以内'
  if (status === 422) return '提交的内容不符合要求'
  if (status >= 500) return `服务器出错（HTTP ${status}），请稍后重试`
  return `请求失败（HTTP ${status}）`
}

/** 输入：fetch 的 Response（非 2xx）→ 输出：ApiError（解析 {code, message, detail}，解析不了则按状态码兜底） */
export async function toApiError(res: Response): Promise<ApiError> {
  let body: unknown = null
  try {
    const text = await res.text()
    body = text ? JSON.parse(text) : null
  } catch {
    body = null
  }
  // FastAPI 默认的 {detail: {...}} 包裹也认
  if (!isErrorBody(body) && body && typeof body === 'object' && isErrorBody((body as { detail?: unknown }).detail)) {
    body = (body as { detail: unknown }).detail
  }
  if (isErrorBody(body)) {
    const detail = body.detail && typeof body.detail === 'object' ? body.detail : {}
    return new ApiError(body.code, body.message || fallbackMessage(res.status), res.status, detail)
  }
  const code = res.status === 401 ? 'UNAUTHORIZED' : res.status === 404 ? 'NOT_FOUND' : `HTTP_${res.status}`
  return new ApiError(code, fallbackMessage(res.status), res.status)
}

/** 输入：ApiError → 输出：是否表示「尚未完成首次设置」 */
export function isNeedsSetup(err: ApiError): boolean {
  return (
    err.code === 'NEEDS_SETUP' ||
    err.code === 'needs_setup' ||
    err.detail?.needs_setup === true ||
    err.detail?.reason === 'needs_setup'
  )
}

/** 输入：方法、路径、选项 → 输出：解析后的 JSON（204 / 空体为 null）；失败抛 ApiError */
export async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
  const upper = method.toUpperCase()
  const headers: Record<string, string> = { Accept: 'application/json' }
  let body: BodyInit | undefined
  if (upper !== 'GET' && upper !== 'HEAD') {
    headers['X-Requested-With'] = 'pc-console'
    if (opts.form) {
      body = opts.form
    } else if (opts.body !== undefined) {
      headers['Content-Type'] = 'application/json'
      body = JSON.stringify(opts.body)
    }
  }
  let res: Response
  try {
    res = await fetch(buildUrl(path, opts.query), {
      method: upper,
      headers,
      body,
      credentials: 'same-origin',
      signal: opts.signal,
    })
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e
    throw new ApiError('NETWORK', '无法连接服务器，请检查服务端是否在运行', 0)
  }
  if (!res.ok) {
    const err = await toApiError(res)
    if (isNeedsSetup(err)) handlers.onNeedsSetup(err)
    else if (res.status === 401 && !opts.silent) handlers.onUnauthorized(err)
    throw err
  }
  if (res.status === 204) return null as T
  const text = await res.text()
  if (!text) return null as T
  try {
    return JSON.parse(text) as T
  } catch {
    throw new ApiError('BAD_RESPONSE', '服务器返回的内容无法解析', res.status)
  }
}

export const get = <T>(path: string, query?: Query, opts: Omit<RequestOptions, 'query' | 'body'> = {}) =>
  request<T>('GET', path, { ...opts, query })
export const post = <T>(path: string, body?: unknown, opts: Omit<RequestOptions, 'body'> = {}) =>
  request<T>('POST', path, { ...opts, body: body ?? {} })
export const patch = <T>(path: string, body: unknown, opts: Omit<RequestOptions, 'body'> = {}) =>
  request<T>('PATCH', path, { ...opts, body })
export const del = <T>(path: string, opts: RequestOptions = {}) => request<T>('DELETE', path, opts)

/** 输入：列表接口的返回（{total, rows} 或裸数组）→ 输出：{total, rows} */
export function asPage<T>(x: Page<T> | T[] | null | undefined): Page<T> {
  if (!x) return { total: 0, rows: [] }
  if (Array.isArray(x)) return { total: x.length, rows: x }
  return { total: typeof x.total === 'number' ? x.total : (x.rows?.length ?? 0), rows: x.rows ?? [] }
}

/** 输入：下载接口路径与查询参数 → 输出：无（用隐藏链接触发浏览器下载，Cookie 同源随带） */
export function download(path: string, query?: Query): void {
  const a = document.createElement('a')
  a.href = buildUrl(path, query)
  a.rel = 'noopener'
  a.download = ''
  document.body.appendChild(a)
  a.click()
  a.remove()
}
