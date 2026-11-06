# Element Plus 接入

EasyQuick UI 在 Vue 3 项目里的分工是 **Element Plus 管交互，EasyQuick 主题管外观**：表格、表单、弹窗、下拉、日期、分页、通知这些带交互的组件一律用 Element Plus 2.14，再由 `easyquick-element.css` 把它们换成 EasyQuick 的配色、圆角、尺寸与明暗主题；Element Plus 没有的外壳与展示部件（侧边栏、顶栏、页面标题、统计卡、空状态、状态点）用起步项目里的 Vue 组件，它们套用本系统的样式类。主题针对 Element Plus 2.14 编写并实测。

主题文件在本系统的 `element-plus/easyquick-element.css`，也随「EasyQuick UI 工具包」和「EasyQuick 管理后台起步项目」一起提供（路径 `design-system/element-plus/`），下文的 `design-system/…` 指项目里的这个文件夹。组件页「Element Plus」一组（ElementButtons、ElementForm、ElementTable、ElementFeedback）是真实 Element Plus 组件套用本主题后的渲染快照，明暗随页面切换；Screens 资源组是起步项目的实拍截图。

## 安装与加载顺序

依赖：`vue`、`element-plus`、`vue-router`；开发依赖 `tailwindcss@3`、`postcss`、`autoprefixer`。Tailwind 只用于编译 EasyQuick 的样式类和页面布局工具类（`flex`、`grid`、`gap-*`、`dark:`），不提供组件；在 `tailwind.config` 里写 `presets: [require('./design-system/tailwind/easyquick.preset.cjs')]`。

`main.ts` 中样式的引入顺序不能改，否则 Tailwind 的基础重置样式会把 Element Plus 按钮的底色清掉：

```ts
import './design-system/tokens.css'                         // 1. EasyQuick 变量
import './design-system/tailwind/easyquick.components.css'  // 2. EasyQuick 样式类 + 布局工具类
import 'element-plus/dist/index.css'                        // 3. Element Plus
import 'element-plus/theme-chalk/dark/css-vars.css'         // 4. Element Plus 深色变量
import './design-system/element-plus/easyquick-element.css' // 5. EasyQuick × Element Plus 主题
```

语言包用 `<el-config-provider :locale="zhCn">`（`element-plus/es/locale/lang/zh-cn`）。

## 深色模式

切换时同时设置 `<html class="dark">`（给 Element Plus）和 `<html data-theme="dark">`（给 EasyQuick 样式类与 `dark:` 前缀）。起步项目的 `src/composables/useTheme.ts` 已实现并记住用户选择；`index.html` 里有一段首屏脚本避免闪白。

## 组件对照

| 部件 | 用什么 | 写法要点 |
| --- | --- | --- |
| Button | `el-button` | `type="primary"` 主操作（自动变成青绿渐变 + 投影）；不写 type 就是次要按钮；`text` 是幽灵按钮；`type="danger"` 删除类操作；`size="small"` / `"large"`；`:loading` 显示加载 |
| Input | `el-input`、`el-input-number`、`type="textarea"` | 前缀图标用 `#prefix` 插槽放 `AppIcon`；数字框加 `:controls="false"` 时左对齐 |
| Select | `el-select`、`el-date-picker` | 高度与输入框一致（40px） |
| Toggle | `el-switch` | 放在设置列表行尾，左侧标题 + 一行说明 |
| Tabs（分段） | `el-segmented` | 列表状态筛选；页面内容分区才用 `el-tabs` |
| Badge | `el-tag` | 保持默认 light 效果，按含义选 `type`：success / warning / danger / info / primary |
| StatusBadge | 起步项目写法 | 8px 圆点 + 文字，Element Plus 无对应组件 |
| Card | `el-card` | 卡片里只放表格时加 `class="eq-table-card"` 去掉内边距 |
| StatCard | 起步项目 `StatCard.vue` | Element Plus 无对应组件 |
| DataTable | `el-table` | 编号列用 `<span class="code">`，状态列用 `el-tag`，操作列用 `el-button text size="small"` + 图标 |
| Pagination | `el-pagination` | 必须加 `background`，并用 `v-model:current-page`、`v-model:page-size` 双向绑定；放在表格卡片底部，左侧写已选/合计 |
| Progress | `el-progress` | `:show-text="false" :stroke-width="8"`，数值写在上方 |
| Dialog | `el-dialog` | 页脚：「取消」（默认按钮）在左，主操作在右；表单用 `label-position="top"` |
| 确认框 | `ElMessageBox.confirm` | 文案「确定要……吗？」，危险操作加 `confirmButtonClass: 'el-button--danger'` |
| Toast | `ElNotification` | 操作结果通知（右上角，左侧状态色条）；一句话的轻提示用 `ElMessage` |
| EmptyState | 起步项目 `EmptyState.vue` | 不用 `el-empty`；表格空状态放进 `el-table` 的 `#empty` 插槽 |
| Loading | `v-loading`、`el-skeleton` | 按钮加载用 `el-button` 的 `:loading` |
| Sidebar / AppHeader / PageHeader / AppLayout | 起步项目同名组件 | 菜单项在 `AppSidebar.vue` 的 `sections` 里加；顶栏标题读路由 `meta.title` |

## 规则

- 图标只用 `AppIcon`（82 个 EasyQuick 图标），不引入 `@element-plus/icons-vue`。
- 颜色只用 EasyQuick 色阶类（`text-gray-500 dark:text-dark-400`、`bg-primary-50` 等，由 preset 提供）或 Element Plus 的 `type`，不写十六进制色值，也不改 `--el-*` 变量。
- 自己写的元素都要成对写浅色与深色类，照抄外观基准卡片里的组合。
- 类名必须完整出现在代码里，不能用字符串拼接（`stat-icon-${type}` 会在编译时被清掉）；需要动态选择时写一张完整类名的映射表。
- 浅色主题里品牌色与状态色用 600 阶（作为文字更清楚），实心按钮仍是 500 → 600 渐变；深色主题用 500 阶。
