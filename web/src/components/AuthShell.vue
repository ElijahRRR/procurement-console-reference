<script setup lang="ts">
/** 登录与首次设置共用的外框：mesh 渐变底 + 居中卡片 + 品牌标志 + 主题切换。 */
import AppIcon from './AppIcon.vue'
import { useTheme } from '@/composables/useTheme'

defineProps<{ title: string; description: string }>()
const { isDark, toggle } = useTheme()
</script>

<template>
  <div class="relative flex min-h-screen items-center justify-center bg-gray-50 px-4 py-12 dark:bg-dark-950">
    <div class="pointer-events-none fixed inset-0 bg-mesh-gradient" aria-hidden="true" />
    <button
      type="button"
      class="absolute right-4 top-4 flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-900 dark:text-dark-400 dark:hover:bg-dark-800 dark:hover:text-white"
      @click="toggle"
    >
      <AppIcon :name="isDark ? 'sun' : 'moon'" size="sm" />{{ isDark ? '浅色模式' : '深色模式' }}
    </button>
    <div class="pc-auth-card relative">
      <div class="mb-6 flex items-center gap-3">
        <div class="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-primary text-white shadow-glow">
          <AppIcon name="cube" />
        </div>
        <div>
          <div class="text-lg font-bold text-gray-900 dark:text-white">采购控制台</div>
          <div class="text-xs text-gray-500 dark:text-dark-400">订单分配、额度与插件执行</div>
        </div>
      </div>
      <div class="card">
        <div class="card-body">
          <h1 class="text-lg font-semibold text-gray-900 dark:text-white">{{ title }}</h1>
          <p class="mb-6 mt-1 text-sm text-gray-500 dark:text-dark-400">{{ description }}</p>
          <slot />
        </div>
      </div>
    </div>
  </div>
</template>
