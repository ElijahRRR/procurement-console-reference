<script setup lang="ts">
/** 状态点：8px 圆点 + 文字（状态永远带文字，不只靠颜色）。 */
import { computed } from 'vue'
import type { DotColor } from '@/utils/labels'

const props = withDefaults(defineProps<{ color?: DotColor; label?: string; pulse?: boolean }>(), {
  color: 'gray',
  label: '',
  pulse: false,
})

const DOT_CLASS: Record<DotColor, string> = {
  green: 'bg-green-500',
  yellow: 'bg-yellow-500',
  red: 'bg-red-500',
  blue: 'bg-blue-500',
  gray: 'bg-gray-400 dark:bg-dark-500',
}
const dotClass = computed(() => DOT_CLASS[props.color])
</script>

<template>
  <span class="inline-flex max-w-full items-center gap-1.5 whitespace-nowrap" :title="label || undefined">
    <span class="relative inline-flex h-2 w-2 shrink-0">
      <span v-if="pulse" class="absolute inline-flex h-full w-full animate-ping rounded-full opacity-60" :class="dotClass" />
      <span class="relative inline-block h-2 w-2 rounded-full" :class="dotClass" />
    </span>
    <span v-if="label || $slots.default" class="min-w-0 truncate text-sm text-gray-700 dark:text-gray-300"><slot>{{ label }}</slot></span>
  </span>
</template>
