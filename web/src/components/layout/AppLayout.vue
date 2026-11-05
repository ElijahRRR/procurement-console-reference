<script setup lang="ts">
/** 应用外壳：bg-page + 固定的 mesh 渐变；侧边栏 256 / 72px；毛玻璃顶栏 64px；主内容区内边距 4 → 6 → 8。 */
import { onMounted } from 'vue'
import AppHeader from './AppHeader.vue'
import AppSidebar from './AppSidebar.vue'
import { appState, loadAppFlags } from '@/stores/app'

onMounted(() => {
  void loadAppFlags()
})
</script>

<template>
  <div class="min-h-screen bg-gray-50 dark:bg-dark-950">
    <div class="pointer-events-none fixed inset-0 bg-mesh-gradient" aria-hidden="true" />
    <AppSidebar />
    <div
      class="relative min-h-screen transition-[margin] duration-300"
      :class="appState.sidebarCollapsed ? 'lg:ml-[72px]' : 'lg:ml-64'"
    >
      <AppHeader />
      <main class="p-4 md:p-6 lg:p-8">
        <RouterView v-slot="{ Component }">
          <component :is="Component" />
        </RouterView>
      </main>
    </div>
  </div>
</template>
