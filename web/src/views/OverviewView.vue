<script setup lang="ts">
/** 概览：状态计数、今日可采购、在线设备、最近异常、最近一次分配摘要（未覆盖账号与原因），快捷操作「重新分配」。 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import type { AllocationSummary, OrderRow, Overview } from '@/api/types'
import * as overviewApi from '@/api/overview'
import * as allocationApi from '@/api/allocation'
import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatCard from '@/components/StatCard.vue'
import OrderTable from '@/components/orders/OrderTable.vue'
import OrderDetailDrawer from '@/components/orders/OrderDetailDrawer.vue'
import { formatCount, formatDate, formatDateTime, formatNumber, greeting } from '@/utils/format'
import { displayName, session } from '@/stores/session'
import { ALLOCATION_TRIGGER_LABEL, labelOf } from '@/utils/labels'
import { exceptionManual, preStartManual, uncoveredTitle } from '@/utils/overview'
import { confirmAction, notifyFailure, notifySuccess } from '@/utils/feedback'

const router = useRouter()
const data = ref<Overview | null>(null)
const loading = ref(false)
const loadError = ref('')
const allocating = ref(false)
const drawerOpen = ref(false)
const drawerId = ref<number | null>(null)

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    data.value = await overviewApi.getOverview()
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}
onMounted(load)

const counts = computed(() => data.value?.counts ?? {})
/** 待采购页的待确认 + 资料待补齐 + 待核实 */
const preStart = computed(() => preStartManual(data.value?.counts))
/** 异常处理页的异常 + 结果待核实 + 待决定重采 */
const exceptions = computed(() => exceptionManual(data.value?.counts))
const unknownCount = computed(() => counts.value.result_unknown ?? 0)
/** 置了「需要留意」提醒的任务数（不论状态） */
const attentionCount = computed(() => data.value?.needs_attention ?? 0)
const last = computed<AllocationSummary | null>(() => data.value?.last_allocation ?? null)

async function reallocate() {
  const ok = await confirmAction({ message: '确定要重新分配吗？自动分配的任务会按当前账号配置重算，人工改派的任务保持不动。' })
  if (!ok) return
  allocating.value = true
  try {
    const res = await allocationApi.runAllocation()
    notifySuccess('重新分配完成', `参与 ${formatNumber(res?.stats?.considered)} 笔，分出 ${formatNumber(res?.stats?.assigned)} 笔，未分出 ${formatNumber(res?.stats?.unassigned)} 笔。`)
    await load()
  } catch (e) {
    notifyFailure('重新分配', e)
  } finally {
    allocating.value = false
  }
}

function open(row: OrderRow | { id: number }) {
  drawerId.value = row.id
  drawerOpen.value = true
}
</script>

<template>
  <div>
    <!-- 顶栏已显示「概览」；页面标题放本页自己的内容（EasyQuick AppLayout） -->
    <PageHeader :title="session.me ? `${greeting()}，${displayName(session.me)}` : '今日概况'">
      <template #description>
        <template v-if="data">北京日期 {{ formatDate(data.bj_date) }} · 账号 {{ formatNumber(data.accounts.enabled) }}/{{ formatNumber(data.accounts.total) }} 个开启采购<template v-if="data.accounts.paused"> · {{ formatNumber(data.accounts.paused) }} 个暂停执行</template></template>
        <template v-else>今日采购任务、执行设备与最近一次分配</template>
      </template>
      <template #actions>
        <el-button :loading="loading" @click="load"><AppIcon name="refresh" size="sm" class="mr-1.5" />刷新</el-button>
        <el-button type="primary" :loading="allocating" @click="reallocate"><AppIcon name="swap" size="sm" class="mr-1.5" />重新分配</el-button>
      </template>
    </PageHeader>

    <el-alert v-if="loadError" :title="`读取概览失败：${loadError}`" type="error" show-icon :closable="false" class="mb-6" />

    <div v-loading="loading && !data" class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
      <StatCard
        label="今日可采购"
        :value="formatNumber(data?.today_executable)"
        icon="play"
        tone="primary"
        :hint="data ? `未来日期 ${formatNumber(data.future_assigned)} 笔` : ''"
        clickable
        @click="router.push({ path: '/orders', query: { view: 'today' } })"
      />
      <StatCard
        label="待分配"
        :value="formatNumber(counts.queued)"
        icon="inbox"
        tone="warning"
        :hint="data ? `已分配 ${formatNumber(counts.assigned)} 笔` : ''"
        clickable
        @click="router.push({ path: '/orders', query: { view: 'unassigned' } })"
      />
      <StatCard
        label="开始前待处理"
        :value="formatNumber(data ? preStart.total : null)"
        icon="clipboard"
        tone="warning"
        :hint="data ? preStart.hint : ''"
        clickable
        @click="router.push({ path: '/orders', query: { view: preStart.view } })"
      />
      <StatCard
        label="异常待处理"
        :value="formatNumber(data ? exceptions.total : null)"
        icon="exclamationTriangle"
        tone="danger"
        :hint="data ? exceptions.hint : ''"
        clickable
        @click="router.push({ path: '/exceptions', query: { view: exceptions.view } })"
      />
      <StatCard
        label="设备在线"
        :value="data ? `${formatNumber(data.devices.online)} / ${formatNumber(data.devices.total)}` : '—'"
        icon="cpu"
        tone="success"
        :hint="data ? `已采购 ${formatCount(counts.purchased ?? 0, '笔')}` : ''"
        clickable
        @click="router.push('/accounts')"
      />
    </div>

    <div class="mt-6 grid grid-cols-1 gap-6 xl:grid-cols-3">
      <el-card class="eq-table-card xl:col-span-2" shadow="always">
        <template #header>
          <div class="flex items-center justify-between">
            <h3 class="text-base font-semibold text-gray-900 dark:text-white">最近异常</h3>
            <el-button text size="small" @click="router.push({ path: '/exceptions', query: { view: 'exception' } })">查看全部<AppIcon name="chevronRight" size="sm" class="ml-1" /></el-button>
          </div>
        </template>
        <div v-if="unknownCount || attentionCount" class="space-y-3 px-4 pt-4">
          <el-alert v-if="unknownCount" type="error" show-icon :closable="false" :title="`有 ${formatNumber(unknownCount)} 笔结果待核实：已登记提交但没有确认结果，对应账号已暂停执行。核实前不会重买。`">
            <template #default>
              <el-button size="small" class="mt-2" @click="router.push({ path: '/exceptions', query: { view: 'result_unknown' } })">去核实</el-button>
            </template>
          </el-alert>
          <el-alert v-if="attentionCount" type="warning" show-icon :closable="false" :title="`有 ${formatNumber(attentionCount)} 笔任务需要留意（如上游在采购开始后修改或取消），处理后在任务详情里标记已处理。`">
            <template #default>
              <el-button size="small" class="mt-2" @click="router.push({ path: '/exceptions', query: { view: 'needs_attention' } })">去处理</el-button>
            </template>
          </el-alert>
        </div>
        <OrderTable
          :rows="data?.recent_exceptions ?? []"
          mode="compact"
          :loading="loading && !data"
          empty-title="没有异常任务"
          empty-description="护栏拦截、缺货、页面错误重试后仍失败的任务会出现在这里；结果待核实的任务见「异常处理 → 结果待核实」。"
          @open="open"
        />
      </el-card>

      <el-card shadow="always">
        <template #header>
          <div class="flex items-center justify-between">
            <h3 class="text-base font-semibold text-gray-900 dark:text-white">最近一次分配</h3>
            <span v-if="last" class="text-xs text-gray-500 dark:text-dark-400">{{ formatDateTime(last.at) }}</span>
          </div>
        </template>
        <EmptyState v-if="!last" title="还没有分配记录" description="导入订单或点击「重新分配」后，这里显示分配结果与未覆盖账号。" icon="swap" compact />
        <template v-else>
          <div class="mb-4 text-sm text-gray-500 dark:text-dark-400">
            {{ labelOf(ALLOCATION_TRIGGER_LABEL, last.trigger) }} · 计划日期 {{ formatDate(last.bj_date) }}
          </div>
          <div class="grid grid-cols-3 gap-3">
            <div class="rounded-xl bg-gray-50 px-3 py-2.5 dark:bg-dark-800">
              <div class="text-xs text-gray-500 dark:text-dark-400">参与</div>
              <div class="text-lg font-semibold text-gray-900 dark:text-white">{{ formatNumber(last.stats.considered) }}</div>
            </div>
            <div class="rounded-xl bg-gray-50 px-3 py-2.5 dark:bg-dark-800">
              <div class="text-xs text-gray-500 dark:text-dark-400">分出</div>
              <div class="text-lg font-semibold text-gray-900 dark:text-white">{{ formatNumber(last.stats.assigned) }}</div>
            </div>
            <div class="rounded-xl bg-gray-50 px-3 py-2.5 dark:bg-dark-800">
              <div class="text-xs text-gray-500 dark:text-dark-400">未分出</div>
              <div class="text-lg font-semibold" :class="last.stats.unassigned ? 'text-amber-700 dark:text-amber-400' : 'text-gray-900 dark:text-white'">
                {{ formatNumber(last.stats.unassigned) }}
              </div>
            </div>
          </div>
          <p class="mt-3 text-xs text-gray-500 dark:text-dark-400">
            保底 {{ formatNumber(last.stats.coverage) }} 笔（每个账号当天首单）· 按优先级填充 {{ formatNumber(last.stats.fill) }} 笔
          </p>

          <div class="mt-5">
            <h4 class="mb-2 text-sm font-semibold text-gray-900 dark:text-white">未覆盖账号</h4>
            <p v-if="!last.uncovered.length" class="text-sm text-gray-500 dark:text-dark-400">每个开启采购的账号在各计划日期都分到了首单。</p>
            <template v-else>
              <p class="mb-1 text-xs text-gray-500 dark:text-dark-400">没写日期的是计划当天 {{ formatDate(last.bj_date) }}。</p>
              <ul class="divide-y divide-gray-100 dark:divide-dark-700">
                <li v-for="u in last.uncovered" :key="`${u.account_id}-${u.date}`" class="flex items-start justify-between gap-3 py-2 text-sm">
                  <span class="shrink-0 whitespace-nowrap text-gray-900 dark:text-gray-100">{{ uncoveredTitle(u, last.bj_date) }}</span>
                  <span class="min-w-0 text-right text-gray-500 dark:text-dark-400">{{ u.reason }}</span>
                </li>
              </ul>
            </template>
          </div>
          <div v-if="last.unassigned.length" class="mt-5">
            <h4 class="mb-2 text-sm font-semibold text-gray-900 dark:text-white">未分出的任务</h4>
            <ul class="divide-y divide-gray-100 dark:divide-dark-700">
              <li v-for="u in last.unassigned.slice(0, 6)" :key="u.order_id" class="py-2 text-sm">
                <button type="button" class="code pc-code-wrap max-w-full cursor-pointer text-left" :title="u.upstream_order_no" @click="open({ id: u.order_id })">{{ u.upstream_order_no }}</button>
                <div class="mt-1 text-gray-500 dark:text-dark-400">{{ u.reason }}</div>
              </li>
            </ul>
            <p v-if="last.unassigned.length > 6" class="mt-2 text-xs text-gray-500 dark:text-dark-400">
              另有 {{ formatNumber(last.unassigned.length - 6) }} 笔，见「待采购 → 待分配」。
            </p>
          </div>
        </template>
      </el-card>
    </div>

    <div v-if="data && !data.feishu_configured" class="mt-6">
      <el-alert type="info" show-icon :closable="false" title="未配置飞书应用：文件导入不受影响。需要从飞书多维表格接单时，在 backend/.env 设置 FEISHU_APP_ID 与 FEISHU_APP_SECRET 后重启服务。" />
    </div>

    <OrderDetailDrawer v-model="drawerOpen" :order-id="drawerId" @changed="load" />
  </div>
</template>
