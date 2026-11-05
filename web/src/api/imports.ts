/** 文件导入、个人模板与结果导出（契约 §7、§8、§10）。 */
import { asPage, del, download, get, post, request } from './client'
import type {
  ImportBatch,
  ImportCommitResult,
  ImportPreview,
  ImportRow,
  ImportTemplate,
  ImportVerdict,
  OrderView,
  Page,
  PageQuery,
  UploadResult,
} from './types'

/** 输入：.xlsx / .csv 文件 → 输出：批次 id、工作表、表头、建议对应、系统字段、个人模板 */
export function uploadImport(file: File): Promise<UploadResult> {
  const form = new FormData()
  form.append('file', file)
  return request<UploadResult>('POST', '/imports', { form })
}

/** 输入：批次 id、工作表、{表头: 系统字段} → 输出：预览统计与第一页行（准入规则用工作区设置里的文件准入规则） */
export const previewImport = (id: number, sheet: string, mapping: Record<string, string | null>) =>
  post<ImportPreview>(`/imports/${id}/preview`, { sheet, mapping })

/** 输入：批次 id、结论筛选、分页 → 输出：批次行 {total, rows} */
export const listImportRows = async (
  id: number,
  query: PageQuery & { verdict?: ImportVerdict | '' } = {},
): Promise<Page<ImportRow>> => asPage(await get<Page<ImportRow>>(`/imports/${id}/rows`, { ...query }))

/** 输入：批次 id → 输出：批次（含最近一次的字段对应），用于继续未完成的导入 */
export const getImport = (id: number) => get<ImportBatch>(`/imports/${id}`)

/** 输入：批次 id → 输出：{batch, stats, allocation}（逐行进入任务流，随后分配一次） */
export const commitImport = (id: number) => post<ImportCommitResult>(`/imports/${id}/commit`, {})

/** 输入：批次 id → 输出：放弃后的批次 */
export const discardImport = (id: number) => post<ImportBatch>(`/imports/${id}/discard`, {})

/** 输入：分页 → 输出：批次列表 {total, rows} */
export const listImports = async (query: PageQuery = { page: 1, page_size: 20 }): Promise<Page<ImportBatch>> =>
  asPage(await get<Page<ImportBatch>>('/imports', { ...query }))

/** 输入：无 → 输出：保存的文件名（下载标准模板）；失败抛 ApiError */
export const downloadTemplate = () => download('/imports/template.xlsx', undefined, '采购导入模板.xlsx')

/** 输入：批次 id → 输出：保存的文件名（下载问题行：原列 + 结论 + 原因）；失败抛 ApiError */
export const downloadProblems = (id: number) => download(`/imports/${id}/problems.xlsx`, undefined, `问题行-批次${id}.xlsx`)

/** 输入：批次 id 或视图 → 输出：保存的文件名（下载结果 xlsx）；失败抛 ApiError */
export const downloadOrdersExport = (query: { batch_id?: number; view?: OrderView }) =>
  download('/exports/orders.xlsx', { ...query }, '采购结果.xlsx')

/** 输入：无 → 输出：个人模板 {total, rows} */
export const listTemplates = async (): Promise<Page<ImportTemplate>> =>
  asPage(await get<Page<ImportTemplate>>('/import-templates'))

/** 输入：模板名、{表头: 系统字段} → 输出：新模板 */
export const createTemplate = (name: string, mapping: Record<string, string | null>) =>
  post<ImportTemplate>('/import-templates', { name, mapping })

/** 输入：模板 id → 输出：无 */
export const deleteTemplate = (id: number) => del<null>(`/import-templates/${id}`)
