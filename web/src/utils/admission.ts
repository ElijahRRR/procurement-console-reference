/**
 * 准入规则（契约 §6 admission）：飞书连接与文件导入共用的取值项、缺省值与说明。
 * 缺省值与 services/normalize.DEFAULT_ADMISSION、FILE_ADMISSION_DEFAULTS 一致（tests/unit/admission.test.ts 核对）。
 */
import type { FeishuAdmission } from '@/api/types'

export type AdmissionListKey = Exclude<keyof FeishuAdmission, 'import_historical'>

export const ADMISSION_LIST_KEYS: readonly AdmissionListKey[] = [
  'approved_values',
  'rejected_values',
  'purchaser_values',
  'skip_purchase_status_values',
  'cancel_after_sale_values',
]

/** 飞书连接的缺省准入规则 */
export const FEISHU_ADMISSION_DEFAULTS: FeishuAdmission = {
  approved_values: ['通过'],
  rejected_values: ['拒绝', '建议拒绝', '钓鱼'],
  purchaser_values: [],
  skip_purchase_status_values: ['已拍单', '拿回'],
  cancel_after_sale_values: [],
  import_historical: false,
}

/** 文件导入的缺省准入规则：带 AMZ 订单号的行按历史采购登记；售后状态「已取消」= 上游取消（文件靠更新后的文件表达取消） */
export const FILE_ADMISSION_DEFAULTS: FeishuAdmission = {
  ...FEISHU_ADMISSION_DEFAULTS,
  cancel_after_sale_values: ['已取消'],
  import_historical: true,
}

export interface AdmissionField {
  key: AdmissionListKey
  label: string
  hint: string
}

/** 取值项的名称与说明（「默认……」由 defaultsHint 按来源补上） */
export const ADMISSION_FIELDS: readonly AdmissionField[] = [
  { key: 'approved_values', label: '人审通过的取值', hint: '「人工审核」列为这些值时准入。' },
  { key: 'rejected_values', label: '人审拒绝的取值', hint: '为这些值时不下发采购。' },
  { key: 'purchaser_values', label: '只接这些采购方', hint: '「采购方」列为这些值的行才进入本系统；留空表示不过滤。' },
  { key: 'skip_purchase_status_values', label: '不新建采购的采购状态', hint: '「采购状态」为这些值的行只作为历史或更新。' },
  { key: 'cancel_after_sale_values', label: '视为上游取消的售后状态', hint: '「售后状态」为这些值时按上游取消处理。' },
]

/** 输入：缺省取值 → 输出：「默认「通过」。」/「默认不设置。」 */
export function defaultsHint(values: readonly string[]): string {
  return values.length ? `默认${values.map((v) => `「${v}」`).join('')}。` : '默认不设置。'
}

/** 输入：可能缺项的规则、缺省规则 → 输出：补齐后的完整规则（列表复制一份，便于表单直接编辑） */
export function fullAdmission(a: Partial<FeishuAdmission> | null | undefined, defaults: FeishuAdmission): FeishuAdmission {
  const out = { import_historical: typeof a?.import_historical === 'boolean' ? a.import_historical : defaults.import_historical } as FeishuAdmission
  for (const k of ADMISSION_LIST_KEYS) {
    const v = a?.[k]
    out[k] = Array.isArray(v) ? v.map((x) => String(x).trim()).filter(Boolean) : [...defaults[k]]
  }
  return out
}

/** 输入：两份完整规则 → 输出：是否相同（列表按顺序比较） */
export function admissionEquals(a: FeishuAdmission, b: FeishuAdmission): boolean {
  if (a.import_historical !== b.import_historical) return false
  return ADMISSION_LIST_KEYS.every((k) => a[k].length === b[k].length && a[k].every((v, i) => v === b[k][i]))
}
