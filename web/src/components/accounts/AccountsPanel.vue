<script setup lang="ts">
/**
 * 买家账号列表：金额区间、今日额度（已用 / 计划 / 上限）、优先级、配送上限（继承标记）、采购与同步开关（行内确认）、
 * 暂停 / 恢复执行（与采购开关分开呈现）、执行模式与时间、绑定设备在线状态、当前执行批次。
 */
import { computed, onMounted, ref } from 'vue'
import type { Account, Device } from '@/api/types'
import * as accountsApi from '@/api/accounts'
import * as devicesApi from '@/api/devices'
import * as settingsApi from '@/api/settings'
import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import StatusDot from '@/components/StatusDot.vue'
import AccountDialog from './AccountDialog.vue'
import { formatAmountRange, formatDateTime, formatNumber, formatRelative } from '@/utils/format'
import { IDENTITY_STATUS_DOT, IDENTITY_STATUS_LABEL, PAUSE_CAUSE_LABEL, PURCHASE_MODE_LABEL, labelOf } from '@/utils/labels'
import { resumeConfirmText, resumeNeedsCaution } from '@/utils/accounts'
import { confirmAction, notifyFailure, notifySuccess, promptText, toast } from '@/utils/feedback'

const accounts = ref<Account[]>([])
const devices = ref<Device[]>([])
const defaultMaxDays = ref<number | null>(null)
const loading = ref(false)
const busy = ref<string | null>(null)
const dialogOpen = ref(false)
const editing = ref<Account | null>(null)

async function load() {
  loading.value = true
  try {
    const [a, d] = await Promise.all([accountsApi.listAccounts(), devicesApi.listDevices().catch(() => ({ total: 0, rows: [] as Device[] }))])
    accounts.value = a.rows
    devices.value = d.rows
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
defineExpose({ reload: load })

const summary = computed(() => {
  const list = accounts.value
  return {
    total: list.length,
    enabled: list.filter((a) => a.purchase_enabled).length,
    paused: list.filter((a) => a.execution_paused).length,
  }
})

function openCreate() {
  editing.value = null
  dialogOpen.value = true
}
function openEdit(a: Account) {
  editing.value = a
  dialogOpen.value = true
}

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
    <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
      <p class="text-sm text-gray-500 dark:text-dark-400">
        共 {{ formatNumber(summary.total) }} 个账号，{{ formatNumber(summary.enabled) }} 个开启采购<template v-if="summary.paused">，{{ formatNumber(summary.paused) }} 个暂停执行</template>。<!--
        -->账号配置以服务端为准，插件显示的是服务端已确认的有效设置。
      </p>
      <div class="flex gap-2">
        <el-button @click="load"><AppIcon name="refresh" size="sm" class="mr-1.5" />刷新</el-button>
        <el-button type="primary" @click="openCreate"><AppIcon name="plus" size="sm" class="mr-1.5" />新建账号</el-button>
      </div>
    </div>

    <el-card class="eq-table-card" shadow="always">
      <el-table v-loading="loading" :data="accounts" row-key="id">
        <el-table-column label="账号" min-width="170">
          <template #default="{ row }">
            <div class="whitespace-nowrap font-medium text-gray-900 dark:text-white">{{ row.name }}</div>
            <div class="mt-0.5 whitespace-nowrap text-xs text-gray-500 dark:text-dark-400">
              <span v-if="row.amazon_customer_id" class="font-mono">{{ row.amazon_customer_id }}</span>
              <span v-else>未登记买家号 ID</span>
              · {{ row.site }}
            </div>
            <div class="mt-0.5 text-xs text-gray-500 dark:text-dark-400">
              {{ labelOf(PURCHASE_MODE_LABEL, row.purchase_mode) }}<template v-if="row.purchase_mode === 'scheduled'"> · {{ row.schedule_times?.length ? row.schedule_times.join('、') : '未设置时间' }}</template>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="金额与规则" min-width="150">
          <template #default="{ row }">
            <div class="whitespace-nowrap text-gray-900 dark:text-gray-100">{{ formatAmountRange(row.min_amount, row.max_amount) }}</div>
            <div class="mt-0.5 whitespace-nowrap text-xs text-gray-500 dark:text-dark-400">优先级 {{ row.priority }}</div>
            <div class="whitespace-nowrap text-xs text-gray-500 dark:text-dark-400">
              配送上限 {{ formatNumber(row.effective_max_delivery_days) }} 天<span v-if="row.max_delivery_days === null">（继承）</span><span v-else class="text-amber-700 dark:text-amber-400">（单独设置）</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="今日额度" width="160">
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
        <el-table-column label="执行" min-width="170">
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
        <el-table-column label="绑定设备" min-width="170">
          <template #default="{ row }">
            <div v-if="!row.devices?.length" class="text-sm text-gray-400 dark:text-dark-500">未绑定</div>
            <div v-for="d in row.devices" :key="d.id" class="mb-1 last:mb-0">
              <StatusDot :color="!d.online ? 'gray' : IDENTITY_STATUS_DOT[d.identity_status as 'match']" :label="d.name" />
              <div class="ml-3.5 text-xs text-gray-500 dark:text-dark-400">
                {{ d.online ? labelOf(IDENTITY_STATUS_LABEL, d.identity_status) : `离线 · ${formatRelative(d.last_seen_at)}` }}
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="64" align="right">
          <template #default="{ row }">
            <el-tooltip content="编辑账号" placement="top">
              <el-button text size="small" aria-label="编辑账号" @click="openEdit(row as Account)"><AppIcon name="edit" size="sm" /></el-button>
            </el-tooltip>
          </template>
        </el-table-column>
        <template #empty>
          <EmptyState title="还没有买家账号" description="新建账号并设置金额区间与每日额度，分配才会把任务分给它。" icon="users" compact>
            <template #actions><el-button type="primary" @click="openCreate">新建账号</el-button></template>
          </EmptyState>
        </template>
      </el-table>
    </el-card>

    <AccountDialog v-model="dialogOpen" :account="editing" :devices="devices" :default-max-days="defaultMaxDays" @saved="load" />
  </div>
</template>
