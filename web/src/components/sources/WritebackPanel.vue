<script setup lang="ts">
/** 写回队列：按状态筛选，失败或待写回的可立即重试。写回按（任务、连接、结果版本）去重，旧版本被新结果取代。 */
import { computed, onMounted, ref, watch } from 'vue'
import type { Writeback, WritebackStatus } from '@/api/types'
import { WRITEBACK_STATUSES } from '@/api/types'
import * as writebacks from '@/api/writebacks'
import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import { displayValue, formatDateTime, formatNumber, formatRelative } from '@/utils/format'
import { WRITEBACK_STATUS_LABEL, WRITEBACK_STATUS_TAG, labelOf, tagOf } from '@/utils/labels'
import { notifyFailure, notifySuccess } from '@/utils/feedback'

const status = ref<WritebackStatus | ''>('')
const rows = ref<Writeback[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const busyId = ref<number | null>(null)

const options = computed(() => [
  { value: '', label: '全部' },
  ...WRITEBACK_STATUSES.map((s) => ({ value: s, label: WRITEBACK_STATUS_LABEL[s] })),
])

async function load() {
  loading.value = true
  try {
    const res = await writebacks.listWritebacks({ status: status.value, page: page.value, page_size: 20 })
    rows.value = res.rows
    total.value = res.total
  } catch (e) {
    notifyFailure('读取写回队列', e)
  } finally {
    loading.value = false
  }
}
onMounted(load)
watch(status, () => {
  page.value = 1
  void load()
})

async function retry(w: Writeback) {
  busyId.value = w.id
  try {
    await writebacks.retryWriteback(w.id)
    notifySuccess('已安排立即重试', '只重试写回，不会重新采购。')
    await load()
  } catch (e) {
    notifyFailure('重试写回', e)
  } finally {
    busyId.value = null
  }
}

function fieldsText(f: Record<string, unknown>): string {
  return Object.entries(f ?? {})
    .map(([k, v]) => `${k}：${displayValue(v)}`)
    .join('；')
}
</script>

<template>
  <div>
    <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
      <el-segmented v-model="status" :options="options" />
      <el-button @click="load"><AppIcon name="refresh" size="sm" class="mr-1.5" />刷新</el-button>
    </div>
    <el-card class="eq-table-card" shadow="always">
      <el-table v-loading="loading" :data="rows">
        <el-table-column label="平台订单号" min-width="224">
          <template #default="{ row }">
            <span class="code">{{ row.upstream_order_no }}</span>
            <div class="mt-1 text-xs text-gray-500 dark:text-dark-400">结果版本 {{ row.result_version }}</div>
          </template>
        </el-table-column>
        <el-table-column label="连接 / 记录" min-width="170">
          <template #default="{ row }">
            <div class="text-gray-900 dark:text-gray-100">{{ row.connection_name || `连接 ${row.connection_id}` }}</div>
            <div class="font-mono text-xs text-gray-500 dark:text-dark-400">{{ row.record_id }}</div>
          </template>
        </el-table-column>
        <el-table-column label="写回内容" min-width="240">
          <template #default="{ row }">
            <span class="line-clamp-2 text-gray-700 dark:text-gray-300">{{ fieldsText(row.fields) || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="tagOf(WRITEBACK_STATUS_TAG, row.status)" disable-transitions>{{ labelOf(WRITEBACK_STATUS_LABEL, row.status) }}</el-tag>
            <div class="mt-1 text-xs text-gray-500 dark:text-dark-400">已试 {{ formatNumber(row.tries) }} 次</div>
          </template>
        </el-table-column>
        <el-table-column label="最近错误 / 下次重试" min-width="220">
          <template #default="{ row }">
            <div v-if="row.last_error" class="text-red-600 dark:text-red-400">{{ row.last_error }}</div>
            <div v-if="row.status === 'pending' || row.status === 'failed'" class="text-xs text-gray-500 dark:text-dark-400">
              下次 {{ formatDateTime(row.next_try_at) }}（{{ formatRelative(row.next_try_at) }}）
            </div>
            <div v-else-if="row.done_at" class="text-xs text-gray-500 dark:text-dark-400">完成于 {{ formatDateTime(row.done_at) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="96" align="right">
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'failed' || row.status === 'pending'"
              text
              size="small"
              :loading="busyId === row.id"
              @click="retry(row as Writeback)"
            >
              重试
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <EmptyState title="写回队列为空" description="飞书来源的任务采购成功后，结果按写回对应排队写入结果表；未验收的连接只积压不发送。" icon="sync" compact />
        </template>
      </el-table>
      <div class="flex items-center justify-between border-t border-gray-200 px-4 py-3 dark:border-dark-700">
        <span class="text-sm text-gray-700 dark:text-gray-300">共 {{ formatNumber(total) }} 条</span>
        <el-pagination v-model:current-page="page" background layout="prev, pager, next" :page-size="20" :total="total" @current-change="load" />
      </div>
    </el-card>
  </div>
</template>
