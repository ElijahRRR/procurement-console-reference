<script setup lang="ts">
/** 顶栏（.glass，64px，粘性）：页面标题与副标题（路由 meta）、模拟模式标记、工作区、用户菜单（退出登录）。 */
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppIcon from '@/components/AppIcon.vue'
import * as auth from '@/api/auth'
import { ApiError } from '@/api/client'
import { appState } from '@/stores/app'
import { displayName, session, setMe } from '@/stores/session'
import { MEMBER_ROLE_LABEL, labelOf } from '@/utils/labels'
import { notifyFailure } from '@/utils/feedback'

const route = useRoute()
const router = useRouter()

const title = computed(() => route.meta.title ?? '')
const subtitle = computed(() => route.meta.subtitle ?? '')
const name = computed(() => displayName(session.me))
const initial = computed(() => (name.value ? name.value.slice(0, 1).toUpperCase() : '·'))
const role = computed(() => labelOf(MEMBER_ROLE_LABEL, session.me?.role))

async function onCommand(cmd: string) {
  if (cmd !== 'logout') return
  try {
    await auth.logout()
  } catch (e) {
    // 会话已失效（401）时本来就是退出状态；其他错误提示后仍回到登录页
    if (!(e instanceof ApiError && e.status === 401)) notifyFailure('退出登录', e)
  }
  setMe(null)
  await router.push('/login')
}
</script>

<template>
  <header class="glass sticky top-0 z-30 border-b border-gray-200/50 dark:border-dark-700/50">
    <div class="flex h-16 items-center justify-between gap-2 px-2 sm:px-4 md:px-6">
      <div class="flex min-w-0 items-center gap-2 sm:gap-4">
        <button
          type="button"
          class="flex items-center rounded-lg p-2 text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-900 dark:text-dark-400 dark:hover:bg-dark-800 dark:hover:text-white lg:hidden"
          aria-label="打开导航"
          @click="appState.mobileSidebarOpen = true"
        >
          <AppIcon name="menu" />
        </button>
        <div class="min-w-0">
          <h1 class="truncate text-lg font-semibold text-gray-900 dark:text-white">{{ title }}</h1>
          <p v-if="subtitle" class="hidden truncate text-xs text-gray-500 dark:text-dark-400 sm:block">{{ subtitle }}</p>
        </div>
      </div>
      <div class="flex min-w-0 shrink-0 items-center gap-1 sm:gap-3">
        <div
          v-if="appState.simulationOnly"
          class="flex items-center gap-1.5 rounded-xl bg-amber-100 px-3 py-1.5 text-amber-700 dark:bg-amber-900/40 dark:text-amber-200"
          title="本阶段服务端只接受模拟执行，不会真实下单"
        >
          <AppIcon name="beaker" size="sm" />
          <span class="text-sm font-semibold">模拟模式</span>
        </div>
        <div
          v-if="session.me"
          class="hidden items-center gap-2 rounded-xl bg-primary-50 px-3 py-1.5 dark:bg-primary-900/20 md:flex"
          title="当前工作区"
        >
          <AppIcon name="grid" size="sm" class="text-primary-600 dark:text-primary-400" />
          <span class="max-w-[12rem] truncate text-sm font-semibold text-primary-700 dark:text-primary-300">{{ session.me.workspace.name }}</span>
        </div>
        <el-dropdown v-if="session.me" trigger="click" placement="bottom-end" @command="onCommand">
          <button
            type="button"
            class="flex items-center gap-2 rounded-xl p-1.5 transition-colors hover:bg-gray-100 dark:hover:bg-dark-800"
            aria-label="用户菜单"
          >
            <div
              class="flex h-8 w-8 items-center justify-center overflow-hidden rounded-xl bg-gradient-to-br from-primary-500 to-primary-600 text-sm font-medium text-white shadow-sm"
            >
              <span>{{ initial }}</span>
            </div>
            <div class="hidden text-left md:block">
              <div class="text-sm font-medium text-gray-900 dark:text-white">{{ name }}</div>
              <div class="text-xs text-gray-500 dark:text-dark-400">{{ role }}</div>
            </div>
            <AppIcon name="chevronDown" size="sm" class="hidden text-gray-400 dark:text-dark-500 md:block" />
          </button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="logout">
                <span class="flex items-center gap-2"><AppIcon name="login" size="sm" />退出登录</span>
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </div>
  </header>
</template>
