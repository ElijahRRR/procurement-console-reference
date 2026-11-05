<script setup lang="ts">
/** 待采购：今日可采购、未来日期、待分配、待确认（批量人审）、资料待补齐（显示缺失字段）、待核实。 */
import { computed, onMounted, ref } from 'vue'
import type { Overview } from '@/api/types'
import * as overviewApi from '@/api/overview'
import * as allocationApi from '@/api/allocation'
import { downloadOrdersExport } from '@/api/imports'
import AppIcon from '@/components/AppIcon.vue'
import PageHeader from '@/components/PageHeader.vue'
import OrderListCard, { type OrderTab } from '@/components/orders/OrderListCard.vue'
import { formatDate, formatNumber } from '@/utils/format'
import { confirmAction, notifyFailure, notifySuccess } from '@/utils/feedback'

const overview = ref<Overview | null>(null)
const list = ref<InstanceType<typeof OrderListCard>>()
const allocating = ref(false)

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
  const o = overview.value
  return [
    { value: 'today', label: '今日可采购', count: o?.today_executable, emptyTitle: '今天没有可采购的任务', emptyDescription: '已分配且建议采购日期不晚于今天的任务会出现在这里，由对应浏览器插件领取执行。' },
    { value: 'future', label: '未来日期', count: o?.future_assigned, emptyTitle: '没有未来日期的任务', emptyDescription: '建议采购日期在明天及以后的已分配任务，到期前不会执行。' },
    { value: 'unassigned', label: '待分配', count: c.queued, emptyTitle: '没有待分配的任务', emptyDescription: '分不出去的任务会写明原因：金额不在任何启用账号的区间内，或匹配账号当天额度已满。' },
    { value: 'pending_review', label: '待确认', count: c.pending_review, emptyTitle: '没有待确认的任务', emptyDescription: '人审结果不是「通过」的任务停在这里，不下发采购。' },
    { value: 'needs_info', label: '资料待补齐', count: c.needs_info, emptyTitle: '没有资料待补齐的任务', emptyDescription: '缺少 ASIN、数量、单价、限价、建议采购日期或收件信息的任务停在这里。' },
    { value: 'conflict', label: '待核实', count: c.conflict, emptyTitle: '没有待核实的任务', emptyDescription: '不同来源内容冲突，或外部单号与本系统记录矛盾的任务停在这里。' },
  ]
})

async function reallocate() {
  const ok = await confirmAction({ message: '确定要重新分配吗？自动分配的任务会按当前账号配置重算，人工改派的任务保持不动。' })
  if (!ok) return
  allocating.value = true
  try {
    const res = await allocationApi.runAllocation()
    notifySuccess('重新分配完成', `分出 ${formatNumber(res?.stats?.assigned)} 笔，未分出 ${formatNumber(res?.stats?.unassigned)} 笔。`)
    await Promise.all([loadCounts(), list.value?.reload()])
  } catch (e) {
    notifyFailure('重新分配', e)
  } finally {
    allocating.value = false
  }
}
</script>

<template>
  <div>
    <PageHeader title="待采购">
      <template #description>
        <template v-if="overview">北京日期 {{ formatDate(overview.bj_date) }} · 今日可采购 {{ formatNumber(overview.today_executable) }} 笔，未来日期 {{ formatNumber(overview.future_assigned) }} 笔</template>
        <template v-else>建议采购日期不晚于今天的已分配任务才会被插件领取</template>
      </template>
      <template #actions>
        <el-button @click="downloadOrdersExport({ view: 'all' })"><AppIcon name="download" size="sm" class="mr-1.5" />导出</el-button>
        <el-button type="primary" :loading="allocating" @click="reallocate"><AppIcon name="swap" size="sm" class="mr-1.5" />重新分配</el-button>
      </template>
    </PageHeader>
    <OrderListCard
      ref="list"
      :tabs="tabs"
      mode="pending"
      :bulk-review-tabs="['pending_review']"
      :missing-tabs="['needs_info']"
      @changed="loadCounts"
    />
  </div>
</template>
