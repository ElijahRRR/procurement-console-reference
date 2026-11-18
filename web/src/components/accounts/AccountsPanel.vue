<script setup lang="ts">
/**
 * 账号页签：顶部「待登记的买家号」（插件读到的或来源指定的、还没登记的买家号 ID），下面是买家账号列表：买家号 ID、金额区间、
 * 今日额度（已用 / 计划 / 上限）、优先级、配送上限（继承标记）、采购与同步开关（行内确认）、暂停 / 恢复执行（与采购开关分开呈现）、
 * 执行模式与时间、所在浏览器（当前登录着这个买家号的浏览器）、当前执行批次。
 */
import { computed, onMounted, ref } from 'vue'
import type { Account, DiscoveredBuyer } from '@/api/types'
import * as accountsApi from '@/api/accounts'
import * as settingsApi from '@/api/settings'
import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import StatusDot from '@/components/StatusDot.vue'
import AccountDialog, { type AccountPreset } from './AccountDialog.vue'
import DiscoveredBuyers from './DiscoveredBuyers.vue'
import { formatAmountRange, formatDateTime, formatNumber, formatRelative } from '@/utils/format'
import { PAUSE_CAUSE_LABEL, PURCHASE_MODE_LABEL, labelOf } from '@/utils/labels'
import { resumeConfirmText, resumeNeedsCaution } from '@/utils/accounts'
import { browserOnlineText } from '@/utils/browsers'
import { confirmAction, notifyFailure, notifySuccess, promptText, toast } from '@/utils/feedback'

const props = withDefaults(defineProps<{ /** 「账号」页签是否激活：没激活时待登记列表不自动刷新 */ active?: boolean }>(), { active: true })
/** 账号或待登记的买家号有变化（登记、填到已有账号）：上层据此刷新「浏览器」页签 */
const emit = defineEmits<{ changed: [] }>()

const accounts = ref<Account[]>([])
const defaultMaxDays = ref<number | null>(null)
const loading = ref(false)
const busy = ref<string | null>(null)
const dialogOpen = ref(false)
const editing = ref<Account | null>(null)
const preset = ref<AccountPreset | null>(null)
const discovered = ref<InstanceType<typeof DiscoveredBuyers>>()

async function load() {
  loading.value = true
  try {
    accounts.value = (await accountsApi.listAccounts()).rows
  } catch (e) {
    notifyFailure('读取账号', e)
  } finally {
    loading.value = false
  }
  try {
    defaultMaxDays.value = (await settingsApi.getSettings()).default_max_delivery_days
  } catch {
    defaultMaxDays.value = null
  }
}
onMounted(load)

const summary = computed(() => {
  const list = accounts.value
  return {
    total: list.length,
    enabled: list.filter((a) => a.purchase_enabled).length,
    paused: list.filter((a) => a.execution_paused).length,
    noId: list.filter((a) => !a.amazon_customer_id).length,
  }
})

function openCreate() {
  editing.value = null
  preset.value = null
  dialogOpen.value = true
}
function openEdit(a: Account) {
  editing.value = a
  preset.value = null
  dialogOpen.value = true
}
/**
 * 输入：要登记的买家号 ID、来源里的名称、这个买家号从哪里来（来源指定了几笔任务、有没有浏览器登录着）
 * → 输出：无（打开账号对话框，ID 与名称已填；提交走新建接口）
 */
function openRegister(customerId: string, nameHint: string | null = null, source: Pick<AccountPreset, 'sourceOrders' | 'hasBrowser'> = {}) {
  editing.value = null
  preset.value = { customerId, name: nameHint, ...source }
  dialogOpen.value = true
}
const registerDiscovered = (item: DiscoveredBuyer) =>
  openRegister(item.customer_id, item.name_hint, { sourceOrders: item.source_orders ?? 0, hasBrowser: !!item.browsers?.length })

/** 输入：无 → 输出：无（重新读账号与待登记的买家号） */
async function reloadAll() {
  await Promise.all([load(), discovered.value?.reload()])
}
/** 账号对话框保存后：账号、待登记列表都要重读；浏览器页签里的账号归属也变了 */
async function onSaved() {
  emit('changed')
  await reloadAll()
}
/** 待登记区把 ID 填到了已有账号上 */
function onDiscoveredChanged() {
  emit('changed')
  void load()
}
defineExpose({ reload: reloadAll, openRegister })

function replace(updated: Account | null | undefined) {
  if (!updated || typeof updated !== 'object' || !('id' in updated)) return void load()
  const i = accounts.value.findIndex((x) => x.id === updated.id)
  if (i >= 0) accounts.value[i] = updated
  else void load()
}

async function toggleSwitch(a: Account, field: 'purchase_enabled' | 'sync_enabled'): Promise<boolean> {
  const turningOn = !a[field]
  const purchase = field === 'purchase_enabled'
  const message = purchase
    ? turningOn
      ? `确定要开启「${a.name}」的采购开关吗？开启后参与自动分配。`
      : `确定要关闭「${a.name}」的采购开关吗？关闭后不再接收新分配，已分配的任务照常执行。`
    : turningOn
      ? `确定要开启「${a.name}」的同步开关吗？`
      : `确定要关闭「${a.name}」的同步开关吗？关闭后暂停该账号的订单与物流同步，不影响采购。`
  const ok = await confirmAction({ message, danger: !turningOn, confirmText: turningOn ? '开启' : '关闭' })
  if (!ok) return false
  busy.value = `${field}-${a.id}`
  try {
    replace(await accountsApi.updateAccount(a.id, { [field]: turningOn }))
    notifySuccess(`${purchase ? '采购' : '同步'}开关已${turningOn ? '开启' : '关闭'}`)
    return false // 列表已用返回值替换，阻止 el-switch 自己再翻转一次
  } catch (e) {
    notifyFailure(`${turningOn ? '开启' : '关闭'}${purchase ? '采购' : '同步'}开关`, e)
    return false
  } finally {
    busy.value = null
  }
}

async function pause(a: Account) {
  const reason = await promptText({
    title: '暂停执行',
    message: `确定要暂停「${a.name}」的采购执行吗？插件不再为它领取任务，正在执行的批次会收到停止信号；已分配的任务保留。`,
    placeholder: '暂停原因，如 需要人工登录',
    required: true,
    confirmText: '暂停执行',
    danger: true,
  })
  if (reason === null) return
  busy.value = `pause-${a.id}`
  try {
    replace(await accountsApi.pauseAccount(a.id, reason))
    notifySuccess(`已暂停「${a.name}」的执行`)
  } catch (e) {
    notifyFailure('暂停执行', e)
  } finally {
    busy.value = null
  }
}

/** 输入：列表里的账号 → 输出：重新读到的账号（暂停起因、结果待核实的任务数是最新的）；读不到时用列表里的 */
async function freshAccount(a: Account): Promise<Account> {
  try {
    const next = await accountsApi.getAccount(a.id)
    replace(next)
    return next
  } catch {
    return a
  }
}

async function resume(row: Account) {
  busy.value = `resume-${row.id}`
  const a = await freshAccount(row)
  busy.value = null
  if (!a.execution_paused) {
    toast(`「${a.name}」已不在暂停状态`, 'info')
    return
  }
  const ok = await confirmAction({
    message: resumeConfirmText(a),
    confirmText: '恢复执行',
    danger: resumeNeedsCaution(a),
  })
  if (!ok) return
  busy.value = `resume-${a.id}`
  try {
    replace(await accountsApi.resumeAccount(a.id))
    notifySuccess(`已恢复「${a.name}」的执行`)
  } catch (e) {
    notifyFailure('恢复执行', e)
  } finally {
    busy.value = null
  }
}

function quotaPercent(a: Account): number {
  const q = a.quota_today
  if (!q || !q.limit) return 0
  return Math.min(100, Math.round(((q.used + q.planned) / q.limit) * 100))
}
/** 输入：账号 → 输出：el-progress 的状态（额度用满时显示为警告色） */
function quotaStatus(a: Account): '' | 'warning' {
  const q = a.quota_today
  return q && q.limit > 0 && q.available <= 0 ? 'warning' : ''
}
</script>

<template>
  <div>
    <DiscoveredBuyers ref="discovered" :accounts="accounts" :active="props.active" @register="registerDiscovered" @changed="onDiscoveredChanged" />

    <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
      <p class="text-sm text-gray-500 dark:text-dark-400">
        共 {{ formatNumber(summary.total) }} 个账号，{{ formatNumber(summary.enabled) }} 个开启采购<template v-if="summary.paused">，{{ formatNumber(summary.paused) }} 个暂停执行</template><template v-if="summary.noId">，{{ formatNumber(summary.noId) }} 个还没登记买家号 ID</template>。<!--
        -->登录着某个账号的买家号的浏览器才能执行它的任务；账号配置以服务端为准。
      </p>
      <div class="flex gap-2">
        <el-button @click="reloadAll"><AppIcon name="refresh" size="sm" class="mr-1.5" />刷新</el-button>
        <el-button type="primary" @click="openCreate"><AppIcon name="plus" size="sm" class="mr-1.5" />新建账号</el-button>
      </div>
    </div>

    <el-card class="eq-table-card" shadow="always">
      <el-table v-loading="loading" :data="accounts" row-key="id">
        <el-table-column label="账号" min-width="136">
          <template #default="{ row }">
            <div class="whitespace-nowrap font-medium text-gray-900 dark:text-white">{{ row.name }}</div>
            <div class="mt-0.5 text-xs text-gray-500 dark:text-dark-400">
              {{ row.site }} · {{ labelOf(PURCHASE_MODE_LABEL, row.purchase_mode) }}<template v-if="row.purchase_mode === 'scheduled'"> · {{ row.schedule_times?.length ? row.schedule_times.join('、') : '未设置时间' }}</template>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="买家号 ID" width="128">
          <template #default="{ row }">
            <span v-if="row.amazon_customer_id" class="break-all font-mono text-xs text-gray-900 dark:text-gray-100">{{ row.amazon_customer_id }}</span>
            <el-tooltip v-else content="没有登记买家号 ID 的账号没有浏览器能执行。在待登记列表里把 ID 填到这个账号上，或编辑账号时填写。" placement="top">
              <el-tag size="small" type="warning" disable-transitions>未登记</el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="金额与规则" min-width="168">
          <template #default="{ row }">
            <div class="whitespace-nowrap text-gray-900 dark:text-gray-100">{{ formatAmountRange(row.min_amount, row.max_amount) }}</div>
            <div class="mt-0.5 whitespace-nowrap text-xs text-gray-500 dark:text-dark-400">优先级 {{ row.priority }}</div>
            <div class="text-xs text-gray-500 dark:text-dark-400">
              配送上限 {{ formatNumber(row.effective_max_delivery_days) }} 天<span v-if="row.max_delivery_days === null">（继承）</span><span v-else class="text-amber-700 dark:text-amber-400">（单独设置）</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="今日额度" width="150">
          <template #default="{ row }">
            <div class="mb-1.5 flex items-center justify-between gap-2 text-xs">
              <span class="whitespace-nowrap text-gray-700 dark:text-gray-300">已用 {{ formatNumber(row.quota_today?.used) }} · 计划 {{ formatNumber(row.quota_today?.planned) }}</span>
              <span class="font-medium text-gray-900 dark:text-white">/ {{ formatNumber(row.quota_today?.limit) }}</span>
            </div>
            <el-progress :percentage="quotaPercent(row as Account)" :show-text="false" :stroke-width="8" :status="quotaStatus(row as Account)" />
            <div class="mt-1 text-xs" :class="row.quota_today?.available ? 'text-gray-500 dark:text-dark-400' : 'text-amber-700 dark:text-amber-400'">
              {{ row.quota_today?.available ? `剩余 ${formatNumber(row.quota_today.available)}` : '今日额度已满' }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="采购开关" width="84" align="center">
          <template #default="{ row }">
            <el-switch
              :model-value="row.purchase_enabled"
              :loading="busy === `purchase_enabled-${row.id}`"
              :before-change="() => toggleSwitch(row as Account, 'purchase_enabled')"
              aria-label="采购开关"
            />
          </template>
        </el-table-column>
        <el-table-column label="同步开关" width="84" align="center">
          <template #default="{ row }">
            <el-switch
              :model-value="row.sync_enabled"
              :loading="busy === `sync_enabled-${row.id}`"
              :before-change="() => toggleSwitch(row as Account, 'sync_enabled')"
              aria-label="同步开关"
            />
          </template>
        </el-table-column>
        <el-table-column label="执行" min-width="160">
          <template #default="{ row }">
            <div class="flex flex-col items-start gap-1">
              <template v-if="row.execution_paused">
                <StatusDot color="red" :label="row.pause_cause ? `已暂停 · ${labelOf(PAUSE_CAUSE_LABEL, row.pause_cause)}` : '已暂停'" />
                <span class="break-all text-xs text-gray-700 dark:text-gray-300">{{ row.pause_reason || '未写原因' }}</span>
                <span v-if="row.open_result_unknown" class="text-xs text-red-600 dark:text-red-400">还有 {{ formatNumber(row.open_result_unknown) }} 笔结果待核实</span>
                <span class="text-xs text-gray-500 dark:text-dark-400">{{ formatDateTime(row.paused_at) }}</span>
                <el-button size="small" :loading="busy === `resume-${row.id}`" @click="resume(row as Account)">恢复执行</el-button>
              </template>
              <template v-else>
                <StatusDot v-if="row.active_run" color="blue" pulse label="执行中" />
                <StatusDot v-else color="green" label="可执行" />
                <span v-if="row.active_run" class="text-xs text-gray-500 dark:text-dark-400">
                  {{ row.active_run.device_name }} · {{ formatRelative(row.active_run.started_at) }}开始
                </span>
                <el-button size="small" type="danger" plain :loading="busy === `pause-${row.id}`" @click="pause(row as Account)">暂停执行</el-button>
              </template>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="所在浏览器" min-width="140">
          <template #default="{ row }">
            <div v-if="!row.browsers?.length" class="text-sm text-gray-400 dark:text-dark-500">—</div>
            <div v-for="b in row.browsers" :key="b.id" class="mb-1 last:mb-0">
              <!-- 名称完整显示（可折行）：同一批环境的名称往往只差结尾几个字，截断了就分不清 -->
              <div class="flex items-start gap-1.5">
                <StatusDot :color="b.online ? 'green' : 'gray'" class="mt-1.5" />
                <span class="min-w-0 break-all text-sm text-gray-700 dark:text-gray-300">{{ b.name }}</span>
              </div>
              <div class="ml-3.5 text-xs text-gray-500 dark:text-dark-400">{{ browserOnlineText(b) }}</div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="60" align="right">
          <template #default="{ row }">
            <el-tooltip content="编辑账号" placement="top">
              <el-button text size="small" aria-label="编辑账号" @click="openEdit(row as Account)"><AppIcon name="edit" size="sm" /></el-button>
            </el-tooltip>
          </template>
        </el-table-column>
        <template #empty>
          <EmptyState title="还没有买家账号" description="新建账号并设置金额区间与每日额度，分配才会把任务分给它。在装了插件的浏览器里登录买家号后，也可以从待登记列表直接登记。" icon="users" compact>
            <template #actions><el-button type="primary" @click="openCreate">新建账号</el-button></template>
          </EmptyState>
        </template>
      </el-table>
    </el-card>

    <AccountDialog v-model="dialogOpen" :account="editing" :preset="preset" :default-max-days="defaultMaxDays" @saved="onSaved" />
  </div>
</template>
