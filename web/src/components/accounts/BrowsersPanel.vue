<script setup lang="ts">
/**
 * 浏览器页签：装了插件的浏览器（由插件自动登记，这里只读）。每行：名称、插件版本、在线、当前登录（买家号与对应账号）、
 * 正在执行的批次、最近联系；操作只有改名与移除。「显示已移除」时带上已从列表移除的。页面可见时每 10 秒自动刷新。
 * 「在线」不等于「能执行」：浏览器登录着已登记的买家号，才会领取对应账号的任务。
 */
import { computed, onMounted, ref, watch } from 'vue'
import type { Account, Device } from '@/api/types'
import * as accountsApi from '@/api/accounts'
import * as devicesApi from '@/api/devices'
import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import StatusDot from '@/components/StatusDot.vue'
import { useVisiblePolling } from '@/composables/useVisiblePolling'
import { browserCounts, browserLoginView, loginReportedText } from '@/utils/browsers'
import { formatDateTime, formatNumber, formatRelative } from '@/utils/format'
import { confirmAction, notifyFailure, notifySuccess, promptText } from '@/utils/feedback'

const props = withDefaults(defineProps<{ /** 「浏览器」页签是否激活：没激活时不自动刷新 */ active?: boolean }>(), { active: true })
const emit = defineEmits<{
  /** 改名或移除后：账号页签里的「所在浏览器」要重读 */
  changed: []
  /** 点了未登记买家号旁的「登记」：上层切到账号页签并打开账号对话框 */
  register: [customerId: string]
}>()

const devices = ref<Device[]>([])
const accounts = ref<Account[]>([])
const loading = ref(false)
const busy = ref<string | null>(null)
const showHidden = ref(false)
/** 正在进行的读取次数、最新一次读取的序号：较早发出的读取晚回来时不覆盖较新的结果 */
let inFlight = 0
let latest = 0

/**
 * 输入：方式 → 输出：无（重新读浏览器与账号）。
 * manual：转圈、失败弹通知；quiet（动作之后）：不转圈、失败不弹通知；auto（自动刷新）：同 quiet，且还有读取没回来时跳过。
 */
async function load(mode: 'manual' | 'quiet' | 'auto' = 'manual') {
  if (mode === 'auto' && inFlight > 0) return
  const mine = ++latest
  inFlight += 1
  if (mode === 'manual') loading.value = true
  try {
    const [d, a] = await Promise.all([
      devicesApi.listDevices({ include_hidden: showHidden.value }),
      // 账号只用来补账号名与「已暂停」，读不到不影响列表
      accountsApi.listAccounts().catch(() => null),
    ])
    if (mine !== latest) return
    devices.value = d.rows
    if (a) accounts.value = a.rows
  } catch (e) {
    if (mode === 'manual' && mine === latest) notifyFailure('读取浏览器', e)
  } finally {
    inFlight -= 1
    if (mine === latest) loading.value = false
  }
}
onMounted(() => load())
watch(showHidden, () => load())
useVisiblePolling(() => load('auto'), { enabled: () => props.active })
defineExpose({ reload: () => load('quiet') })

const counts = computed(() => browserCounts(devices.value))
const accountName = computed(() => new Map(accounts.value.map((a) => [a.id, a.name])))
const pausedIds = computed(() => new Set(accounts.value.filter((a) => a.execution_paused).map((a) => a.id)))

/** 输入：浏览器 → 输出：正在执行的批次属于哪个账号（账号列表里查，查不到用这个浏览器当前登录的账号） */
function runAccountName(d: Device): string {
  const id = d.active_run?.account_id
  if (id === undefined || id === null) return ''
  return accountName.value.get(id) ?? (d.account?.id === id ? d.account.name : `账号 ${id}`)
}

/** 表格行：浏览器 + 「当前登录」一栏的文字与状态点、上报时间说明、执行中批次的账号名 */
const rows = computed(() =>
  devices.value.map((d) => ({
    ...d,
    loginView: browserLoginView(d, pausedIds.value),
    reported: loginReportedText(d.login),
    runAccount: runAccountName(d),
  })),
)

async function rename(d: Device) {
  const name = await promptText({
    title: '修改浏览器名称',
    message: '名称用来区分电脑与浏览器环境，如 Mac-AdsPower-01；任务详情与提示里都用它称呼这个浏览器。',
    placeholder: d.name,
    required: true,
    confirmText: '保存',
  })
  if (!name || name === d.name) return
  busy.value = `rename-${d.id}`
  try {
    await devicesApi.renameDevice(d.id, name)
    notifySuccess('浏览器名称已保存')
    emit('changed')
    await load('quiet')
  } catch (e) {
    notifyFailure('修改浏览器名称', e)
  } finally {
    busy.value = null
  }
}

async function remove(d: Device) {
  const ok = await confirmAction({
    message: `确定要移除「${d.name}」吗？只是把它从列表里清掉；这个浏览器的插件再次联系服务端时会自动回到列表。要停止某个买家号的采购，请暂停对应的账号。`,
    confirmText: '移除',
    danger: true,
  })
  if (!ok) return
  busy.value = `remove-${d.id}`
  try {
    await devicesApi.removeDevice(d.id)
    notifySuccess('已移除浏览器')
    emit('changed')
    await load('quiet')
  } catch (e) {
    notifyFailure('移除浏览器', e)
  } finally {
    busy.value = null
  }
}
</script>

<template>
  <div>
    <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
      <p class="text-sm text-gray-500 dark:text-dark-400">
        {{ formatNumber(counts.online) }}/{{ formatNumber(counts.total) }} 个在线。浏览器登录着已登记的买家号，才会领取对应账号的任务；同一个买家号同时只在一个浏览器里执行。
      </p>
      <div class="flex items-center gap-3">
        <label class="flex cursor-pointer items-center gap-2 whitespace-nowrap text-sm text-gray-700 dark:text-gray-300">
          <el-switch v-model="showHidden" size="small" aria-label="显示已移除" />显示已移除
        </label>
        <el-button @click="load()"><AppIcon name="refresh" size="sm" class="mr-1.5" />刷新</el-button>
      </div>
    </div>

    <el-card class="eq-table-card" shadow="always">
      <el-table v-loading="loading" :data="rows" row-key="id">
        <el-table-column label="名称" min-width="180">
          <template #default="{ row }">
            <div class="flex items-center gap-2">
              <span class="font-medium text-gray-900 dark:text-white">{{ row.name }}</span>
              <el-tag v-if="row.hidden_at" size="small" type="info" disable-transitions>已移除</el-tag>
            </div>
            <div v-if="row.hidden_at" class="mt-0.5 text-xs text-gray-500 dark:text-dark-400">{{ formatDateTime(row.hidden_at) }} 移除</div>
          </template>
        </el-table-column>
        <el-table-column label="插件版本" width="100">
          <template #default="{ row }"><span class="whitespace-nowrap">{{ row.ext_version || '—' }}</span></template>
        </el-table-column>
        <el-table-column label="在线" width="90">
          <template #default="{ row }"><StatusDot :color="row.online ? 'green' : 'gray'" :label="row.online ? '在线' : '离线'" /></template>
        </el-table-column>
        <el-table-column label="当前登录" min-width="240">
          <template #default="{ row }">
            <!-- el-table 收集列定义时会用空行渲染一遍插槽，所以先判断行上有没有内容 -->
            <template v-if="row.loginView">
              <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
                <StatusDot :color="row.loginView.dot" :label="row.loginView.label" />
                <span v-if="row.loginView.customerId" class="break-all font-mono text-xs text-gray-900 dark:text-gray-100">{{ row.loginView.customerId }}</span>
                <el-button v-if="row.loginView.canRegister && !row.hidden_at" type="primary" size="small" @click="emit('register', row.loginView.customerId)">登记</el-button>
              </div>
              <div v-if="row.reported || row.login?.state === 'ready'" class="mt-0.5 text-xs text-gray-500 dark:text-dark-400">
                <span v-if="row.login?.state === 'ready' && row.login?.customer_id" class="font-mono">{{ row.login.customer_id }}</span>
                <template v-if="row.login?.state === 'ready' && row.login?.customer_id && row.reported"> · </template>
                <span :class="row.login?.stale ? 'text-amber-700 dark:text-amber-400' : ''" :title="formatDateTime(row.login?.at)">{{ row.reported }}</span>
              </div>
            </template>
          </template>
        </el-table-column>
        <el-table-column label="正在执行" min-width="170">
          <template #default="{ row }">
            <template v-if="row.active_run">
              <StatusDot color="blue" pulse :label="row.active_run.simulated ? '执行中（模拟）' : '执行中'" />
              <div class="mt-0.5 text-xs text-gray-500 dark:text-dark-400">{{ row.runAccount }} · {{ formatRelative(row.active_run.started_at) }}开始</div>
            </template>
            <span v-else class="text-sm text-gray-400 dark:text-dark-500">—</span>
          </template>
        </el-table-column>
        <el-table-column label="最近联系" width="130">
          <template #default="{ row }">
            <span class="whitespace-nowrap" :title="formatDateTime(row.last_seen_at)">{{ row.last_seen_at ? formatRelative(row.last_seen_at) : '从未联系' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="130" align="right">
          <template #default="{ row }">
            <template v-if="row.id && !row.hidden_at">
              <el-button text size="small" :loading="busy === `rename-${row.id}`" @click="rename(row as Device)">改名</el-button>
              <el-button text size="small" type="danger" class="!text-red-600 dark:!text-red-400" :loading="busy === `remove-${row.id}`" @click="remove(row as Device)">移除</el-button>
            </template>
          </template>
        </el-table-column>
        <template #empty>
          <EmptyState title="还没有浏览器连上来" description="在装了插件的浏览器里打开亚马逊页面，这里会自动出现。" icon="globe" compact />
        </template>
      </el-table>
    </el-card>
  </div>
</template>
