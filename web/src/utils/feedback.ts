/**
 * 操作反馈：成功 / 失败通知与确认框，统一文案规则（失败「动作失败：原因」；确认「确定要……吗？」；取消按钮叫「取消」）。
 */
import { h } from 'vue'
import { ElMessage, ElMessageBox, ElNotification } from 'element-plus'
import { failureText } from './format'
import type { Summary } from './results'

/** 输入：动作名（如「保存账号」）、错误 → 输出：无（右上角错误通知） */
export function notifyFailure(action: string, err: unknown): void {
  ElNotification({ type: 'error', title: failureText(action, err), message: '', duration: 6000 })
}

/** 输入：标题、可选说明 → 输出：无（右上角成功通知） */
export function notifySuccess(title: string, message = ''): void {
  ElNotification({ type: 'success', title, message, duration: 3000 })
}

/**
 * 输入：整理好的结果提示（utils/results）、类型 → 输出：无。
 * 有 warnings 时用警告样式并逐条列出（文本节点，不解析 HTML）；没有时为成功提示。
 */
export function notifySummary(s: Summary, type?: 'success' | 'warning' | 'error'): void {
  const kind = type ?? (s.warnings.length ? 'warning' : 'success')
  const body = s.warnings.length
    ? h('div', { class: 'space-y-1' }, [
        s.message ? h('p', s.message) : null,
        h(
          'ul',
          { class: 'list-disc pl-4' },
          s.warnings.map((w) => h('li', w)),
        ),
      ])
    : s.message
  ElNotification({ type: kind, title: s.title, message: body, duration: s.warnings.length ? 0 : 4000 })
}

/** 输入：动作名（如「导出」）、下载函数 → 输出：无（失败时提示「动作失败：原因」） */
export async function runDownload(action: string, fn: () => Promise<unknown>): Promise<void> {
  try {
    await fn()
  } catch (e) {
    notifyFailure(action, e)
  }
}

/** 输入：一句话 → 输出：无（轻提示） */
export function toast(message: string, type: 'success' | 'info' | 'warning' | 'error' = 'success'): void {
  ElMessage({ message, type, grouping: true })
}

export interface ConfirmOptions {
  /** 「确定要……吗？」 */
  message: string
  title?: string
  confirmText?: string
  danger?: boolean
}

/** 输入：确认文案 → 输出：用户点了确定为 true，取消或关闭为 false */
export async function confirmAction(opts: ConfirmOptions): Promise<boolean> {
  try {
    await ElMessageBox.confirm(opts.message, opts.title ?? '请确认', {
      confirmButtonText: opts.confirmText ?? '确定',
      cancelButtonText: '取消',
      type: opts.danger ? 'warning' : undefined,
      confirmButtonClass: opts.danger ? 'el-button--danger' : undefined,
      autofocus: false,
    })
    return true
  } catch {
    return false
  }
}

/** 输入：提示文案、标题、输入校验 → 输出：输入的文字；取消为 null */
export async function promptText(opts: {
  message: string
  title: string
  placeholder?: string
  required?: boolean
  confirmText?: string
  danger?: boolean
}): Promise<string | null> {
  try {
    const res: unknown = await ElMessageBox.prompt(opts.message, opts.title, {
      confirmButtonText: opts.confirmText ?? '确定',
      cancelButtonText: '取消',
      inputPlaceholder: opts.placeholder,
      confirmButtonClass: opts.danger ? 'el-button--danger' : undefined,
      inputValidator: opts.required ? (v: string) => (v && v.trim() ? true : '请填写内容') : undefined,
    })
    const value = res && typeof res === 'object' && 'value' in res ? (res as { value?: unknown }).value : ''
    return String(value ?? '').trim()
  } catch {
    return null
  }
}
