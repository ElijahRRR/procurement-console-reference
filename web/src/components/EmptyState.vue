<script setup lang="ts">
/** 空状态（.empty-state）：80px bg-muted 圆角框里放 40px 图标，标题 + 说明 + 操作插槽。不用 el-empty。 */
import AppIcon, { type IconName } from './AppIcon.vue'

withDefaults(defineProps<{ title: string; description?: string; icon?: IconName; compact?: boolean }>(), {
  description: '',
  icon: 'inbox',
  compact: false,
})
</script>

<template>
  <div class="empty-state" :class="compact ? 'py-8' : ''">
    <div class="mb-5 flex h-20 w-20 items-center justify-center rounded-2xl bg-gray-100 text-gray-400 dark:bg-dark-800 dark:text-dark-500">
      <AppIcon :name="icon" size="xl" class="h-10 w-10" />
    </div>
    <h3 class="empty-state-title">{{ title }}</h3>
    <p v-if="description || $slots.default" class="empty-state-description">
      <slot>{{ description }}</slot>
    </p>
    <div v-if="$slots.actions" class="mt-6 flex flex-wrap items-center justify-center gap-3">
      <slot name="actions" />
    </div>
  </div>
</template>
