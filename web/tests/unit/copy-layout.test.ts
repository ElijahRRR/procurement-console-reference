/**
 * 静态检查（读源文件）：文案规则与窄屏布局的约定不被后来的改动破坏。
 * - 确认框一律「确定要……吗？」；不用感叹号。
 * - 页面标题（PageHeader）不重复顶栏已显示的页面名（EasyQuick AppLayout：顶栏是分区，页面标题是本页内容）。
 * - 分段切换器放在可横向滚动的容器里；固定宽度的弹窗在窄屏下不超出视口。
 */
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const src = resolve(process.cwd(), 'src')
function files(dir: string, ext: RegExp): string[] {
  return readdirSync(dir).flatMap((f) => {
    const p = join(dir, f)
    if (p.includes('design-system')) return []
    return statSync(p).isDirectory() ? files(p, ext) : ext.test(f) ? [p] : []
  })
}
const sources = files(src, /\.(vue|ts)$/).map((p) => ({ p, text: readFileSync(p, 'utf8') }))

describe('文案规则', () => {
  it('确认文案都是「确定要……吗？」', () => {
    const bad: string[] = []
    for (const { p, text } of sources) {
      for (const m of text.matchAll(/message:\s*[`'"](确定[^`'"]*)/g)) if (!m[1].startsWith('确定要')) bad.push(`${p}: ${m[1]}`)
    }
    expect(bad).toEqual([])
  })
  it('不用全角或半角感叹号', () => {
    const bad = sources.filter(({ text }) => /[！]/.test(text) || /[\u4e00-\u9fff]!/.test(text)).map(({ p }) => p)
    expect(bad).toEqual([])
  })
  it('按钮不用名词：问题行要写成「下载问题行」，读取记录写成「查看读取记录」', () => {
    const all = sources.map(({ text }) => text).join('\n')
    expect(all).not.toMatch(/>\s*问题行\s*</)
    expect(all).not.toMatch(/\/>读取记录</)
  })
  it('导入模板是工作区共用的（v2 §4、契约 §7），不叫「个人模板」', () => {
    const bad = sources.filter(({ text }) => text.includes('个人模板')).map(({ p }) => p)
    expect(bad).toEqual([])
  })
})

describe('页面标题不重复顶栏', () => {
  it('每个页面的 PageHeader 标题与路由 meta.title 不同', () => {
    const router = readFileSync(join(src, 'router/index.ts'), 'utf8')
    const titles = new Map<string, string>()
    for (const m of router.matchAll(/import\('@\/views\/(\w+)\.vue'\),\s*meta: \{[^}]*title: '([^']+)'/g)) titles.set(m[1], m[2])
    expect(titles.size).toBeGreaterThanOrEqual(7)
    for (const [view, title] of titles) {
      const text = readFileSync(join(src, 'views', `${view}.vue`), 'utf8')
      const literal = /<PageHeader[^>]*\stitle="([^"]+)"/.exec(text)?.[1]
      if (literal !== undefined) expect(`${view}: ${literal}`).not.toBe(`${view}: ${title}`)
    }
  })
})

describe('窄屏（手机 390px）布局', () => {
  it('每个 el-segmented 都包在可横向滚动的容器里', () => {
    for (const { p, text } of sources.filter(({ p }) => p.endsWith('.vue'))) {
      const lines = text.split('\n')
      lines.forEach((line, i) => {
        if (line.includes('<el-segmented')) expect(`${p}:${i + 1} ${lines[i - 1]}`).toContain('pc-segmented-scroll')
      })
    }
  })
  it('弹窗最大宽度限制在视口内', () => {
    const css = readFileSync(join(src, 'styles/app.css'), 'utf8')
    expect(css).toMatch(/\.el-dialog\s*\{\s*max-width:\s*calc\(100vw - 2rem\);/)
    expect(css).toMatch(/\.pc-segmented-scroll\s*\{[^}]*overflow-x:\s*auto/)
  })
})
