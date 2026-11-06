<script setup lang="ts">
/**
 * 异常处理：异常（护栏拦截、缺货、页面错误等）、结果待核实（已登记提交、账号已暂停）、待决定重采、
 * 需要留意（上游在采购开始后修改或取消，服务端按 needs_attention 筛选）。
 */
import { computed, onMounted, ref } from 'vue'
import type { Overview } from '@/api/types'
import * as overviewApi from '@/api/overview'
import PageHeader from '@/components/PageHeader.vue'
import OrderListCard, { type OrderTab } from '@/components/orders/OrderListCard.vue'
import { formatNumber } from '@/utils/format'
import { exceptionManual } from '@/utils/overview'

const overview = ref<Overview | null>(null)
async function loadCounts() {
  try {
    overview.value = await overviewApi.getOverview()
  } catch {
    overview.value = null
  }
}
onMounted(loadCounts)

/** 需要留意的任务数（概览的 needs_attention，不论状态；读不到为 null，标签页不显示数字） */
const attentionTotal = computed(() => (typeof overview.value?.needs_attention === 'number' ? overview.value.needs_attention : null))

const tabs = computed<OrderTab[]>(() => {
  const c = overview.value?.counts ?? {}
  return [
    { value: 'exception', label: '异常', count: c.exception, emptyTitle: '没有异常任务', emptyDescription: '超限价、缺货、超配送时长、页面错误重试后仍失败的任务停在这里，其他任务照常执行。' },
    { value: 'result_unknown', label: '结果待核实', count: c.result_unknown, emptyTitle: '没有结果待核实的任务', emptyDescription: '已登记提交但没有确认结果的任务，对应账号已暂停执行；在亚马逊订单页核实后登记结果，核实前不会重买。' },
    { value: 'repurchase_pending', label: '待决定重采', count: c.repurchase_pending, emptyTitle: '没有待决定重采的任务', emptyDescription: '亚马逊取消了采购订单的任务，需要人工决定是否重新采购。' },
    {
      value: 'needs_attention',
      label: '需要留意',
      view: 'all',
      query: { needs_attention: true },
      count: attentionTotal.value,
      emptyTitle: '没有需要留意的任务',
      emptyDescription: '上游在采购开始后修改或取消的任务会标记出来，提醒人工处理对应的亚马逊订单；处理后在任务详情里「标记已处理」。',
    },
  ]
})
/** 与概览「异常待处理」同一口径：异常 + 结果待核实 + 待决定重采 */
const total = computed(() => (overview.value ? exceptionManual(overview.value.counts).total : null))
</script>

<template>
  <div>
    <PageHeader :title="total === null ? '待处理' : `待处理 ${formatNumber(total)} 笔`">
      <template #description>
        恢复前会按最新配置重新校验；已登记提交的任务先核实结果，不会自动重买
      </template>
    </PageHeader>
    <OrderListCard :tabs="tabs" mode="exceptions" @changed="loadCounts" />
  </div>
</template>
