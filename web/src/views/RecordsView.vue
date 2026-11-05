<script setup lang="ts">
/** 采购记录：采购中、已采购、结果待核实、已取消、已关闭；显示最近一次尝试的亚马逊订单号、总额与模拟标记。 */
import { computed, onMounted, ref } from 'vue'
import type { Overview } from '@/api/types'
import * as overviewApi from '@/api/overview'
import { downloadOrdersExport } from '@/api/imports'
import AppIcon from '@/components/AppIcon.vue'
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
    { value: 'executing', label: '采购中', count: c.executing, emptyTitle: '没有采购中的任务', emptyDescription: '插件领取后、结果回报前的任务。' },
    { value: 'purchased', label: '已采购', count: c.purchased, emptyTitle: '还没有已采购的任务', emptyDescription: '采购成功或来源已带亚马逊订单号的任务。模拟结果带「模拟」标记，不写回飞书。' },
    { value: 'result_unknown', label: '结果待核实', count: c.result_unknown, emptyTitle: '没有结果待核实的任务', emptyDescription: '已登记提交但没有确认结果的任务，核实前不会重买。' },
    { value: 'cancelled', label: '已取消', count: c.cancelled, emptyTitle: '没有已取消的任务' },
    { value: 'closed', label: '已关闭', count: c.closed, emptyTitle: '没有已关闭的任务', emptyDescription: '亚马逊取消后人工决定不重采的任务。' },
  ]
})
const purchased = computed(() => overview.value?.counts?.purchased)
</script>

<template>
  <div>
    <PageHeader title="采购记录">
      <template #description>已采购 {{ formatNumber(purchased) }} 笔 · 每次采购尝试与亚马逊订单关联见任务详情</template>
      <template #actions>
        <el-button @click="downloadOrdersExport({ view: 'purchased' })"><AppIcon name="download" size="sm" class="mr-1.5" />导出已采购</el-button>
      </template>
    </PageHeader>
    <OrderListCard :tabs="tabs" mode="records" @changed="loadCounts" />
  </div>
</template>
