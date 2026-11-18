/**
 * 页面可见时按固定间隔执行一个函数（列表自动刷新用）：浏览器标签页切到后台、或所在的页签没有激活时暂停，
 * 回到前台或重新激活时立刻补一次。组件卸载时停止。函数自己负责不重叠（上一次还没回来时直接返回）与不打扰（失败不弹通知）。
 */
import { onBeforeUnmount, onMounted, watch } from 'vue'

export const AUTO_REFRESH_MS = 10_000

export interface VisiblePollingOptions {
  /** 间隔毫秒数，缺省 10 秒 */
  intervalMs?: number
  /** 是否启用（如所在页签是否激活）；不给 = 一直启用。由 false 变 true 时立刻执行一次 */
  enabled?: () => boolean
}

/** 输入：要执行的函数、选项 → 输出：无（随组件挂载开始、卸载结束） */
export function useVisiblePolling(fn: () => void | Promise<void>, options: VisiblePollingOptions = {}): void {
  const { intervalMs = AUTO_REFRESH_MS, enabled = () => true } = options
  let timer: ReturnType<typeof setInterval> | undefined
  const visible = () => typeof document === 'undefined' || document.visibilityState !== 'hidden'
  const tick = () => {
    if (enabled() && visible()) void fn()
  }
  onMounted(() => {
    timer = setInterval(tick, intervalMs)
    document.addEventListener('visibilitychange', tick)
  })
  onBeforeUnmount(() => {
    clearInterval(timer)
    document.removeEventListener('visibilitychange', tick)
  })
  watch(enabled, (on, was) => {
    if (on && !was) tick()
  })
}
