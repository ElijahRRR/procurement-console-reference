/** 路由表与守卫接线。页面标题与副标题放在 meta.title / meta.subtitle，顶栏读取。 */
import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { setAuthHandlers } from '@/api/client'
import { ensureSession, session } from '@/stores/session'
import { decideRoute } from './guard'

declare module 'vue-router' {
  interface RouteMeta {
    title?: string
    subtitle?: string
    public?: boolean
  }
}

const routes: RouteRecordRaw[] = [
  { path: '/setup', name: 'setup', component: () => import('@/views/SetupView.vue'), meta: { public: true, title: '首次设置' } },
  { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue'), meta: { public: true, title: '登录' } },
  {
    path: '/',
    component: () => import('@/components/layout/AppLayout.vue'),
    children: [
      {
        path: '',
        name: 'overview',
        component: () => import('@/views/OverviewView.vue'),
        meta: { title: '概览', subtitle: '今日采购任务、执行设备与最近一次分配' },
      },
      {
        path: 'orders',
        name: 'orders',
        component: () => import('@/views/OrdersView.vue'),
        meta: { title: '待采购', subtitle: '今日可采购、未来日期、待分配与需要人工处理的任务' },
      },
      {
        path: 'records',
        name: 'records',
        component: () => import('@/views/RecordsView.vue'),
        meta: { title: '采购记录', subtitle: '采购中、已采购、结果待核实、已取消与已关闭的任务' },
      },
      {
        path: 'exceptions',
        name: 'exceptions',
        component: () => import('@/views/ExceptionsView.vue'),
        meta: { title: '异常处理', subtitle: '被护栏拦截或执行失败、结果待核实、待决定重采和需要留意的任务' },
      },
      {
        path: 'sources',
        name: 'sources',
        component: () => import('@/views/SourcesView.vue'),
        meta: { title: '订单来源', subtitle: '飞书多维表格连接、文件导入与写回队列' },
      },
      {
        path: 'accounts',
        name: 'accounts',
        component: () => import('@/views/AccountsView.vue'),
        meta: { title: '账号与设备', subtitle: '买家账号的金额区间、额度与开关，插件设备的绑定与在线状态' },
      },
      {
        path: 'settings',
        name: 'settings',
        component: () => import('@/views/SettingsView.vue'),
        meta: { title: '设置', subtitle: '全局配送上限、模拟模式、物流同步与文件导入的准入规则' },
      },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach(async (to) => {
  await ensureSession()
  return decideRoute({ path: to.path, fullPath: to.fullPath, query: to.query, meta: to.meta }, session)
})

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} · 采购控制台` : '采购控制台'
})

setAuthHandlers({
  onUnauthorized: () => {
    session.me = null
    const current = router.currentRoute.value
    if (current.path !== '/login' && current.path !== '/setup') {
      void router.push({ path: '/login', query: current.fullPath !== '/' ? { redirect: current.fullPath } : {} })
    }
  },
  onNeedsSetup: () => {
    session.needsSetup = true
    session.me = null
    if (router.currentRoute.value.path !== '/setup') void router.push('/setup')
  },
})
