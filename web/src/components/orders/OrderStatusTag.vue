<script setup lang="ts">
/** 任务状态标签：el-tag（soft 底 + 700 字），有原因时悬停显示。 */
import { computed } from 'vue'
import type { OrderStatus } from '@/api/types'
import { ORDER_STATUS_LABEL, ORDER_STATUS_TAG, labelOf, tagOf } from '@/utils/labels'

const props = defineProps<{ status: OrderStatus; reason?: string | null; size?: 'small' | 'default' }>()
const label = computed(() => labelOf(ORDER_STATUS_LABEL, props.status))
const type = computed(() => tagOf(ORDER_STATUS_TAG, props.status))
</script>

<template>
  <el-tooltip v-if="reason" :content="reason" placement="top" :show-after="300">
    <el-tag :type="type" :size="size" disable-transitions>{{ label }}</el-tag>
  </el-tooltip>
  <el-tag v-else :type="type" :size="size" disable-transitions>{{ label }}</el-tag>
</template>
