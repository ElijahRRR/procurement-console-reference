<script setup lang="ts">
/** 异常处理：异常（护栏拦截、缺货、页面错误等）、待决定重采、需要人工留意（上游在采购开始后修改或取消）。 */
import { computed, onMounted, ref } from 'vue'
import type { Overview } from '@/api/types'
import * as overviewApi from '@/api/overview'
import PageHeader from '@/components/PageHeader.vue'
import OrderListCard, { type OrderTab } from '@/components/orders/OrderListCard.vue'
import { formatNumber } from '@/utils/format'

const overview = ref<Overview | null>(null)
async function loadCounts() {
  try {
    overview.value = await overviewApi.getOverview()
  } catch {
    overview.value = null
  }
}
onMounted(loadCounts)

const tabs = computed<OrderTab[]>(() => {
  const c = overview.value?.counts ?? {}
  return [
    { value: 'exception', label: '异常', count: c.exception, emptyTitle: '没有异常任务', emptyDescription: '超限价、缺货、超配送时长、页面错误重试后仍失败的任务停在这里，其他任务照常执行。' },
    { value: 'repurchase_pending', label: '待决定重采', count: c.repurchase_pending, emptyTitle: '没有待决定重采的任务', emptyDescription: '亚马逊取消了采购订单的任务，需要人工决定是否重新采购。' },
    {
      value: 'needs_attention',
      label: '需要留意',
      view: 'all',
      extraQuery: { needs_attention: true, page_size: 200 },
      filter: (r) => r.needs_attention,
      emptyTitle: '没有需要留意的任务',
      emptyDescription: '上游在采购开始后修改或取消的任务会标记出来，提醒人工处理对应的亚马逊订单。',
    },
  ]
})
const total = computed(() => {
  const c = overview.value?.counts
  if (!c) return null
  return (c.exception ?? 0) + (c.repurchase_pending ?? 0)
})
</script>

<template>
  <div>
    <PageHeader title="异常处理">
      <template #description>
        待处理 {{ formatNumber(total) }} 笔 · 恢复前会按最新配置重新校验；已登记提交的任务先核实结果，不会自动重买
      </template>
    </PageHeader>
    <OrderListCard :tabs="tabs" mode="exceptions" @changed="loadCounts" />
  </div>
</template>
