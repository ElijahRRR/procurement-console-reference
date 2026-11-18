/** 剪贴板：复制一段文字（买家号 ID、工作区 ID）。 */

/**
 * 输入：要复制的文字 → 输出：是否复制成功。
 * 先用 navigator.clipboard；非安全上下文或没有剪贴板权限时退回到选中隐藏文本框再 execCommand('copy')。
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // 落到下面的退路
  }
  if (typeof document === 'undefined') return false
  const el = document.createElement('textarea')
  el.value = text
  el.setAttribute('readonly', '')
  el.style.position = 'fixed'
  el.style.opacity = '0'
  document.body.appendChild(el)
  el.select()
  try {
    return typeof document.execCommand === 'function' && document.execCommand('copy')
  } catch {
    return false
  } finally {
    el.remove()
  }
}
