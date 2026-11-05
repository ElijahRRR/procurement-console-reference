/**
 * 入口。样式引入顺序不能改（design-system/element-plus.md）：
 * 1. EasyQuick 变量 → 2. EasyQuick 样式类（含基础重置）+ Tailwind 工具类 → 3. Element Plus →
 * 4. Element Plus 深色变量 → 5. EasyQuick × Element Plus 主题。
 */
import './design-system/generated/tokens.css'
import './design-system/bundle.css'
import './styles/tailwind.css'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import './design-system/easyquick-element.css'
import './styles/app.css'

import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import App from './App.vue'
import { router } from './router'

const app = createApp(App)
app.use(ElementPlus, { locale: zhCn })
app.use(router)
app.mount('#app')
