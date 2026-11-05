<script setup lang="ts">
/**
 * 统计卡（.stat-card）：图标框 + 标签 + 数值 + 可选趋势 / 说明。点击时触发 click（可用于跳转）。
 * 涨跌趋势（hintTone 为 up / down）紧跟数值；说明文字（muted）放在数值下方，可换行，不截断。
 */
import { computed } from 'vue'
import AppIcon, { type IconName } from './AppIcon.vue'

const props = withDefaults(
  defineProps<{
    label: string
    value: string | number
    icon: IconName
    tone?: 'primary' | 'success' | 'warning' | 'danger'
    hint?: string
    hintTone?: 'up' | 'down' | 'muted'
    clickable?: boolean
  }>(),
  { tone: 'primary', hint: '', hintTone: 'muted', clickable: false },
)
defineEmits<{ click: [] }>()

// 类名必须完整出现在代码里（Tailwind / bundle.css 不支持拼接）
const ICON_CLASS = {
  primary: 'stat-icon stat-icon-primary',
  success: 'stat-icon stat-icon-success',
  warning: 'stat-icon stat-icon-warning',
  danger: 'stat-icon stat-icon-danger',
} as const
const HINT_CLASS = {
  up: 'stat-trend stat-trend-up',
  down: 'stat-trend stat-trend-down',
  muted: 'stat-trend text-gray-500 dark:text-dark-400',
} as const
const iconClass = computed(() => ICON_CLASS[props.tone])
const hintClass = computed(() => HINT_CLASS[props.hintTone])
</script>

<template>
  <div
    class="stat-card"
    :class="clickable ? 'cursor-pointer transition-shadow hover:shadow-card-hover' : ''"
    :role="clickable ? 'button' : undefined"
    :tabindex="clickable ? 0 : undefined"
    @click="clickable && $emit('click')"
    @keydown.enter="clickable && $emit('click')"
  >
    <div :class="iconClass"><AppIcon :name="icon" size="lg" /></div>
    <div class="min-w-0 flex-1">
      <p class="stat-label truncate">{{ label }}</p>
      <div class="mt-1 flex items-baseline gap-2">
        <p class="stat-value">{{ value }}</p>
        <span v-if="hint && hintTone !== 'muted'" :class="hintClass" class="truncate">{{ hint }}</span>
      </div>
      <p v-if="hint && hintTone === 'muted'" class="mt-1 text-xs leading-5 text-gray-500 dark:text-dark-400">{{ hint }}</p>
    </div>
  </div>
</template>
