import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, asPage, buildUrl, get, post, request, setAuthHandlers, toApiError, isNeedsSetup } from '@/api/client'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(body === undefined ? '' : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('URL 拼接', () => {
  it('加前缀，跳过空值，数组展开', () => {
    expect(buildUrl('/orders')).toBe('/api/v1/orders')
    expect(buildUrl('/orders', { view: 'today', q: '', account_id: null, source: undefined, page: 1 })).toBe('/api/v1/orders?view=today&page=1')
    expect(buildUrl('/orders', { ids: [1, 2] })).toBe('/api/v1/orders?ids=1&ids=2')
    expect(buildUrl('orders', { q: 'B0 DEMO' })).toBe('/api/v1/orders?q=B0+DEMO')
  })
})

describe('错误映射', () => {
  it('后端 {code, message, detail} 原样转成 ApiError', async () => {
    const err = await toApiError(jsonResponse(409, { code: 'ACCOUNT_BUSY', message: '账号正在别处执行', detail: { holder_device_name: 'Mac-DEMO' } }))
    expect(err).toBeInstanceOf(ApiError)
    expect(err.code).toBe('ACCOUNT_BUSY')
    expect(err.status).toBe(409)
    expect(err.message).toBe('账号正在别处执行')
    expect(err.detail).toEqual({ holder_device_name: 'Mac-DEMO' })
  })
  it('FastAPI 的 {detail: {code, message}} 包裹也认', async () => {
    const err = await toApiError(jsonResponse(422, { detail: { code: 'VALIDATION', message: '金额格式不对' } }))
    expect(err.code).toBe('VALIDATION')
    expect(err.message).toBe('金额格式不对')
  })
  it('没有错误体时按状态码兜底', async () => {
    const e500 = await toApiError(new Response('<html>oops</html>', { status: 500 }))
    expect(e500.code).toBe('HTTP_500')
    expect(e500.message).toContain('服务器出错')
    const e401 = await toApiError(new Response('', { status: 401 }))
    expect(e401.code).toBe('UNAUTHORIZED')
    const e404 = await toApiError(new Response('', { status: 404 }))
    expect(e404.code).toBe('NOT_FOUND')
  })
  it('message 为空时用兜底文案', async () => {
    const err = await toApiError(jsonResponse(403, { code: 'FORBIDDEN', message: '' }))
    expect(err.message).toBe('没有权限执行此操作')
  })
  it('识别 needs_setup', () => {
    expect(isNeedsSetup(new ApiError('NEEDS_SETUP', 'x', 409))).toBe(true)
    expect(isNeedsSetup(new ApiError('UNAUTHORIZED', 'x', 401, { needs_setup: true }))).toBe(true)
    expect(isNeedsSetup(new ApiError('UNAUTHORIZED', 'x', 401))).toBe(false)
  })
})

describe('request', () => {
  const fetchMock = vi.fn()
  const onUnauthorized = vi.fn()
  const onNeedsSetup = vi.fn()

  beforeEach(() => {
    fetchMock.mockReset()
    onUnauthorized.mockReset()
    onNeedsSetup.mockReset()
    vi.stubGlobal('fetch', fetchMock)
    setAuthHandlers({ onUnauthorized, onNeedsSetup })
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('GET 不带防跨站头，同源带 Cookie', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { total: 0, rows: [] }))
    await get('/orders', { view: 'today' })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/orders?view=today')
    expect(init.method).toBe('GET')
    expect(init.credentials).toBe('same-origin')
    expect(init.headers['X-Requested-With']).toBeUndefined()
    expect(init.body).toBeUndefined()
  })

  it('写请求带 X-Requested-With: pc-console 与 JSON 体', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { ok: true }))
    const res = await post<{ ok: boolean }>('/orders/1/assign', { account_id: 3 })
    const [, init] = fetchMock.mock.calls[0]
    expect(init.method).toBe('POST')
    expect(init.headers['X-Requested-With']).toBe('pc-console')
    expect(init.headers['Content-Type']).toBe('application/json')
    expect(JSON.parse(init.body)).toEqual({ account_id: 3 })
    expect(res).toEqual({ ok: true })
  })

  it('multipart 上传不设 Content-Type（由浏览器带 boundary）', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { batch_id: 1 }))
    const form = new FormData()
    form.append('file', new Blob(['a,b']), 'demo.csv')
    await request('POST', '/imports', { form })
    const [, init] = fetchMock.mock.calls[0]
    expect(init.headers['Content-Type']).toBeUndefined()
    expect(init.headers['X-Requested-With']).toBe('pc-console')
    expect(init.body).toBe(form)
  })

  it('204 与空体返回 null', async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }))
    expect(await post('/auth/logout')).toBeNull()
  })

  it('401 抛 ApiError 并触发跳转登录', async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { code: 'UNAUTHORIZED', message: '请先登录' }))
    await expect(get('/overview')).rejects.toMatchObject({ code: 'UNAUTHORIZED', status: 401 })
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
    expect(onNeedsSetup).not.toHaveBeenCalled()
  })

  it('silent 请求的 401 不跳转', async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { code: 'UNAUTHORIZED', message: '请先登录' }))
    await expect(get('/auth/me', undefined, { silent: true })).rejects.toBeInstanceOf(ApiError)
    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('needs_setup 触发跳转首次设置', async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { code: 'UNAUTHORIZED', message: '尚未设置', detail: { needs_setup: true } }))
    await expect(get('/overview')).rejects.toBeInstanceOf(ApiError)
    expect(onNeedsSetup).toHaveBeenCalledTimes(1)
    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('业务错误（409 等）不触发跳转', async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, { code: 'INVALID_TRANSITION', message: '当前状态不允许改派', detail: {} }))
    await expect(post('/orders/1/assign', { account_id: 2 })).rejects.toMatchObject({ code: 'INVALID_TRANSITION', status: 409 })
    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('网络错误映射成 NETWORK', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    await expect(get('/overview')).rejects.toMatchObject({ code: 'NETWORK', status: 0 })
  })

  it('返回内容不是 JSON 时报 BAD_RESPONSE', async () => {
    fetchMock.mockResolvedValue(new Response('<html></html>', { status: 200 }))
    await expect(get('/overview')).rejects.toMatchObject({ code: 'BAD_RESPONSE' })
  })
})

describe('asPage', () => {
  it('兼容裸数组与缺字段', () => {
    expect(asPage([1, 2])).toEqual({ total: 2, rows: [1, 2] })
    expect(asPage(null)).toEqual({ total: 0, rows: [] })
    expect(asPage({ total: 9, rows: [1] })).toEqual({ total: 9, rows: [1] })
  })
})
