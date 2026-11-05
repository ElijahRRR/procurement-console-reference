#!/usr/bin/env node
/**
 * 从 src/design-system/tokens.json 生成两份文件（predev / prebuild / pretest 自动执行）：
 *
 *   src/design-system/generated/tokens.css         每个 token 一个 CSS 变量 --<token 名>；
 *                                                  浅色值写在 :root，深色值（与浅色不同的）写在 [data-theme="dark"]；
 *                                                  别名 "{gray-50}" 写成 var(--gray-50)。
 *   src/design-system/generated/easyquick.preset.cjs  Tailwind 3 预设：颜色读 CSS 变量（color-mix 编码透明度，
 *                                                  与 bundle.css 的写法一致），间距、圆角、投影、字体、渐变、模糊读同名变量。
 *
 * 只读 tokens.json，不改 design-system 里的任何源文件。输出稳定（同样的输入得到同样的字节），可以直接提交。
 */
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const dsDir = resolve(here, '../src/design-system')
const outDir = resolve(dsDir, 'generated')
const tokens = JSON.parse(readFileSync(resolve(dsDir, 'tokens.json'), 'utf8'))

/** 输入：token 名 → 输出：CSS 自定义属性名（点号转义，如 space-2.5 → --space-2\.5）。 */
function cssVarName(name) {
  return `--${name.replace(/\./g, '\\.')}`
}

/** 输入：token 值（可能是 "{gray-50}" 别名）→ 输出：CSS 值。 */
function cssValue(value) {
  const m = /^\{([a-z0-9.-]+)\}$/i.exec(String(value).trim())
  return m ? `var(${cssVarName(m[1])})` : String(value)
}

/** 输入：token 的 value 字段（字符串或 {light, dark}）→ 输出：{light, dark}。 */
function themed(value) {
  if (value && typeof value === 'object') {
    const light = value.light ?? value.dark
    return { light, dark: value.dark ?? light }
  }
  return { light: value, dark: value }
}

const light = []
const dark = []
const push = (name, value) => {
  const v = themed(value)
  light.push([name, cssValue(v.light)])
  if (String(v.dark) !== String(v.light)) dark.push([name, cssValue(v.dark)])
}

const colorTokens = tokens.color.tokens
for (const t of colorTokens) push(t.name, t.value)
for (const group of ['spacing', 'radius', 'shadow', 'gradient', 'blur']) {
  for (const t of tokens[group].tokens) push(t.name, t.value)
}
for (const [family, stack] of Object.entries(tokens.type.families)) push(`font-${family}`, stack)
for (const group of tokens.type.groups) {
  for (const s of group.styles) {
    push(`type-${s.name}-size`, s.fontSize)
    push(`type-${s.name}-line-height`, s.lineHeight)
    push(`type-${s.name}-weight`, String(s.fontWeight))
    if (s.letterSpacing) push(`type-${s.name}-letter-spacing`, s.letterSpacing)
  }
}

const block = (selector, entries) =>
  `${selector} {\n${entries.map(([n, v]) => `  ${cssVarName(n)}: ${v};`).join('\n')}\n}\n`

const header = `/* 由 scripts/gen-design-tokens.mjs 从 tokens.json 生成，不要手改。${tokens.name} v${tokens.version} */\n`
const css = `${header}${block(':root', light)}\n${block('[data-theme="dark"]', dark)}`

// ── Tailwind 预设 ──────────────────────────────────────────────────────────────

const SCALE_RE = /^([a-z]+)-(50|[1-9]00|950)$/
const scales = {}
const semantic = {}
for (const t of colorTokens) {
  const m = SCALE_RE.exec(t.name)
  if (m) (scales[m[1]] ||= []).push(m[2])
  else semantic[t.name] = true
}

// 语义 token 分组：bg-* → backgroundColor，text-* → textColor，border-* → borderColor，其余进 colors
const bgSemantic = Object.keys(semantic).filter((n) => n.startsWith('bg-'))
const textSemantic = Object.keys(semantic).filter((n) => n.startsWith('text-'))
const borderSemantic = Object.keys(semantic).filter((n) => n.startsWith('border-'))
const otherSemantic = Object.keys(semantic).filter(
  (n) => !n.startsWith('bg-') && !n.startsWith('text-') && !n.startsWith('border-'),
)

const q = (s) => JSON.stringify(s)
const lines = []
lines.push(`/* 由 scripts/gen-design-tokens.mjs 从 tokens.json 生成，不要手改。${tokens.name} v${tokens.version} */`)
lines.push(`/* eslint-disable */`)
lines.push(`'use strict'`)
lines.push('')
lines.push(`/** 输入：token 名 → 输出：Tailwind 颜色函数；透明度按 bundle.css 的写法编码为 color-mix。 */`)
lines.push(`function v(name) {`)
lines.push(`  const ref = 'var(--' + name.replace(/\\./g, '\\\\.') + ')'`)
lines.push(`  return function color(opts) {`)
lines.push(`    const o = opts || {}`)
lines.push(`    if (o.opacityVariable && (o.opacityValue === undefined || o.opacityValue === 'var(' + o.opacityVariable + ')')) {`)
lines.push(`      return 'color-mix(in srgb, ' + ref + ' calc(var(' + o.opacityVariable + ', 1) * 100%), transparent)'`)
lines.push(`    }`)
lines.push(`    if (o.opacityValue !== undefined && o.opacityValue !== null) {`)
lines.push(`      return 'color-mix(in srgb, ' + ref + ' calc(' + o.opacityValue + ' * 100%), transparent)'`)
lines.push(`    }`)
lines.push(`    return ref`)
lines.push(`  }`)
lines.push(`}`)
lines.push('')
lines.push(`const ref = (name) => 'var(--' + name.replace(/\\./g, '\\\\.') + ')'`)
lines.push('')
lines.push(`const colors = {`)
lines.push(`  transparent: 'transparent',`)
lines.push(`  current: 'currentColor',`)
lines.push(`  inherit: 'inherit',`)
lines.push(`  white: '#ffffff',`)
lines.push(`  black: '#000000',`)
for (const [scale, steps] of Object.entries(scales)) {
  lines.push(`  ${q(scale)}: {`)
  for (const step of steps) lines.push(`    ${q(step)}: v(${q(`${scale}-${step}`)}),`)
  lines.push(`  },`)
}
for (const n of otherSemantic) lines.push(`  ${q(n)}: v(${q(n)}),`)
lines.push(`}`)
lines.push('')
lines.push(`module.exports = {`)
lines.push(`  darkMode: ['variant', '&:where([data-theme="dark"], [data-theme="dark"] *)'],`)
lines.push(`  corePlugins: { preflight: false },`)
lines.push(`  theme: {`)
lines.push(`    colors,`)
lines.push(`    extend: {`)
lines.push(`      backgroundColor: {`)
for (const n of bgSemantic) lines.push(`        ${q(n.slice(3))}: v(${q(n)}),`)
lines.push(`      },`)
lines.push(`      textColor: {`)
for (const n of textSemantic) lines.push(`        ${q(n.slice(5))}: v(${q(n)}),`)
lines.push(`      },`)
lines.push(`      borderColor: {`)
for (const n of borderSemantic) lines.push(`        ${q(n.slice(7))}: v(${q(n)}),`)
lines.push(`      },`)
lines.push(`      spacing: {`)
for (const t of tokens.spacing.tokens) lines.push(`        ${q(t.name.replace(/^space-/, ''))}: ref(${q(t.name)}),`)
lines.push(`      },`)
lines.push(`      borderRadius: {`)
for (const t of tokens.radius.tokens) {
  const key = t.name === 'radius' ? 'DEFAULT' : t.name.replace(/^radius-/, '')
  lines.push(`        ${q(key)}: ref(${q(t.name)}),`)
}
lines.push(`      },`)
lines.push(`      boxShadow: {`)
for (const t of tokens.shadow.tokens) lines.push(`        ${q(t.name.replace(/^shadow-/, ''))}: ref(${q(t.name)}),`)
lines.push(`      },`)
lines.push(`      backgroundImage: {`)
for (const t of tokens.gradient.tokens) {
  // gradient-mesh 在 bundle.css / 预览里写作 bg-mesh-gradient，其余保持 bg-gradient-*
  const key = t.name === 'gradient-mesh' ? 'mesh-gradient' : t.name
  lines.push(`        ${q(key)}: ref(${q(t.name)}),`)
}
lines.push(`      },`)
lines.push(`      backdropBlur: {`)
for (const t of tokens.blur.tokens) lines.push(`        ${q(t.name.replace(/^blur-/, ''))}: ref(${q(t.name)}),`)
lines.push(`      },`)
lines.push(`      blur: {`)
for (const t of tokens.blur.tokens) lines.push(`        ${q(t.name.replace(/^blur-/, ''))}: ref(${q(t.name)}),`)
lines.push(`      },`)
lines.push(`      fontFamily: {`)
for (const family of Object.keys(tokens.type.families)) lines.push(`        ${q(family)}: [ref(${q(`font-${family}`)})],`)
lines.push(`      },`)
lines.push(`    },`)
lines.push(`  },`)
lines.push(`}`)
lines.push('')

mkdirSync(outDir, { recursive: true })
const write = (file, content) => {
  const path = resolve(outDir, file)
  let old = null
  try {
    old = readFileSync(path, 'utf8')
  } catch {
    old = null
  }
  if (old !== content) writeFileSync(path, content)
}
write('tokens.css', css)
write('easyquick.preset.cjs', lines.join('\n'))
console.log(`design tokens: ${light.length} 个变量（深色覆盖 ${dark.length} 个）→ src/design-system/generated/`)
