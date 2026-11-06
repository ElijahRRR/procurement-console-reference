<script setup lang="ts">
/** 页面标题（.page-header）：左侧标题与说明，右侧操作按钮插槽。 */
defineProps<{ title: string; description?: string }>()
</script>

<template>
  <div class="page-header flex flex-wrap items-start justify-between gap-4">
    <div class="min-w-0">
      <h1 class="page-title">{{ title }}</h1>
      <p v-if="description || $slots.description" class="page-description">
        <slot name="description">{{ description }}</slot>
      </p>
    </div>
    <div v-if="$slots.actions" class="flex flex-wrap items-center gap-3">
      <slot name="actions" />
    </div>
  </div>
</template>
