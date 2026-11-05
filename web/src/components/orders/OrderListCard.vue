<script setup lang="ts">
/**
 * 任务列表卡片（待采购、采购记录、异常处理共用）：el-segmented 视图切换 + 搜索 + 表格 + 分页 + 详情抽屉。
 * 当前视图与搜索词同步到地址栏（?view=&q=），便于从概览跳转与刷新后保持。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { OrderRow, OrderView, Page } from '@/api/types'
import type { Query as ClientQuery } from '@/api/client'
import * as ordersApi from '@/api/orders'
import { get } from '@/api/client'
import AppIcon from '@/components/AppIcon.vue'
import OrderTable from './OrderTable.vue'
import OrderDetailDrawer from './OrderDetailDrawer.vue'
import { formatNumber } from '@/utils/format'
import { confirmAction, notifyFailure, notifySuccess } from '@/utils/feedback'

export interface OrderTab {
  value: string
  label: string
  /** 后端视图；不给时等于 value */
  view?: OrderView
  /** 附加查询参数（契约之外的筛选，后端不认时由 filter 在前端兜底） */
  extraQuery?: ClientQuery
  filter?: (row: OrderRow) => boolean
  count?: number | null
  emptyTitle?: string
  emptyDescription?: string
}

const props = withDefaults(
  defineProps<{
    tabs: OrderTab[]
    mode?: 'pending' | 'records' | 'exceptions'
    /** 在哪些标签页显示批量人审 */
    bulkReviewTabs?: string[]
    /** 在哪些标签页显示缺失字段 */
    missingTabs?: string[]
  }>(),
  { mode: 'pending', bulkReviewTabs: () => [], missingTabs: () => [] },
)
const emit = defineEmits<{ changed: [] }>()

const route = useRoute()
const router = useRouter()

const initialTab = () => {
  const v = typeof route.query.view === 'string' ? route.query.view : ''
  return props.tabs.some((t) => t.value === v) ? v : props.tabs[0].value
}
const tab = ref(initialTab())
const q = ref(typeof route.query.q === 'string' ? route.query.q : '')
const page = ref(1)
const pageSize = ref(20)
const rows = ref<OrderRow[]>([])
const total = ref(0)
const loading = ref(false)
const selection = ref<OrderRow[]>([])
const bulkBusy = ref(false)
const drawerOpen = ref(false)
const drawerId = ref<number | null>(null)

const current = computed(() => props.tabs.find((t) => t.value === tab.value) ?? props.tabs[0])
const bulkEnabled = computed(() => props.bulkReviewTabs.includes(tab.value))
const segmentOptions = computed(() =>
  props.tabs.map((t) => ({
    value: t.value,
    label: t.count === null || t.count === undefined ? t.label : `${t.label} ${formatNumber(t.count)}`,
  })),
)

let seq = 0
async function load() {
  const my = ++seq
  loading.value = true
  try {
    const t = current.value
    let res: Page<OrderRow>
    if (t.extraQuery) {
      const query: ClientQuery = { view: t.view ?? 'all', q: q.value.trim(), page: page.value, page_size: pageSize.value, ...t.extraQuery }
      const raw = await get<Page<OrderRow>>('/orders', query)
      res = { total: raw?.total ?? 0, rows: raw?.rows ?? [] }
    } else {
      res = await ordersApi.listOrders({
        view: (t.view ?? t.value) as OrderView,
        q: q.value.trim() || undefined,
        page: page.value,
        page_size: pageSize.value,
      })
    }
    if (my !== seq) return
    if (t.filter) {
      const kept = res.rows.filter(t.filter)
      rows.value = kept
      total.value = kept.length === res.rows.length ? res.total : kept.length
    } else {
      rows.value = res.rows
      total.value = res.total
    }
  } catch (e) {
    if (my !== seq) return
    rows.value = []
    total.value = 0
    notifyFailure('读取任务列表', e)
  } finally {
    if (my === seq) loading.value = false
  }
}

function syncQuery() {
  const query: Record<string, string> = { ...(route.query as Record<string, string>) }
  query.view = tab.value
  if (q.value.trim()) query.q = q.value.trim()
  else delete query.q
  void router.replace({ query })
}

watch(tab, () => {
  page.value = 1
  selection.value = []
  syncQuery()
  void load()
})
// 已在本页时从别处跳来（如概览点统计卡），地址栏的 view 变了也要跟着切换
watch(
  () => route.query.view,
  (v) => {
    if (typeof v === 'string' && v !== tab.value && props.tabs.some((t) => t.value === v)) tab.value = v
  },
)
watch([page, pageSize], () => void load())

let timer: ReturnType<typeof setTimeout> | undefined
function onSearch() {
  clearTimeout(timer)
  timer = setTimeout(() => {
    page.value = 1
    syncQuery()
    void load()
  }, 300)
}

onMounted(load)

function open(row: OrderRow) {
  drawerId.value = row.id
  drawerOpen.value = true
}

function onChanged() {
  void load()
  emit('changed')
}

async function bulk(decision: 'approve' | 'reject') {
  const ids = selection.value.map((r) => r.id)
  if (!ids.length) return
  const approve = decision === 'approve'
  const ok = await confirmAction({
    message: approve
      ? `确定要将选中的 ${formatNumber(ids.length)} 笔任务标记为人审通过吗？`
      : `确定要拒绝选中的 ${formatNumber(ids.length)} 笔任务吗？拒绝后不会下发采购。`,
    danger: !approve,
    confirmText: approve ? '人审通过' : '拒绝',
  })
  if (!ok) return
  bulkBusy.value = true
  try {
    await ordersApi.bulkReview(ids, decision)
    notifySuccess(approve ? `已通过 ${formatNumber(ids.length)} 笔任务` : `已拒绝 ${formatNumber(ids.length)} 笔任务`)
    selection.value = []
    onChanged()
  } catch (e) {
    notifyFailure(approve ? '批量人审通过' : '批量拒绝', e)
  } finally {
    bulkBusy.value = false
  }
}

defineExpose({ reload: load })
</script>

<template>
  <div>
    <div class="mb-4 flex flex-wrap items-center gap-3">
      <el-segmented v-model="tab" :options="segmentOptions" />
      <div class="ml-auto flex flex-wrap items-center gap-3">
        <el-input v-model="q" style="width: 18rem" clearable placeholder="搜索平台订单号、ASIN、收件人" @input="onSearch" @clear="onSearch">
          <template #prefix><AppIcon name="search" size="sm" /></template>
        </el-input>
        <slot name="tools" :tab="tab" />
      </div>
    </div>

    <div
      v-if="bulkEnabled && selection.length"
      class="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl bg-primary-50 px-4 py-2.5 dark:bg-primary-900/20"
    >
      <span class="text-sm text-primary-700 dark:text-primary-300">已选 {{ formatNumber(selection.length) }} 笔</span>
      <div>
        <el-button size="small" type="danger" plain :loading="bulkBusy" @click="bulk('reject')">拒绝</el-button>
        <el-button size="small" type="primary" :loading="bulkBusy" @click="bulk('approve')">人审通过</el-button>
      </div>
    </div>

    <el-card class="eq-table-card" shadow="always">
      <OrderTable
        :rows="rows"
        :loading="loading"
        :mode="mode"
        :selectable="bulkEnabled"
        :show-missing="missingTabs.includes(tab)"
        :empty-title="current.emptyTitle || `没有「${current.label}」的任务`"
        :empty-description="current.emptyDescription || ''"
        @open="open"
        @selection="(v) => (selection = v)"
      />
      <div class="flex flex-wrap items-center justify-between gap-3 border-t border-gray-200 px-4 py-3 dark:border-dark-700">
        <span class="text-sm text-gray-700 dark:text-gray-300">共 {{ formatNumber(total) }} 笔</span>
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          background
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="sizes, prev, pager, next"
        />
      </div>
    </el-card>

    <OrderDetailDrawer v-model="drawerOpen" :order-id="drawerId" @changed="onChanged" />
  </div>
</template>
