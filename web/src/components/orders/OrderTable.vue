<script setup lang="ts">
/**
 * 任务表格（待采购、采购记录、异常处理、概览共用）：编号列用 .code，状态列用 el-tag，点击行打开详情。
 * mode=pending 显示分配列；mode=records 显示最近一次尝试的亚马逊单号与总额；mode=exceptions 显示原因。
 */
import { computed } from 'vue'
import type { OrderRow } from '@/api/types'
import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import OrderStatusTag from './OrderStatusTag.vue'
import { formatDate, formatMoney, formatNumber, formatTime } from '@/utils/format'
import { ASSIGNMENT_KIND_LABEL, SOURCE_KIND_LABEL, errorCodeLabel, fieldLabel, labelOf } from '@/utils/labels'

const props = withDefaults(
  defineProps<{
    rows: OrderRow[]
    loading?: boolean
    mode?: 'pending' | 'records' | 'exceptions' | 'compact'
    selectable?: boolean
    showMissing?: boolean
    emptyTitle?: string
    emptyDescription?: string
  }>(),
  { loading: false, mode: 'pending', selectable: false, showMissing: false, emptyTitle: '没有任务', emptyDescription: '' },
)
const emit = defineEmits<{ open: [row: OrderRow]; selection: [rows: OrderRow[]] }>()

const compact = computed(() => props.mode === 'compact')
const showAssign = computed(() => props.mode === 'pending' || props.mode === 'compact')
const showAttempt = computed(() => props.mode === 'records')
const showReason = computed(() => props.mode === 'exceptions' || props.mode === 'compact')

function noteOf(row: OrderRow): string {
  if (props.showMissing && row.missing_fields?.length) return `缺少：${row.missing_fields.map(fieldLabel).join('、')}`
  return row.assignment_note || row.status_reason || ''
}

function reasonOf(row: OrderRow): string {
  const parts: string[] = []
  if (row.status_reason) parts.push(row.status_reason)
  else if (row.latest_attempt?.error_code) parts.push(errorCodeLabel(row.latest_attempt.error_code))
  if (row.needs_attention && row.attention_note) parts.push(row.attention_note)
  return parts.join('；')
}

function onRowClick(row: OrderRow, _col: unknown, event: Event) {
  const target = event.target as HTMLElement | null
  if (target?.closest('.el-checkbox')) return
  emit('open', row)
}
</script>

<template>
  <el-table
    v-loading="loading"
    :data="rows"
    row-key="id"
    class="pc-clickable-rows"
    @row-click="onRowClick"
    @selection-change="(v: OrderRow[]) => emit('selection', v)"
  >
    <el-table-column v-if="selectable" type="selection" width="48" :reserve-selection="false" />
    <el-table-column label="平台订单号" min-width="220">
      <template #default="{ row }">
        <span class="code pc-code-wrap" :title="row.upstream_order_no">{{ row.upstream_order_no }}</span>
        <div class="mt-1 flex flex-wrap items-center gap-x-2 text-xs text-gray-500 dark:text-dark-400">
          <span>
            {{ row.source_kinds.map((k: 'feishu' | 'file') => labelOf(SOURCE_KIND_LABEL, k)).join(' + ') }}<template v-if="row.store_name"> · {{ row.store_name }}</template>
          </span>
          <el-tooltip v-if="row.needs_attention" :content="row.attention_note || '需要人工留意'" placement="top">
            <span class="inline-flex items-center gap-0.5 font-medium text-amber-700 dark:text-amber-400"><AppIcon name="bell" size="xs" />需留意</span>
          </el-tooltip>
        </div>
      </template>
    </el-table-column>
    <el-table-column v-if="!compact" label="ASIN" min-width="124">
      <template #default="{ row }">
        <div class="whitespace-nowrap font-mono text-sm text-gray-900 dark:text-gray-100">{{ row.asin_eff || '—' }}</div>
        <el-tooltip v-if="row.has_override" content="控制台已修改执行参数，原值见详情" placement="top">
          <el-tag size="small" type="warning" class="mt-1" disable-transitions>已改</el-tag>
        </el-tooltip>
      </template>
    </el-table-column>
    <el-table-column v-if="!compact && !showAttempt" label="数量" width="64" align="right">
      <template #default="{ row }">{{ formatNumber(row.quantity_eff) }}</template>
    </el-table-column>
    <el-table-column label="商品小计" width="104" align="right">
      <template #default="{ row }">{{ formatMoney(row.subtotal_eff) }}</template>
    </el-table-column>
    <el-table-column v-if="!compact && !showAttempt" label="限价" width="96" align="right">
      <template #default="{ row }">{{ formatMoney(row.price_cap_eff) }}</template>
    </el-table-column>
    <el-table-column v-if="!showAttempt && !compact" label="建议采购日期" width="112">
      <template #default="{ row }">{{ formatDate(row.suggested_purchase_date) }}</template>
    </el-table-column>
    <el-table-column v-if="showAssign || showAttempt" label="账号" min-width="136">
      <template #default="{ row }">
        <template v-if="row.assigned_account">
          <div class="whitespace-nowrap text-gray-900 dark:text-gray-100">{{ row.assigned_account.name }}</div>
          <div v-if="row.assignment_kind" class="text-xs text-gray-500 dark:text-dark-400">
            {{ labelOf(ASSIGNMENT_KIND_LABEL, row.assignment_kind) }}
          </div>
        </template>
        <span v-else class="text-gray-400 dark:text-dark-500">未分配</span>
      </template>
    </el-table-column>
    <template v-if="showAttempt">
      <el-table-column label="亚马逊订单号" min-width="220">
        <template #default="{ row }">
          <template v-if="row.latest_attempt?.amazon_order_no">
            <span class="code pc-code-wrap" :title="row.latest_attempt.amazon_order_no">{{ row.latest_attempt.amazon_order_no }}</span>
            <el-tag v-if="row.latest_attempt.simulated" size="small" type="warning" class="ml-1.5" disable-transitions>模拟</el-tag>
          </template>
          <span v-else class="text-gray-400 dark:text-dark-500">—</span>
        </template>
      </el-table-column>
      <el-table-column label="结算总额" width="104" align="right">
        <template #default="{ row }">{{ formatMoney(row.latest_attempt?.total) }}</template>
      </el-table-column>
      <el-table-column label="完成时间" width="112">
        <template #default="{ row }">
          <template v-if="row.latest_attempt?.finished_at">
            <div class="whitespace-nowrap">{{ formatDate(row.latest_attempt.finished_at) }}</div>
            <div class="text-xs text-gray-500 dark:text-dark-400">{{ formatTime(row.latest_attempt.finished_at) }}</div>
          </template>
          <span v-else class="text-gray-400 dark:text-dark-500">—</span>
        </template>
      </el-table-column>
    </template>
    <el-table-column label="状态" width="116">
      <template #default="{ row }">
        <OrderStatusTag :status="row.status" :reason="row.status_reason" />
      </template>
    </el-table-column>
    <el-table-column v-if="showReason" label="原因" min-width="160">
      <template #default="{ row }">
        <span class="text-gray-700 dark:text-gray-300">{{ reasonOf(row as OrderRow) || '—' }}</span>
      </template>
    </el-table-column>
    <el-table-column v-else-if="!showAttempt" label="说明" min-width="136">
      <template #default="{ row }">
        <span class="text-gray-700 dark:text-gray-300">{{ noteOf(row as OrderRow) || '—' }}</span>
      </template>
    </el-table-column>
    <template #empty>
      <EmptyState :title="emptyTitle" :description="emptyDescription" icon="inbox" compact />
    </template>
  </el-table>
</template>
