<script setup lang="ts">
/** 侧边栏（.sidebar）：展开 256px / 收起 72px；分组导航、当前项高亮、深色模式切换、收起按钮。小屏为抽屉。 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import AppIcon, { type IconName } from '@/components/AppIcon.vue'
import { useTheme } from '@/composables/useTheme'
import { appState, setSidebarCollapsed } from '@/stores/app'

interface NavItem {
  to: string
  label: string
  icon: IconName
}
interface NavSection {
  title?: string
  items: NavItem[]
}

const sections: NavSection[] = [
  {
    items: [
      { to: '/', label: '概览', icon: 'home' },
      { to: '/orders', label: '待采购', icon: 'clipboard' },
      { to: '/records', label: '采购记录', icon: 'document' },
      { to: '/exceptions', label: '异常处理', icon: 'exclamationTriangle' },
    ],
  },
  {
    title: '配置',
    items: [
      { to: '/sources', label: '订单来源', icon: 'database' },
      { to: '/accounts', label: '账号与设备', icon: 'users' },
      { to: '/settings', label: '设置', icon: 'cog' },
    ],
  },
]

const route = useRoute()
const { isDark, toggle } = useTheme()
const collapsed = computed(() => appState.sidebarCollapsed && !appState.mobileSidebarOpen)

const isActive = (to: string) => (to === '/' ? route.path === '/' : route.path === to || route.path.startsWith(`${to}/`))
const closeMobile = () => {
  appState.mobileSidebarOpen = false
}
</script>

<template>
  <div
    v-if="appState.mobileSidebarOpen"
    class="fixed inset-0 z-30 bg-black/50 backdrop-blur-sm lg:hidden"
    aria-hidden="true"
    @click="closeMobile"
  />
  <aside
    class="sidebar"
    :class="[
      collapsed ? 'w-[72px]' : '',
      appState.mobileSidebarOpen ? 'translate-x-0' : '-translate-x-full lg:translate-x-0',
    ]"
    aria-label="主导航"
  >
    <div class="sidebar-header" :class="collapsed ? 'sidebar-header-collapsed' : ''">
      <div class="sidebar-logo flex h-9 w-9 items-center justify-center overflow-hidden rounded-xl bg-gradient-primary text-white shadow-glow">
        <AppIcon name="cube" size="md" />
      </div>
      <div class="sidebar-brand" :class="collapsed ? 'sidebar-brand-collapsed' : ''" :aria-hidden="collapsed ? 'true' : undefined">
        <span class="sidebar-brand-title text-lg font-bold text-gray-900 dark:text-white">采购控制台</span>
      </div>
    </div>
    <nav class="sidebar-nav scrollbar-hide">
      <div v-for="(section, si) in sections" :key="si" class="sidebar-section">
        <div v-if="section.title" class="sidebar-section-title" :class="collapsed ? 'sidebar-section-title-collapsed' : ''">
          <span class="sidebar-section-title-text" :class="collapsed ? 'sidebar-section-title-text-collapsed' : ''">{{ section.title }}</span>
        </div>
        <RouterLink
          v-for="item in section.items"
          :key="item.to"
          :to="item.to"
          class="sidebar-link mb-1"
          :class="[isActive(item.to) ? 'sidebar-link-active' : '', collapsed ? 'sidebar-link-collapsed' : '']"
          :title="collapsed ? item.label : undefined"
          :aria-current="isActive(item.to) ? 'page' : undefined"
          @click="closeMobile"
        >
          <AppIcon :name="item.icon" class="h-5 w-5 flex-shrink-0" />
          <span class="sidebar-label" :class="collapsed ? 'sidebar-label-collapsed' : ''">{{ item.label }}</span>
        </RouterLink>
      </div>
    </nav>
    <div class="mt-auto border-t border-gray-100 p-3 dark:border-dark-800">
      <button
        type="button"
        class="sidebar-link mb-2 w-full"
        :class="collapsed ? 'sidebar-link-collapsed' : ''"
        :title="collapsed ? (isDark ? '浅色模式' : '深色模式') : undefined"
        @click="toggle"
      >
        <AppIcon :name="isDark ? 'sun' : 'moon'" class="h-5 w-5 flex-shrink-0" />
        <span class="sidebar-label" :class="collapsed ? 'sidebar-label-collapsed' : ''">{{ isDark ? '浅色模式' : '深色模式' }}</span>
      </button>
      <button
        type="button"
        class="sidebar-link hidden w-full lg:flex"
        :class="collapsed ? 'sidebar-link-collapsed' : ''"
        :title="collapsed ? '展开' : undefined"
        @click="setSidebarCollapsed(!appState.sidebarCollapsed)"
      >
        <AppIcon :name="collapsed ? 'chevronRight' : 'chevronLeft'" class="h-5 w-5 flex-shrink-0" />
        <span class="sidebar-label" :class="collapsed ? 'sidebar-label-collapsed' : ''">{{ collapsed ? '展开' : '收起' }}</span>
      </button>
    </div>
  </aside>
</template>
