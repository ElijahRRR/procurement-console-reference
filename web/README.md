# 采购控制台 · 前端

Vue 3 + TypeScript + Vite + Element Plus 2.14 + Tailwind CSS 3，外观按 `src/design-system/`（EasyQuick）实现。接口形状以 `docs/development/开发契约-v2.md` §8、§10 为准，类型在 `src/api/types.ts`。

```bash
npm ci            # 依赖（.npmrc 关闭了自动安装 peer，所需 peer 已显式列出）
npm run dev       # 127.0.0.1:5173，/api 与 /plugin 代理到 127.0.0.1:8790（PC_BACKEND_URL 可改）
npm run build     # 生成 design token → vue-tsc 类型检查 → vite build 到 dist/（由 python cli.py serve 托管）
npm test          # vitest：格式化、标签表覆盖全部后端枚举、接口客户端错误映射、路由守卫、token 生成
npm run shots -- <目录>   # 先 build；Playwright 截图（浅色 / 深色），所有接口由 tests/mocks 模拟
```

## 目录

```
scripts/gen-design-tokens.mjs   tokens.json → src/design-system/generated/{tokens.css, easyquick.preset.cjs}
src/api/                        fetch 客户端（ApiError、401 → 登录、needs_setup → 首次设置）与按领域分的接口模块
src/router/                     路由表（meta.title / subtitle）与守卫判断（guard.ts 为纯函数）
src/components/                 外壳（layout/）、AppIcon、PageHeader、StatCard、EmptyState、StatusDot 与各页面部件
src/views/                      概览、待采购、采购记录、异常处理、订单来源、账号与设备、设置、登录、首次设置
src/utils/                      format（金额、日期、中文排版）、labels（枚举 → 中文与 el-tag 类型）、feedback、guard
tests/unit/                     vitest 单测
tests/mocks/                    虚构的接口数据与模拟后端（截图与评审复用）
tests/visual/screenshots.ts     截图脚本（Node 直接运行 .ts）
```

## 约定

- 样式引入顺序见 `src/main.ts`，不能改。Tailwind 只生成工具类（preflight 关闭，基础重置在 `bundle.css`）。
- 深色模式同时切换 `html.dark` 与 `html[data-theme=dark]`，选择记在 `localStorage['pc-theme']`。
- 任务详情的操作按钮只由后端返回的 `allowed_actions` 决定。
- 图标只用 `AppIcon`；颜色只用色阶类或 Element Plus 的 `type`，浅色与深色成对写。
- 仓库公开：mocks 里只放虚构数据。
