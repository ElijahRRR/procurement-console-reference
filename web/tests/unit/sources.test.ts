/** 订单来源的纯函数：飞书字段对应（多列回退）、文件导入向导（预览过期、模板、继续批次）、准入规则缺省值。 */
import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import type { SystemField } from '@/api/types'
import { fillSuggested, fromColumns, missingRequired, toColumns, toEditableMap, toFieldMap } from '@/utils/feishuMap'
import {
  applyTemplate,
  canResume,
  findTemplateByName,
  mappingSignature,
  previousUploadText,
  resumeFromBatch,
  templateOverwriteMessage,
} from '@/utils/importWizard'
import { ADMISSION_LIST_KEYS, FEISHU_ADMISSION_DEFAULTS, FILE_ADMISSION_DEFAULTS, admissionEquals, defaultsHint, fullAdmission } from '@/utils/admission'

describe('飞书字段对应：一项可以对应多列（按顺序回退）', () => {
  it('服务端的列表值原样变成有序多选，不显示成「不对应」', () => {
    const m = toEditableMap({ upstream_order_no: '平台订单号', asin: ['ASIN', '来源码'], quantity: ['数量', '数量1'], address1: ['收货人地址', '地址1'], sku: '' })
    expect(m).toEqual({ upstream_order_no: ['平台订单号'], asin: ['ASIN', '来源码'], quantity: ['数量', '数量1'], address1: ['收货人地址', '地址1'] })
  })
  it('保存时一列写成字符串、多列保留列表与顺序、空列表去掉', () => {
    expect(toFieldMap({ upstream_order_no: ['平台订单号'], asin: ['来源码', 'ASIN'], sku: [], phone: undefined })).toEqual({
      upstream_order_no: '平台订单号',
      asin: ['来源码', 'ASIN'],
    })
    expect(fromColumns([' ASIN ', 'ASIN', ''])).toBe('ASIN')
    expect(fromColumns([])).toBeUndefined()
    expect(toColumns(null)).toEqual([])
    expect(toColumns(['a', 3, 'b'])).toEqual(['a', 'b'])
  })
  it('补建议时只补空缺，已选的（含回退列）不动', () => {
    const m: Record<string, string[]> = { asin: ['ASIN'], quantity: [] }
    fillSuggested(m, { asin: ['ASIN', '来源码'], quantity: ['数量', '数量1'], upstream_order_no: '平台订单号' })
    expect(m).toEqual({ asin: ['ASIN'], quantity: ['数量', '数量1'], upstream_order_no: ['平台订单号'] })
  })
  it('必填检查：空列表算没对应', () => {
    const fields: SystemField[] = [
      { key: 'upstream_order_no', label: '平台订单号', required: true },
      { key: 'asin', label: 'ASIN', required: false },
    ]
    expect(missingRequired(fields, { upstream_order_no: [] })).toEqual(['平台订单号'])
    expect(missingRequired(fields, {})).toEqual(['平台订单号'])
    expect(missingRequired(fields, { upstream_order_no: ['平台订单号'] })).toEqual([])
  })
})

describe('文件导入向导', () => {
  it('预览之后改字段对应、换工作表都会让签名变化（预览过期，不能提交）', () => {
    const sig = mappingSignature('采购订单', { 平台订单号: 'upstream_order_no', 限价: 'price_cap', 备注: null })
    expect(mappingSignature('采购订单', { 备注: null, 限价: 'price_cap', 平台订单号: 'upstream_order_no' })).toBe(sig)
    expect(mappingSignature('采购订单', { 平台订单号: 'upstream_order_no', 限价: null, 备注: null })).not.toBe(sig)
    expect(mappingSignature('采购订单', { 平台订单号: 'upstream_order_no', 限价: 'unit_price', 备注: null })).not.toBe(sig)
    expect(mappingSignature('Sheet2', { 平台订单号: 'upstream_order_no', 限价: 'price_cap', 备注: null })).not.toBe(sig)
    // 空串与 null 都是「不导入」
    expect(mappingSignature('采购订单', { 平台订单号: 'upstream_order_no', 限价: 'price_cap', 备注: '' })).toBe(sig)
  })
  it('套用模板：模板里有的表头用模板的对应，其余不变', () => {
    expect(applyTemplate(['平台订单号', '限价', '备注'], { 平台订单号: 'upstream_order_no', 限价: null, 备注: null }, { mapping: { 限价: 'price_cap', 不存在的列: 'sku' } })).toEqual({
      平台订单号: 'upstream_order_no',
      限价: 'price_cap',
      备注: null,
    })
  })
  it('只有已上传、已预览的批次可以继续', () => {
    expect(canResume({ status: 'uploaded' })).toBe(true)
    expect(canResume({ status: 'previewed' })).toBe(true)
    expect(canResume({ status: 'committed' })).toBe(false)
    expect(canResume({ status: 'discarded' })).toBe(false)
  })
  it('读不到原文件（headers、sheets 为空）时退回批次的工作表与 mapping 的键', () => {
    expect(
      resumeFromBatch({ sheet_name: '采购订单', mapping: { 平台订单号: 'upstream_order_no', 备注: null, ASIN: '' }, sheets: [], headers: [], stats: { total: 12 } }),
    ).toEqual({
      sheet: '采购订单',
      headers: ['平台订单号', '备注', 'ASIN'],
      mapping: { 平台订单号: 'upstream_order_no', 备注: null, ASIN: null },
      sheets: [{ name: '采购订单', row_count: 12, headers: ['平台订单号', '备注', 'ASIN'] }],
    })
    // 旧服务端没有 headers / sheets 字段
    expect(resumeFromBatch({ sheet_name: 'S', mapping: { a: 'asin' } })).toEqual({
      sheet: 'S', headers: ['a'], mapping: { a: 'asin' }, sheets: [{ name: 'S', row_count: null, headers: ['a'] }],
    })
    expect(resumeFromBatch({ sheet_name: null, mapping: { a: null } })).toBeNull()
    expect(resumeFromBatch({ sheet_name: 'S', mapping: null })).toBeNull()
    expect(resumeFromBatch({ sheet_name: 'S', mapping: {} })).toBeNull()
  })
  it('表头按原文件顺序（GET /imports/{id} 的 headers），工作表列表用 sheets', () => {
    const state = resumeFromBatch({
      sheet_name: '采购订单',
      // jsonb 打乱了键的顺序，还带着一个原文件里没有的键
      mapping: { 限价: 'price_cap', 平台订单号: 'upstream_order_no', 旧列: 'asin' },
      headers: ['平台订单号', 'ASIN', '限价'],
      sheets: [
        { name: '说明', row_count: 3, headers: ['说明'] },
        { name: '采购订单', row_count: 28, headers: ['平台订单号', 'ASIN', '限价'] },
      ],
    })
    expect(state).toEqual({
      sheet: '采购订单',
      headers: ['平台订单号', 'ASIN', '限价'],
      mapping: { 平台订单号: 'upstream_order_no', ASIN: null, 限价: 'price_cap' },
      sheets: [
        { name: '说明', row_count: 3, headers: ['说明'] },
        { name: '采购订单', row_count: 28, headers: ['平台订单号', 'ASIN', '限价'] },
      ],
    })
    // 批次没记下工作表时取第一个有表头的工作表，对应全部留空
    expect(resumeFromBatch({ sheet_name: null, mapping: null, headers: [], sheets: [{ name: '空表', row_count: 0, headers: [] }, { name: 'S2', row_count: 2, headers: ['x'] }] })).toEqual({
      sheet: 'S2', headers: ['x'], mapping: { x: null }, sheets: [{ name: '空表', row_count: 0, headers: [] }, { name: 'S2', row_count: 2, headers: ['x'] }],
    })
  })
  it('重复上传提醒写明批次、状态与时间', () => {
    const t = previousUploadText({ id: 41, status: 'committed', created_at: '2026-11-05T02:20:00+00:00' })
    expect(t).toContain('2026-11-05 10:20')
    expect(t).toContain('批次 41，已提交')
    expect(t).toContain('按平台订单号去重')
  })
  it('模板工作区共用：保存的名称与已有模板相同（去掉首尾空白后完全一致）时找出被覆盖的那个', () => {
    const list = [
      { id: 1, name: '小蜜蜂导出', created_by_name: 'DEMO 运营' },
      { id: 2, name: 'DEMO 店铺导出', created_by_name: null },
    ]
    expect(findTemplateByName(list, '小蜜蜂导出')?.id).toBe(1)
    expect(findTemplateByName(list, '  DEMO 店铺导出 ')?.id).toBe(2)
    expect(findTemplateByName(list, 'demo 店铺导出')).toBeUndefined()
    expect(findTemplateByName(list, '新格式')).toBeUndefined()
    expect(findTemplateByName([], '小蜜蜂导出')).toBeUndefined()
  })
  it('覆盖确认写明模板名、创建人与对整个工作区生效；不知道创建人时不提', () => {
    const t = templateOverwriteMessage({ name: '小蜜蜂导出', created_by_name: 'DEMO 运营' })
    expect(t.startsWith('确定要覆盖模板「小蜜蜂导出」吗？')).toBe(true)
    expect(t).toContain('它由 DEMO 运营 创建')
    expect(t).toContain('工作区')
    const anon = templateOverwriteMessage({ name: '小蜜蜂导出', created_by_name: null })
    expect(anon).not.toContain('创建')
    expect(anon).toContain('工作区')
  })
})

describe('准入规则', () => {
  it('文件缺省：售后状态「已取消」视为上游取消，导入历史采购', () => {
    expect(FILE_ADMISSION_DEFAULTS.cancel_after_sale_values).toEqual(['已取消'])
    expect(FILE_ADMISSION_DEFAULTS.import_historical).toBe(true)
    expect(FEISHU_ADMISSION_DEFAULTS.cancel_after_sale_values).toEqual([])
    expect(FEISHU_ADMISSION_DEFAULTS.import_historical).toBe(false)
  })
  it('补齐缺项、去空白；空列表保留为空', () => {
    const a = fullAdmission({ approved_values: [' 通过 ', ''], cancel_after_sale_values: [] }, FILE_ADMISSION_DEFAULTS)
    expect(a.approved_values).toEqual(['通过'])
    expect(a.cancel_after_sale_values).toEqual([])
    expect(a.rejected_values).toEqual(FILE_ADMISSION_DEFAULTS.rejected_values)
    expect(a.import_historical).toBe(true)
    expect(admissionEquals(fullAdmission(null, FILE_ADMISSION_DEFAULTS), FILE_ADMISSION_DEFAULTS)).toBe(true)
    expect(admissionEquals(a, FILE_ADMISSION_DEFAULTS)).toBe(false)
    // 复制一份：改表单不会改到缺省值
    fullAdmission(null, FILE_ADMISSION_DEFAULTS).approved_values.push('x')
    expect(FILE_ADMISSION_DEFAULTS.approved_values).toEqual(['通过'])
  })
  it('缺省说明', () => {
    expect(defaultsHint(['已拍单', '拿回'])).toBe('默认「已拍单」「拿回」。')
    expect(defaultsHint([])).toBe('默认不设置。')
  })

  const normalizePath = resolve(process.cwd(), '..', 'backend/services/normalize.py')
  it.skipIf(!existsSync(normalizePath))('缺省值与 services/normalize.DEFAULT_ADMISSION、FILE_ADMISSION_DEFAULTS 一致', () => {
    const src = readFileSync(normalizePath, 'utf8')
    const body = /DEFAULT_ADMISSION[^=]*=\s*\{([\s\S]*?)\n\}/.exec(src)?.[1] ?? ''
    const list = (key: string, text: string) => {
      const m = new RegExp(`"${key}":\\s*\\[([^\\]]*)\\]`).exec(text)
      if (!m) throw new Error(`没有 ${key}`)
      return [...m[1].matchAll(/"([^"]*)"/g)].map((x) => x[1])
    }
    for (const k of ADMISSION_LIST_KEYS) expect(FEISHU_ADMISSION_DEFAULTS[k]).toEqual(list(k, body))
    expect(/"import_historical":\s*False/.test(body)).toBe(true)
    const file = /FILE_ADMISSION_DEFAULTS[^=]*=\s*(\{[^\n]*\})/.exec(src)?.[1] ?? ''
    expect(list('cancel_after_sale_values', file)).toEqual(FILE_ADMISSION_DEFAULTS.cancel_after_sale_values)
    expect(/"import_historical":\s*True/.test(file)).toBe(true)
  })
})
