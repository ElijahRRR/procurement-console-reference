import { describe, expect, it } from 'vitest'
import type { AllocationSummary, FeishuConnection, FeishuSyncResult, VerifyWritebackResult } from '@/api/types'
import {
  allocationBrief,
  bulkReviewSummary,
  commitSummary,
  problemLine,
  problemLines,
  resetSummary,
  statsText,
  syncSummary,
  unsampledProblems,
  verifySummary,
} from '@/utils/results'

const alloc: AllocationSummary = {
  id: 3,
  bj_date: '2026-11-05',
  at: '2026-11-05T02:00:00+00:00',
  trigger: 'intake',
  stats: { considered: 5, assigned: 4, coverage: 2, fill: 2, unassigned: 1 },
  uncovered: [],
  unassigned: [],
}

describe('统计与分配文案', () => {
  it('只列大于 0 的项，读取数总是列出', () => {
    expect(statsText({ read: 0, new: 0, update: 2, conflict: 1 })).toBe('读取 0 · 更新 2 · 待核实 1')
    expect(statsText(null)).toBe('')
  })
  it('分配摘要', () => {
    expect(allocationBrief(alloc)).toBe('随后分配：分出 4 笔，未分出 1 笔')
    expect(allocationBrief(null)).toBe('')
  })
})

/** 连接级问题的原文（services/feishu_intake.buyer_columns_problem） */
const MISSING = '来源表里找不到买家号列「买家号id」，这一列的指定保持上次读到的内容，新记录暂不接收'

describe('问题记录的文字（立即同步的提示、读取记录、连接卡片共用）', () => {
  it('记录级：「平台订单号：结论，原因」；没有平台订单号写「记录 recX」；没有原因只写结论', () => {
    expect(problemLine({ record_id: 'recA', upstream_order_no: 'PO-DEMO-1', verdict: 'skipped', reason: '采购方不是本系统接的采购方' })).toBe('PO-DEMO-1：跳过，采购方不是本系统接的采购方')
    expect(problemLine({ record_id: 'recB', upstream_order_no: null, verdict: 'invalid', reason: '缺少平台订单号' })).toBe('记录 recB：无效，缺少平台订单号')
    expect(problemLine({ record_id: 'recC', upstream_order_no: 'PO-DEMO-3', verdict: 'conflict', reason: null })).toBe('PO-DEMO-3：待核实')
  })
  it('连接级与记录级分开：连接级只写原因；不是列表、缺项的旧记录不出错', () => {
    const lines = problemLines([
      { record_id: null, upstream_order_no: '买家号列', verdict: 'skipped', reason: MISSING, scope: 'connection' },
      { record_id: 'recA', upstream_order_no: 'PO-DEMO-1', verdict: 'skipped', reason: '来源表里找不到买家号列「买家号id」，读不出这一行有没有指定买家号，暂不接收；恢复这一列或改字段对应后会自动接收' },
    ])
    expect(lines.whole).toEqual([MISSING])
    expect(lines.records).toEqual(['PO-DEMO-1：跳过，来源表里找不到买家号列「买家号id」，读不出这一行有没有指定买家号，暂不接收；恢复这一列或改字段对应后会自动接收'])
    expect(problemLines(undefined)).toEqual({ whole: [], records: [] })
    expect(problemLines('x')).toEqual({ whole: [], records: [] })
    expect(problemLines([null, 3])).toEqual({ whole: [], records: [] })
  })
  it('读取记录里没有保留样本的问题记录条数 = 跳过 + 无效 + 待核实 − 记录级样本数（连接级的不算）', () => {
    expect(unsampledProblems({ read: 300, skipped: 120, invalid: 3, conflict: 1, problems: [
      { record_id: null, upstream_order_no: '买家号列', verdict: 'skipped', reason: MISSING, scope: 'connection' },
      ...Array.from({ length: 100 }, (_, i) => ({ record_id: `rec${i}`, upstream_order_no: null, verdict: 'skipped' as const, reason: null })),
    ] })).toBe(24)
    expect(unsampledProblems({ read: 3, skipped: 1, problems: [{ record_id: 'recA', upstream_order_no: null, verdict: 'skipped', reason: null }] })).toBe(0)
    expect(unsampledProblems(null)).toBe(0)
    // 旧记录没有 problems：统计里的条数都没有样本
    expect(unsampledProblems({ read: 3, skipped: 2 })).toBe(2)
  })
})

describe('立即同步（POST /feishu/connections/{id}/sync 的 {read, new, …, problems, allocation}）', () => {
  const base: FeishuSyncResult = {
    read: 12, new: 2, update: 1, unchanged: 8, conflict: 0, historical: 0, skipped: 1, invalid: 0,
    run_id: 7, problems: [], allocation: alloc,
  }
  it('统计直接取顶层字段（不是 run.stats）', () => {
    const s = syncSummary('订单中心', base)
    expect(s.title).toBe('已同步「订单中心」')
    expect(s.message).toBe('读取 12 · 新单 2 · 更新 1 · 无变化 8 · 跳过 1；随后分配：分出 4 笔，未分出 1 笔')
    expect(s.warnings).toEqual([])
  })
  it('有问题记录时逐条列出样本，超出的给出剩余条数', () => {
    const s = syncSummary('订单中心', {
      ...base,
      skipped: 3,
      invalid: 4,
      allocation: null,
      problems: [
        { record_id: 'recA', upstream_order_no: 'PO-DEMO-1', verdict: 'skipped', reason: '采购方不是本系统接的采购方' },
        { record_id: 'recB', upstream_order_no: null, verdict: 'invalid', reason: '缺少平台订单号' },
      ],
    })
    expect(s.message).toBe('读取 12 · 新单 2 · 更新 1 · 无变化 8 · 跳过 3 · 无效 4')
    expect(s.warnings).toEqual(['PO-DEMO-1：跳过，采购方不是本系统接的采购方', '记录 recB：无效，缺少平台订单号', '另有 5 条，见「读取记录」'])
  })
  it('来源表里找不到买家号列（说整个连接的问题）：排在最前面、只写原因，不算进记录条数', () => {
    const missing = { record_id: null, upstream_order_no: '买家号列', verdict: 'skipped' as const, reason: MISSING, scope: 'connection' }
    // 只有这一条：统计里没有跳过的记录，也不出现「另有 n 条」
    const only = syncSummary('订单中心', { ...base, skipped: 0, allocation: null, problems: [missing] })
    expect(only.message).toBe('读取 12 · 新单 2 · 更新 1 · 无变化 8')
    expect(only.warnings).toEqual([MISSING])
    // 与记录样本一起：记录样本照常列出，剩余条数只按记录算
    const mixed = syncSummary('订单中心', {
      ...base,
      skipped: 2,
      allocation: null,
      problems: [missing, { record_id: 'recA', upstream_order_no: 'PO-DEMO-1', verdict: 'skipped', reason: '采购方不是本系统接的采购方' }],
    })
    expect(mixed.warnings).toEqual([MISSING, 'PO-DEMO-1：跳过，采购方不是本系统接的采购方', '另有 1 条，见「读取记录」'])
  })
  it('分配摘要带来源指定的笔数时，提示里的分出数已经含它', () => {
    const s = syncSummary('订单中心', { ...base, allocation: { ...alloc, stats: { considered: 6, assigned: 5, coverage: 2, fill: 2, designated: 1, unassigned: 1 } } })
    expect(s.message).toBe('读取 12 · 新单 2 · 更新 1 · 无变化 8 · 跳过 1；随后分配：分出 5 笔，未分出 1 笔')
  })
})

describe('写回验收（{ok, mismatches, written, restored, restore_error, error, connection}）', () => {
  const conn = { id: 1 } as FeishuConnection
  const ok: VerifyWritebackResult = { ok: true, mismatches: [], written: { 买家号: '回填验收' }, restored: true, restore_error: null, error: null, connection: conn }

  it('ok 为 true 才算通过', () => {
    const s = verifySummary(ok)
    expect(s.ok).toBe(true)
    expect(s.title).toBe('写回验收通过')
    expect(s.warnings).toEqual([])
  })
  it('读回不一致：列出每一列', () => {
    const s = verifySummary({ ...ok, ok: false, mismatches: [{ result_field: 'total', column: '总额', expected: 0, actual: null }] })
    expect(s.ok).toBe(false)
    expect(s.title).toBe('写回验收失败：读回的值与写入的不一致')
    expect(s.warnings).toEqual(['「总额」写入 0，读回 —'])
  })
  it('飞书报错', () => {
    const s = verifySummary({ ...ok, ok: false, error: '写回验收失败：飞书返回 1254045', restored: true })
    expect(s.title).toBe('写回验收失败：飞书接口报错')
    expect(s.warnings).toEqual(['写回验收失败：飞书返回 1254045'])
  })
  it('通过了但测试记录没能恢复原值：标题写明，提醒放在最前面并单独给出', () => {
    const s = verifySummary({ ...ok, restored: false, restore_error: '恢复测试记录原值失败：飞书限流（飞书错误码 99991400）。' }, 'recDEMO0001')
    expect(s.ok).toBe(true)
    expect(s.title).toBe('写回验收通过，但测试记录的原值没能恢复')
    expect(s.restoreProblem).toBe('测试记录 recDEMO0001 里还留着测试值，请到飞书里手动改回原值。恢复测试记录原值失败：飞书限流（飞书错误码 99991400）')
    expect(s.warnings).toEqual([s.restoreProblem])
    expect(verifySummary({ ...ok, restored: false }).warnings).toEqual(['测试记录的原值没有恢复，请到飞书里核对这条记录'])
    expect(verifySummary(ok).restoreProblem).toBe('')
  })
  it('没通过且没能恢复原值：恢复的问题排在不一致的列前面', () => {
    const s = verifySummary({
      ...ok,
      ok: false,
      restored: false,
      restore_error: '飞书返回 1254291',
      mismatches: [{ result_field: 'total', column: '总额', expected: 0, actual: null }],
    })
    expect(s.title).toBe('写回验收失败：读回的值与写入的不一致')
    expect(s.warnings[0]).toBe('测试记录里还留着测试值，请到飞书里手动改回原值。飞书返回 1254291')
    expect(s.warnings[1]).toBe('「总额」写入 0，读回 —')
  })
})

describe('提交导入（{batch, stats, allocation}）', () => {
  it('用提交后的实际统计与分配摘要', () => {
    const s = commitSummary({
      batch: { id: 42, filename: 'orders-demo.xlsx', sheet_name: 'S', status: 'committed', stats: null, created_at: '', committed_at: '', uploaded_by_name: null },
      stats: { total: 10, new: 6, update: 1, unchanged: 2, conflict: 0, historical: 0, invalid: 1, pending_review: 2, needs_info: 1 },
      allocation: alloc,
    })
    expect(s.title).toBe('已提交导入「orders-demo.xlsx」')
    expect(s.message).toBe('新单 6 · 更新 1 · 无变化 2 · 无效 1；其中待确认 2 笔、资料待补齐 1 笔不会下发采购；随后分配：分出 4 笔，未分出 1 笔')
  })
})

describe('清除模拟结果（{runs_ended, attempts_reset, orders_restored, orders_skipped, accounts_resumed}）', () => {
  it('列表按条数计，没能还原的任务列为警告', () => {
    const s = resetSummary({ runs_ended: [3], attempts_reset: 5, orders_restored: [1, 2, 3], orders_skipped: [{ order_id: 9, reason: '任务已被人工处理' }], accounts_resumed: [2, 4] })
    expect(s.message).toBe('中止模拟尝试 5 次，还原任务 3 笔，结束执行批次 1 个，恢复账号 2 个')
    expect(s.warnings).toEqual(['任务 9 没有还原：任务已被人工处理'])
  })
})

describe('批量人审（{updated, skipped}）', () => {
  const rows = [
    { id: 7, upstream_order_no: 'PO-DEMO-0007' },
    { id: 8, upstream_order_no: 'PO-DEMO-0008' },
  ]
  it('按服务端实际处理的条数提示，跳过的列出订单号与原因', () => {
    const s = bulkReviewSummary({ updated: [8], skipped: [{ id: 7, reason: '「已取消」状态的任务不能人审' }] }, 'approve', rows)
    expect(s.title).toBe('已通过 1 笔任务')
    expect(s.message).toBe('1 笔未处理')
    expect(s.warnings).toEqual(['PO-DEMO-0007：「已取消」状态的任务不能人审'])
  })
  it('一笔都没处理时是失败提示，不说「已通过」', () => {
    const s = bulkReviewSummary({ updated: [], skipped: [{ id: 7, reason: '「已取消」状态的任务不能人审' }] }, 'approve', rows)
    expect(s.title).toBe('批量人审通过失败：选中的任务都没有处理')
    expect(s.title).not.toContain('已通过')
    expect(bulkReviewSummary({ updated: [7, 8], skipped: [] }, 'reject', rows)).toEqual({ title: '已拒绝 2 笔任务', message: '', warnings: [] })
  })
})
