/** 飞书接入：状态、链接解析、字段、预览、连接管理、同步与写回验收（契约 §6、§8、§10）。 */
import { asPage, get, patch, post } from './client'
import type {
  FeishuConnection,
  FeishuConnectionInput,
  FeishuFieldsResult,
  FeishuPreview,
  FeishuPreviewInput,
  FeishuStatus,
  FeishuSyncResult,
  Page,
  PageQuery,
  ResolvedLink,
  SyncRun,
  SystemFields,
  VerifyWritebackResult,
} from './types'

/** 输入：无 → 输出：{configured}（服务端是否配置了 FEISHU_APP_ID / FEISHU_APP_SECRET） */
export const getFeishuStatus = () => get<FeishuStatus>('/feishu/status')

/** 输入：无 → 输出：字段对应下拉用的系统字段清单（来源字段、结果字段） */
export const getSystemFields = () => get<SystemFields>('/feishu/system-fields')

/** 输入：多维表格链接 → 输出：app_token、链接里的表与视图、全部数据表 */
export const resolveLink = (link: string) => post<ResolvedLink>('/feishu/resolve-link', { link })

/** 输入：app_token、table_id → 输出：字段与类型、建议的字段对应与写回对应 */
export const getFields = (appToken: string, tableId: string) =>
  post<FeishuFieldsResult>('/feishu/fields', { app_token: appToken, table_id: tableId })

/** 输入：表定位、字段对应、准入规则、样本数 → 输出：样本预览（只读） */
export const previewFeishu = (body: FeishuPreviewInput) => post<FeishuPreview>('/feishu/preview', body)

/** 输入：分页 → 输出：{total, rows} */
export const listConnections = async (query: PageQuery = { page: 1, page_size: 100 }): Promise<Page<FeishuConnection>> =>
  asPage(await get<Page<FeishuConnection>>('/feishu/connections', { ...query }))

/** 输入：连接 id → 输出：连接 */
export const getConnection = (id: number) => get<FeishuConnection>(`/feishu/connections/${id}`)

/** 输入：连接配置 → 输出：新连接 */
export const createConnection = (body: FeishuConnectionInput) => post<FeishuConnection>('/feishu/connections', body)

/** 输入：连接 id、要改的字段（enabled=false 即停用）→ 输出：更新后的连接 */
export const updateConnection = (id: number, body: FeishuConnectionInput) =>
  patch<FeishuConnection>(`/feishu/connections/${id}`, body)

/** 输入：连接 id → 输出：本次读取的统计 {read, new, update, …, run_id, problems, allocation}；读飞书失败抛 ApiError */
export const syncConnection = (id: number) => post<FeishuSyncResult>(`/feishu/connections/${id}/sync`, {})

/**
 * 输入：连接 id、测试记录 id → 输出：{ok, mismatches, written, restored, restore_error, error, connection}
 * （ok 为 true 才算通过并记 writeback_verified_at；写入 / 读回时飞书报错不抛异常，见 error）
 */
export const verifyWriteback = (id: number, recordId: string) =>
  post<VerifyWritebackResult>(`/feishu/connections/${id}/verify-writeback`, { record_id: recordId })

/** 输入：连接 id、分页 → 输出：读取记录 {total, rows} */
export const listSyncRuns = async (id: number, query: PageQuery = { page: 1, page_size: 20 }): Promise<Page<SyncRun>> =>
  asPage(await get<Page<SyncRun>>(`/feishu/connections/${id}/runs`, { ...query }))
