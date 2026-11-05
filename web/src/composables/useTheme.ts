/**
 * 明暗主题：同时切换 <html class="dark">（Element Plus）与 <html data-theme="dark">（EasyQuick 样式类与 dark: 前缀），
 * 记住用户选择（localStorage，读写失败时忽略）。index.html 的首屏脚本用同一个键。
 */
import { ref } from 'vue'

export const THEME_KEY = 'pc-theme'

const isDark = ref(typeof document !== 'undefined' && document.documentElement.classList.contains('dark'))

/** 输入：是否深色 → 输出：无（改 html 的 class 与 data-theme） */
export function applyTheme(dark: boolean): void {
  const root = document.documentElement
  root.classList.toggle('dark', dark)
  if (dark) root.setAttribute('data-theme', 'dark')
  else root.removeAttribute('data-theme')
  isDark.value = dark
}

/** 输入：无 → 输出：{isDark, toggle, set} */
export function useTheme() {
  const set = (dark: boolean) => {
    applyTheme(dark)
    try {
      window.localStorage.setItem(THEME_KEY, dark ? 'dark' : 'light')
    } catch {
      // 隐私模式等情况下存储不可用：本次会话内仍然生效
    }
  }
  const toggle = () => set(!isDark.value)
  return { isDark, toggle, set }
}
