/**
 * 视觉检查：启动 vite preview（需先 npm run build），用 Playwright 打开各页面，所有 /api/v1 请求由
 * tests/mocks/handlers.ts 模拟，浅色与深色各截一套图。
 *
 * 用法：node tests/visual/screenshots.ts [输出目录]
 *   输出目录默认 ./screenshots；环境变量 SHOTS_ONLY=overview,orders 只截部分页面。
 *   名字以 phone- 开头的场景用 390 × 844 视口，页面出现横向滚动即判失败。
 * Playwright 不在本项目依赖里：用全局安装的 playwright（require('playwright')）。
 */
import { spawn, type ChildProcess } from 'node:child_process'
import { mkdirSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createMockApi, type MockOptions } from '../mocks/handlers.ts'

const require = createRequire(import.meta.url)
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { chromium } = require('playwright') as typeof import('playwright')

const webDir = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const outDir = resolve(process.argv[2] || resolve(webDir, 'screenshots'))
const PORT = Number(process.env.SHOTS_PORT || 4179)
const BASE = `http://127.0.0.1:${PORT}`
const only = (process.env.SHOTS_ONLY || '').split(',').filter(Boolean)

type Page = import('playwright').Page
type Browser = import('playwright').Browser

async function waitForServer(url: string, timeoutMs = 30_000): Promise<void> {
  const start = Date.now()
  while (Date.now() - start < timeoutMs) {
    try {
      const res = await fetch(url)
      if (res.ok) return
    } catch {
      // 还没起来
    }
    await new Promise((r) => setTimeout(r, 300))
  }
  throw new Error(`vite preview 在 ${timeoutMs}ms 内没有响应：${url}`)
}

function startPreview(): ChildProcess {
  const child = spawn(resolve(webDir, 'node_modules/.bin/vite'), ['preview', '--port', String(PORT), '--strictPort', '--host', '127.0.0.1'], {
    cwd: webDir,
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  child.stderr?.on('data', (d) => process.stderr.write(d))
  return child
}

const DESKTOP = { width: 1440, height: 900 }
const PHONE = { width: 390, height: 844 }

async function newPage(browser: Browser, theme: 'light' | 'dark', opts: MockOptions = {}, viewport = DESKTOP) {
  const context = await browser.newContext({
    viewport,
    deviceScaleFactor: 1,
    colorScheme: theme,
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
  })
  await context.addInitScript((t: string) => {
    try {
      window.localStorage.setItem('pc-theme', t)
    } catch {
      // 忽略
    }
  }, theme)
  const api = createMockApi(opts)
  const page = await context.newPage()
  const problems: string[] = []
  page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`))
  page.on('console', (m) => {
    if (m.type() === 'error' || m.type() === 'warning') problems.push(`console.${m.type()}: ${m.text()}`)
  })
  await page.route('**/api/v1/**', async (route) => {
    const req = route.request()
    const url = new URL(req.url())
    let body: unknown = null
    const ct = req.headers()['content-type'] || ''
    if (ct.includes('application/json')) {
      try {
        body = req.postDataJSON()
      } catch {
        body = null
      }
    }
    const res = api.handle(req.method(), url, body)
    if (res.status === 204) return route.fulfill({ status: 204, body: '' })
    return route.fulfill({ status: res.status, contentType: 'application/json', body: JSON.stringify(res.body) })
  })
  return { context, page, problems }
}

/** 输入：页面 → 输出：无；页面比视口宽（出现横向滚动）时抛错，并列出超出视口的元素 */
async function assertNoHorizontalScroll(page: Page) {
  const res = await page.evaluate(() => {
    const vw = document.documentElement.clientWidth
    const sw = Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)
    const wide: string[] = []
    if (sw > vw) {
      for (const el of Array.from(document.querySelectorAll('body *'))) {
        const r = el.getBoundingClientRect()
        if (r.right > vw + 1 && r.width > 0) {
          // 只报最外层的：父元素没有超出
          const parent = el.parentElement?.getBoundingClientRect()
          if (!parent || parent.right <= vw + 1) wide.push(`${el.tagName.toLowerCase()}.${(el.getAttribute('class') ?? '').split(' ').slice(0, 3).join('.')} → ${Math.round(r.right)}px`)
        }
        if (wide.length >= 8) break
      }
    }
    return { vw, sw, wide }
  })
  if (res.sw > res.vw) throw new Error(`页面宽 ${res.sw}px，超出视口 ${res.vw}px：${res.wide.join('；')}`)
}

async function settle(page: Page) {
  await page.waitForLoadState('networkidle')
  await page.waitForTimeout(450)
}

async function shot(page: Page, name: string, fullPage = true, viewport = DESKTOP) {
  const file = resolve(outDir, `${name}.png`)
  if (fullPage) {
    // 固定定位的侧边栏与背景只覆盖视口：把视口拉到内容高度再截，整页图才和真实浏览一致
    const height = await page.evaluate(() => document.documentElement.scrollHeight)
    await page.setViewportSize({ width: viewport.width, height: Math.max(viewport.height, Math.min(height, 4000)) })
    await page.waitForTimeout(250)
  }
  await page.screenshot({ path: file })
  console.log(`  ${file}`)
}

interface Scene {
  name: string
  run: (page: Page) => Promise<void>
  opts?: MockOptions
  fullPage?: boolean
  themes?: ('light' | 'dark')[]
}

const scenes: Scene[] = [
  {
    name: 'overview',
    run: async (page) => {
      await page.goto(`${BASE}/`)
      await page.waitForSelector('.stat-card')
    },
  },
  {
    name: 'orders',
    run: async (page) => {
      await page.goto(`${BASE}/orders?view=today`)
      await page.waitForSelector('.el-table__row')
    },
  },
  {
    name: 'orders-needs-info',
    themes: ['light'],
    run: async (page) => {
      await page.goto(`${BASE}/orders?view=needs_info`)
      await page.waitForSelector('.el-table__row')
    },
  },
  {
    name: 'order-drawer',
    fullPage: false,
    run: async (page) => {
      await page.goto(`${BASE}/exceptions?view=exception`)
      await page.waitForSelector('.el-table__row')
      await page.locator('.el-table__row').first().click()
      await page.waitForSelector('.el-drawer .pc-section')
    },
  },
  {
    name: 'order-drawer-attempts',
    fullPage: false,
    run: async (page) => {
      await page.goto(`${BASE}/exceptions?view=exception`)
      await page.waitForSelector('.el-table__row')
      await page.locator('.el-table__row').first().click()
      await page.waitForSelector('.el-drawer .el-timeline')
      await page.locator('.el-drawer .el-timeline').first().scrollIntoViewIfNeeded()
    },
  },
  {
    name: 'order-drawer-conflict',
    fullPage: false,
    themes: ['light'],
    run: async (page) => {
      await page.goto(`${BASE}/orders?view=conflict`)
      await page.waitForSelector('.el-table__row')
      await page.locator('.el-table__row').first().click()
      await page.waitForSelector('.el-drawer .pc-section')
      await page.getByRole('heading', { name: '来源冲突' }).scrollIntoViewIfNeeded()
    },
  },
  {
    name: 'order-drawer-unknown',
    fullPage: false,
    themes: ['light'],
    run: async (page) => {
      await page.goto(`${BASE}/exceptions?view=result_unknown`)
      await page.waitForSelector('.el-table__row')
      await page.locator('.el-table__row').first().click()
      await page.waitForSelector('.el-drawer .pc-section')
      await page.getByRole('button', { name: '核实结果' }).click()
      await page.waitForSelector('text=已按插件上报的回执预填')
    },
  },
  {
    name: 'order-drawer-conflict-external',
    fullPage: false,
    themes: ['light'],
    run: async (page) => {
      await page.goto(`${BASE}/orders?view=conflict`)
      await page.waitForSelector('.el-table__row')
      await page.locator('.el-table__row', { hasText: '格式不对' }).first().click()
      await page.waitForSelector('text=只能保留当前')
      await page.getByRole('heading', { name: '来源冲突' }).scrollIntoViewIfNeeded()
    },
  },
  {
    name: 'records',
    themes: ['light'],
    run: async (page) => {
      await page.goto(`${BASE}/records?view=purchased`)
      await page.waitForSelector('.el-table__row')
    },
  },
  {
    name: 'sources-feishu',
    run: async (page) => {
      await page.goto(`${BASE}/sources?tab=feishu`)
      await page.waitForSelector('.el-card')
    },
  },
  {
    name: 'sources-feishu-unconfigured',
    opts: { feishuConfigured: false },
    themes: ['light'],
    run: async (page) => {
      await page.goto(`${BASE}/sources?tab=feishu`)
      await page.waitForSelector('.empty-state')
    },
  },
  {
    name: 'sources-feishu-wizard',
    fullPage: false,
    run: async (page) => {
      await page.goto(`${BASE}/sources?tab=feishu`)
      await page.getByRole('button', { name: '连接飞书表' }).first().click()
      await page.locator('.el-dialog input').first().fill('https://demo-tenant.feishu.cn/base/bascnDEMO0000000000000003?table=tblDEMOsource03')
      await page.getByRole('button', { name: '下一步' }).click()
      await page.waitForTimeout(300)
      await page.getByRole('button', { name: '下一步' }).click()
      await page.waitForSelector('.el-dialog .el-table__row')
    },
  },
  {
    name: 'sources-feishu-restore-failed',
    themes: ['light'],
    run: async (page) => {
      await page.goto(`${BASE}/sources?tab=feishu`)
      await page.getByRole('button', { name: '写回验收' }).first().click()
      await page.locator('.el-message-box input').fill('recDEMOrestorefail')
      await page.getByRole('button', { name: '开始验收' }).click()
      await page.waitForSelector('text=里还留着测试值')
      // 通知不自动关闭；截卡片上的提示
      await page.locator('.el-notification__closeBtn').first().click().catch(() => {})
    },
  },
  {
    name: 'sources-file',
    run: async (page) => {
      await page.goto(`${BASE}/sources?tab=file`)
      await page.waitForSelector('.el-upload-dragger')
    },
  },
  {
    name: 'sources-file-preview',
    run: async (page) => {
      await page.goto(`${BASE}/sources?tab=file`)
      await page.waitForSelector('.el-upload-dragger')
      await page.locator('input[type=file]').setInputFiles({ name: 'orders-demo-today.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: Buffer.from('PK demo') })
      await page.getByRole('button', { name: '生成预览' }).click()
      await page.waitForSelector('text=提交导入')
    },
  },
  {
    name: 'sources-writebacks',
    themes: ['light'],
    run: async (page) => {
      await page.goto(`${BASE}/sources?tab=writebacks`)
      await page.waitForSelector('.el-table__row')
    },
  },
  {
    name: 'accounts',
    run: async (page) => {
      await page.goto(`${BASE}/accounts?tab=accounts`)
      await page.waitForSelector('#pane-accounts .el-table__row')
    },
  },
  {
    name: 'accounts-dialog',
    fullPage: false,
    themes: ['light'],
    run: async (page) => {
      await page.goto(`${BASE}/accounts?tab=accounts`)
      await page.waitForSelector('#pane-accounts .el-table__row')
      await page.getByRole('button', { name: '编辑账号' }).first().click()
      await page.waitForSelector('.el-dialog .el-form')
    },
  },
  {
    name: 'devices',
    run: async (page) => {
      await page.goto(`${BASE}/accounts?tab=devices`)
      await page.waitForSelector('#pane-devices .el-table__row')
    },
  },
  {
    name: 'devices-pairing',
    fullPage: false,
    run: async (page) => {
      await page.goto(`${BASE}/accounts?tab=devices`)
      await page.waitForSelector('#pane-devices .el-table__row')
      await page.getByRole('button', { name: '生成配对码' }).first().click()
      await page.waitForSelector('text=DEMO7K2Q')
    },
  },
  {
    name: 'settings',
    run: async (page) => {
      await page.goto(`${BASE}/settings`)
      await page.waitForSelector('.el-card')
    },
  },
  {
    name: 'login',
    opts: { loggedIn: false },
    fullPage: false,
    run: async (page) => {
      await page.goto(`${BASE}/login`)
      await page.waitForSelector('.pc-auth-card')
    },
  },
  {
    name: 'setup',
    opts: { needsSetup: true, loggedIn: false },
    fullPage: false,
    themes: ['light'],
    run: async (page) => {
      await page.goto(`${BASE}/`)
      await page.waitForSelector('text=首次设置')
    },
  },
  // ── 手机宽度（390px）：不出现横向滚动 ──
  ...(
    [
      ['phone-overview', '/', '.stat-card'],
      ['phone-orders', '/orders?view=today', '.el-table__row'],
      ['phone-exceptions', '/exceptions?view=needs_attention', '.el-table__row'],
      ['phone-records', '/records?view=purchased', '.el-table__row'],
      ['phone-sources-feishu', '/sources?tab=feishu', '.el-card'],
      ['phone-sources-file', '/sources?tab=file', '.el-upload-dragger'],
      ['phone-accounts', '/accounts?tab=accounts', '#pane-accounts .el-table__row'],
      ['phone-devices', '/accounts?tab=devices', '#pane-devices .el-table__row'],
      ['phone-settings', '/settings', '.el-card'],
    ] as const
  ).map(
    ([name, path, ready]): Scene => ({
      name,
      themes: ['light'],
      run: async (page) => {
        await page.goto(`${BASE}${path}`)
        await page.waitForSelector(ready)
      },
    }),
  ),
  {
    name: 'phone-order-drawer',
    themes: ['light'],
    fullPage: false,
    run: async (page) => {
      await page.goto(`${BASE}/exceptions?view=result_unknown`)
      await page.waitForSelector('.el-table__row')
      await page.locator('.el-table__row').first().click()
      await page.waitForSelector('.el-drawer .pc-section')
      await page.getByRole('button', { name: '核实结果' }).click()
      await page.waitForSelector('text=已按插件上报的回执预填')
    },
  },
  {
    name: 'phone-order-drawer-conflict',
    themes: ['light'],
    fullPage: false,
    run: async (page) => {
      await page.goto(`${BASE}/orders?view=conflict`)
      await page.waitForSelector('.el-table__row')
      await page.locator('.el-table__row').first().click()
      await page.waitForSelector('.el-drawer .pc-section')
      await page.getByRole('heading', { name: '来源冲突' }).scrollIntoViewIfNeeded()
    },
  },
  {
    name: 'collapsed-sidebar',
    themes: ['dark'],
    fullPage: false,
    run: async (page) => {
      await page.goto(`${BASE}/orders?view=today`)
      await page.waitForSelector('.el-table__row')
      await page.getByRole('button', { name: '收起' }).click()
      await page.waitForTimeout(400)
    },
  },
]

async function main() {
  mkdirSync(outDir, { recursive: true })
  const preview = startPreview()
  let failures = 0
  try {
    await waitForServer(`${BASE}/`)
    const browser = await chromium.launch()
    for (const scene of scenes) {
      if (only.length && !only.includes(scene.name)) continue
      for (const theme of scene.themes ?? (['light', 'dark'] as const)) {
        const phone = scene.name.startsWith('phone-')
        const viewport = phone ? PHONE : DESKTOP
        const { context, page, problems } = await newPage(browser, theme, scene.opts, viewport)
        try {
          await scene.run(page)
          await settle(page)
          if (phone) await assertNoHorizontalScroll(page)
          await shot(page, `${scene.name}-${theme}`, scene.fullPage ?? true, viewport)
        } catch (e) {
          failures += 1
          console.error(`场景 ${scene.name}-${theme} 失败：${e instanceof Error ? e.message : String(e)}`)
          await page.screenshot({ path: resolve(outDir, `${scene.name}-${theme}-FAILED.png`) }).catch(() => {})
        }
        const relevant = problems.filter((p) => !p.includes('favicon'))
        if (relevant.length) console.warn(`  ${scene.name}-${theme} 控制台：\n    ${relevant.join('\n    ')}`)
        await context.close()
      }
    }
    await browser.close()
  } finally {
    preview.kill()
  }
  if (failures) {
    console.error(`${failures} 个场景失败`)
    process.exitCode = 1
  }
}

void main()
