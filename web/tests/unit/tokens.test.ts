/** 设计 token 生成结果：变量命名、深色覆盖、Tailwind 颜色函数与 bundle.css 的 color-mix 写法一致。 */
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const web = process.cwd()
const css = readFileSync(resolve(web, 'src/design-system/generated/tokens.css'), 'utf8')
const tokens = JSON.parse(readFileSync(resolve(web, 'src/design-system/tokens.json'), 'utf8'))
const bundle = readFileSync(resolve(web, 'src/design-system/bundle.css'), 'utf8')
const require = createRequire(import.meta.url)
const preset = require(resolve(web, 'src/design-system/generated/easyquick.preset.cjs'))

const [lightBlock, darkBlock] = css.split('[data-theme="dark"]')

describe('tokens.css', () => {
  it('每个 token 都是一个 CSS 变量', () => {
    const names: string[] = [
      ...tokens.color.tokens.map((t: { name: string }) => t.name),
      ...['spacing', 'radius', 'shadow', 'gradient', 'blur'].flatMap((g) => tokens[g].tokens.map((t: { name: string }) => t.name)),
    ]
    for (const n of names) expect(lightBlock).toContain(`--${n.replace(/\./g, '\\.')}:`)
    expect(lightBlock).toContain('--font-sans:')
    expect(lightBlock).toContain('--font-mono:')
  })
  it('别名写成 var()，点号转义', () => {
    expect(lightBlock).toContain('--bg-page: var(--gray-50);')
    expect(lightBlock).toContain('--space-2\\.5: 0.625rem;')
  })
  it('深色块只放与浅色不同的值', () => {
    expect(darkBlock).toContain('--bg-page: var(--dark-950);')
    expect(darkBlock).toContain('--text-strong: #ffffff;')
    expect(darkBlock).not.toContain('--primary-500')
    expect(darkBlock).not.toContain('--brand:')
  })
  it('bundle.css 与 Element Plus 主题用到的变量都有定义', () => {
    const theme = readFileSync(resolve(web, 'src/design-system/easyquick-element.css'), 'utf8')
    const used = new Set([...`${bundle}\n${theme}`.matchAll(/var\((--[a-z0-9\\.-]+)/gi)].map((m) => m[1]))
    for (const v of used) {
      if (v.startsWith('--tw-') || v.startsWith('--el-') || v.startsWith('--eq-')) continue
      expect(css, `缺少 ${v}`).toContain(`${v}:`)
    }
  })
})

describe('Tailwind 预设', () => {
  it('关闭 preflight，深色按 data-theme 选择器', () => {
    expect(preset.corePlugins.preflight).toBe(false)
    expect(preset.darkMode).toEqual(['variant', '&:where([data-theme="dark"], [data-theme="dark"] *)'])
  })
  it('颜色函数与 bundle.css 的写法一致', () => {
    const gray50 = preset.theme.colors.gray['50']
    const withVar = gray50({ opacityVariable: '--tw-bg-opacity', opacityValue: 'var(--tw-bg-opacity)' })
    expect(withVar).toBe('color-mix(in srgb, var(--gray-50) calc(var(--tw-bg-opacity, 1) * 100%), transparent)')
    expect(bundle).toContain(withVar)
    const p20 = preset.theme.colors.primary['900']({ opacityValue: '0.2' })
    expect(p20).toBe('color-mix(in srgb, var(--primary-900) calc(0.2 * 100%), transparent)')
    expect(bundle).toContain(p20)
    expect(preset.theme.colors.primary['500']({})).toBe('var(--primary-500)')
  })
  it('间距、圆角、投影、渐变读同名变量', () => {
    expect(preset.theme.extend.spacing['2.5']).toBe('var(--space-2\\.5)')
    expect(preset.theme.extend.borderRadius.xl).toBe('var(--radius-xl)')
    expect(preset.theme.extend.borderRadius.DEFAULT).toBe('var(--radius)')
    expect(preset.theme.extend.boxShadow.card).toBe('var(--shadow-card)')
    expect(preset.theme.extend.backgroundImage['mesh-gradient']).toBe('var(--gradient-mesh)')
    expect(preset.theme.extend.backgroundImage['gradient-primary']).toBe('var(--gradient-primary)')
    expect(preset.theme.extend.fontFamily.mono).toEqual(['var(--font-mono)'])
  })
})
