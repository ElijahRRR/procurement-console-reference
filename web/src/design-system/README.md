EasyQuick UI 是一套面向数据密集型后台（订单、商品、库存、报表）的界面规范：青绿主色配深蓝灰暗色，大圆角、轻投影、局部毛玻璃，浅色与深色两套主题等价可用。在 Vue 3 项目里，**交互由 Element Plus 负责，外观由 EasyQuick 主题负责**：表格、表单、弹窗、下拉、日期、分页、通知全部用 Element Plus 组件，`element-plus/easyquick-element.css` 把它们换成本系统的配色、圆角、尺寸与明暗主题；Element Plus 没有的外壳与展示部件（侧边栏、顶栏、页面标题、统计卡、空状态、状态点）由起步项目里的 Vue 组件提供。视觉源自开源项目 sub2api 的前端。

## 内容基调

- 界面文字以简体中文为主，专有名词与技术词保留英文：API、SKU、Webhook、WFS、OAuth。中英文之间留一个半角空格（`API 密钥`、`Walmart 美国站`）。
- 短句、陈述语气，先说结果再说条件。设置项的说明写清「作用 + 默认值」，例如：「向已登录用户展示他们能访问的渠道、模型和定价聚合视图。默认关闭。」
- 确认框用 `ElMessageBox.confirm`，文案「确定要……吗？」：「确定要删除此订单吗？」危险操作的确认按钮设为红色（`confirmButtonClass: 'el-button--danger'`）。
- 失败提示写「动作 + 失败」，必要时补原因：「保存面板接口限流配置失败」「同步失败：Walmart 接口返回 429，请稍后重试。」
- 不用感叹号、不用 emoji、不用口号。数字带单位与千分位（`$48,210`、`1,284 笔`），范围用短横（`5–1440 分钟`）。
- 按钮文案用动词：新建、保存、导出、同步、删除；取消按钮永远叫「取消」。

## 颜色

- 颜色分两层。**语义层**（`bg-*`、`text-*`、`border-*`、`brand*`、`success*` 等）随主题切换，说明「什么地方用什么颜色」；**色阶层**（`primary-*`、`gray-*`、`dark-*` 及状态色阶）是具体色值，起步项目里写成 `text-gray-500 dark:text-dark-400` 这样的类名。两层都由 `tokens.css` 提供，Element Plus 主题也只读这些变量。
- Element Plus 的颜色全部映射到本系统：浅色主题里 `--el-color-primary` = `primary-600`，成功 = `emerald-600`，警告 = `amber-600`，危险 = `red-600`，信息 = `gray-500`（作为文字用时更清楚）；深色主题里换成各自的 500 阶。它们的浅色梯度（light-3 … light-9）依次对应 500、300、200、100、50 阶，深色主题对应 600 … 950 阶。
- 页面底用 `bg-page`；卡片、输入框、弹窗、下拉、通知用 `bg-surface`；分段控件轨道、行内代码、空状态图标框用 `bg-muted`；侧边栏用 `bg-sidebar`。
- 文字分四级：标题与关键数字 `text-strong`，正文 `text-default`，表格与表单 `text-body`，说明与提示 `text-muted`。占位符 `text-placeholder` 只用于占位符。
- 品牌色 `brand`（`primary-500`）只做填充：实心按钮、开关开启、进度条、聚焦边框。品牌色文字用 `brand-text`，浅底用 `brand-soft`（当前导航项）。
- 实心按钮（`el-button` 的 `type="primary"`）是 `gradient-btn-primary`（`primary-500` → `primary-600` 从左到右），配 `shadow-btn-primary` 的青绿投影；`danger`、`success`、`warning` 同样是本色 500 → 600 的横向渐变加同色 25% 投影。
- 状态色：成功 `success` / `success-soft`（emerald），警告 `warning` / `warning-soft`（amber），危险 `danger` / `danger-soft`（red）。`el-tag` 用「soft 底 + 700 字」，暗色下变成「900 的 30% 底 + 400 字」。状态点与 `ElNotification` 的状态条另用 `green-500`、`yellow-500`、`red-500`、`blue-500`。状态永远同时带文字或图标，不只靠颜色区分。
- 深色模式不是反相：表面换成 `dark-*` 色阶（页面 `dark-950`、侧边栏与表格 `dark-900`、表面 `dark-800`、边框 `dark-700`/`dark-600`），品牌与状态文字提亮到 400 阶，浅底换成 900 阶的 20%–30% 透明色。
- 对比度（来源原值，保留不改）：浅色下 `brand-text`（`primary-600`）对白底约 3.7:1，白字对 `primary-500` 约 2.5:1，`text-placeholder` 约 2.5:1，`border-control` 约 1.2:1，均低于 WCAG 建议。小号品牌色文字需要达标时改用 `primary-700`（约 5.5:1）；`text-strong`、`text-body`、`text-muted` 在两套主题的 `bg-page`、`bg-surface` 上都 ≥ 4.6:1。

## 字体

- 只用系统字体栈 `sans`（system-ui → PingFang SC → 微软雅黑），代码与编号用 `mono`。没有自带字体文件；Element Plus 的 `--el-font-family` 也指向 `sans`。
- 界面主体是 14px：正文 `body`（输入框、表格单元格、下拉项），按钮、表单标签、分段控件、导航用 `label`（字重 500）。12px 只给 `caption`（表单提示与错误、顶栏副标题）、`badge`（`el-tag`、趋势值、小号按钮）和 `overline`（侧边栏分组标题，全大写、`0.05em` 字距；`el-table` 表头同款但字重 500）。
- 标题只有三档：页面标题 `page-title`（24px 粗体，PageHeader），弹窗、卡片头与顶栏标题 `section-title`（18px 半粗），空状态标题 `empty-title`（18px 中粗）。统计数值 `stat-value` 与页面标题同尺寸，单行截断。
- 订单号、SKU、密钥用 `code`：`<span class="code">`，是 `bg-muted` 上的 `brand-text` 等宽小块。

## 间距与布局

- 应用外壳：固定侧边栏 256px（折叠 72px）+ 粘性毛玻璃顶栏 64px（`space-16`）+ 主内容区，主内容内边距随宽度 `space-4` → `space-6` → `space-8`。外壳背景是 `bg-page` 上叠一层固定的 `gradient-mesh`。
- 控件统一 40px 高（Element Plus 的 `--el-component-size`），小号 32px、大号 44px。按钮内边距 `space-4` × `space-2.5`，小号 `space-3` × `space-1.5`，大号 `space-6` × `space-3`。
- 容器：卡片内容 `space-6`，卡片头脚 `space-6` × `space-4`，统计卡 `space-5`。卡片之间 `space-4`（统计卡网格）或 `space-6`（区块之间）。页面标题下留 `space-6`，筛选栏与表格之间 `space-4`。
- 表格单元格横向 `space-4`、纵向约 14px，表头纵向 `space-3`；只放表格的卡片去掉内边距，让表头贴合卡片圆角。
- 表单统一标签在上（`label-position="top"`），两到三列栅格，列间距 `space-6`，表单项间距 22px（含错误提示的位置）。

## 圆角、边框与投影

- 圆角分三档：控件 `radius-xl`（按钮、输入框、下拉、导航链接、统计图标框，即 `--el-border-radius-base`），容器 `radius-2xl`（`el-card`、`el-dialog`、大号按钮），胶囊 `radius-full`（`el-tag`、开关、进度条、状态点）。小号按钮、分段选中块、通知、下拉选项用 `radius-lg`；分页按钮用 `radius-md`。
- 边框一律 1px：卡片 `border-subtle`，控件 `border-control`，其余 `border-default`。暗色卡片用 `dark-800` 50% 底配 `dark-700` 50% 边。
- 投影很轻：卡片静止 `shadow-card`，可点击卡片（`shadow="hover"`）悬停 `shadow-card-hover` 并上移 2px；浮层（下拉、日期面板、通知）`shadow-lg`，弹窗 `shadow-2xl`。品牌光晕 `shadow-glow` 只给侧边栏 Logo 框这类单点。
- 毛玻璃只用于叠在内容或渐变之上的层：顶栏（表面 80% + `blur-xl`）、毛玻璃卡（表面 70% + `blur-xl` + `shadow-glass`）、弹窗遮罩（黑 50% + `blur-sm`）。

## 动效与状态

- 过渡统一 200ms ease-out，卡片 300ms；弹窗缩放淡入，通知从右侧滑入，均使用 Element Plus 自带动画。
- 按下：按钮缩小到 98%。悬停：默认按钮与表格行变 `gray-50`（暗色 `dark-700`/`dark-800`），文字按钮与下拉项变 `gray-100`；实心按钮渐变整体加深一阶（600 → 700）。
- 聚焦：按钮是 2px `focus-ring`（`primary-500` 50%）加 2px 页面色间隙；输入框与下拉边框变 `primary-500` 并加 3px 30% 光环。
- 禁用：透明度 50%、不可点击；禁用输入框底色 `gray-100`（暗色 `dark-900`）。
- 错误：`el-form-item` 校验失败时边框 `red-500` 加 20% 光环，下方 12px `red-500` 提示文字。

## 图标

- 图标是 Heroicons outline 风格：24×24 网格、`stroke-width` 1.5、圆角端点，用 `currentColor` 继承文字色，共 82 个，收在 Icons 组。
- 起步项目里一律用 `<AppIcon name="…" />`（已内置全部 82 个），不引入 `@element-plus/icons-vue`。放进 Element Plus 组件时用它的插槽，例如 `el-input` 的 `#prefix`、`el-button` 的默认插槽。
- 尺寸五档：xs 12px（趋势箭头）、sm 16px（顶栏链接、表格行内操作）、md 20px（按钮内、导航、输入框前缀，默认）、lg 24px（统计卡图标）、xl 32px。
- 资源库里的 SVG 把描边固定成 `#64748b`（`dark-500`），只为在 `<img>` 中能显示；写进界面时用 `AppIcon` 或内联 SVG + `currentColor`。
- 不用 emoji 或彩色插画替代图标。空状态用 40px 图标放进 80px 的 `bg-muted` 圆角框。

## Logo

- EasyQuick 还没有自己的标志。侧边栏 Logo 位（起步项目 `AppSidebar.vue`）是 36px、`radius-xl`、带 `shadow-glow` 的方框，目前用 `gradient-primary` 底 + 白色 `cube` 图标占位；有了正式标志后替换进去，并放进 Logos 组。
- Logos 组里的 `sub2api-logo.svg` 是来源项目 Sub2API 的标志，只作参考，不要当作 EasyQuick 的标志使用。

## 如何使用

- **新项目**：用「EasyQuick 管理后台起步项目」（Vue 3 + Element Plus，已接好主题、明暗切换、外壳和示例页），交给 Claude Code 时它会读项目里的 `CLAUDE.md`。
- **已有的 Vue 3 项目**：按「Element Plus 接入」一节引入 `tokens.css`、Element Plus 和 `easyquick-element.css`，组件按那一节的对照表选用。
- **组件页怎么看**：「基础控件」「数据展示」「反馈」「布局」四组是**外观基准**——每张卡片画出这个部件应有的样子，说明里写明用哪个 Element Plus 组件（或起步项目里的哪个组件）实现、要注意哪些写法；「Element Plus」一组是真实 Element Plus 组件套用本主题后的渲染快照，用来核对最终效果。
- `components/bundle.css` 是本系统自己的样式表：外壳、统计卡、空状态、状态点等 Element Plus 没有的部件靠它的样式类（`sidebar-*`、`stat-*`、`empty-state`、`page-header`、`code` 等），也供不用 Vue 的纯 HTML 页面直接套用。它和 Element Plus 主题读同一份 `tokens.css`，在本页修改 token 会同时改变两边。
- Tailwind CSS 3 在起步项目里只承担两件事：编译上述样式类，以及页面布局用的工具类（`flex`、`grid`、`gap-4`、`dark:` 前缀）。它不提供组件。
- 预览中的商品、订单等内容是示例数据，只示范排版，不是规范的一部分。

## 说明与未收录

- 没有字体文件：只用系统字体栈。
- 本页所有预览都是静态的：本系统不打包组件脚本，页面不运行预览里的脚本。Element Plus 一组是在外部渲染后保存的快照。
- sub2api 自己的 Vue 组件（DataTable、Pagination、BaseDialog、Toast 等）没有移植，它们的功能由对应的 Element Plus 组件代替，外观由主题对齐；外观基准卡片里保留的是它们的样子。
- `bundle.css` 里还保留了来源样式表中的 `.dropdown`、`.dialog-*`、`.code-block`、`.tour-*` 等类，以及第三方支付按钮 `.btn-stripe`、`.btn-alipay`、`.btn-wxpay`、`.btn-airwallex`；在 Element Plus 项目里不需要它们，也没有导入对应的品牌图标。
- `bundle.css` 对来源样式表作了三处适配：暗色选择器从 `.dark` 改为 `[data-theme="dark"]`；侧边栏与下拉触发器的组件内样式并入；色值改为读取 `tokens.css` 的变量。
