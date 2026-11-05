/**
 * 外壳用到的全局标记：是否只允许模拟执行（顶栏显示「模拟模式」）、侧边栏是否收起。
 * 侧边栏收起状态记在 localStorage（读写失败时忽略）。
 */
import { reactive } from 'vue'
import * as settingsApi from '@/api/settings'

const COLLAPSE_KEY = 'pc-sidebar-collapsed'

function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(COLLAPSE_KEY) === '1'
  } catch {
    return false
  }
}

export const appState = reactive({
  /** null = 尚未读到 */
  simulationOnly: null as boolean | null,
  sidebarCollapsed: readCollapsed(),
  mobileSidebarOpen: false,
})

/** 输入：是否收起 → 输出：无 */
export function setSidebarCollapsed(collapsed: boolean): void {
  appState.sidebarCollapsed = collapsed
  try {
    window.localStorage.setItem(COLLAPSE_KEY, collapsed ? '1' : '0')
  } catch {
    // 存储不可用时只在本次会话内生效
  }
}

/** 输入：无 → 输出：无（读取设置里的 simulation_only；失败时保持 null，不假设任何值） */
export async function loadAppFlags(): Promise<void> {
  try {
    const s = await settingsApi.getSettings()
    appState.simulationOnly = !!s.simulation_only
  } catch {
    appState.simulationOnly = null
  }
}
