<script setup lang="ts">
/**
 * 插件设备：在线状态（3 分钟内有心跳）、亚马逊登录身份、绑定账号（行内选择，确认后生效）、重命名、撤销授权。
 * 「在线」不等于「已登录可采购」，身份一致才会领取任务。
 */
import { computed, onMounted, ref } from 'vue'
import type { Account, Device } from '@/api/types'
import * as accountsApi from '@/api/accounts'
import * as devicesApi from '@/api/devices'
import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import StatusDot from '@/components/StatusDot.vue'
import PairingDialog from './PairingDialog.vue'
import { formatDateTime, formatNumber, formatRelative } from '@/utils/format'
import { IDENTITY_STATUS_LABEL, IDENTITY_STATUS_TAG, labelOf, tagOf } from '@/utils/labels'
import { confirmAction, notifyFailure, notifySuccess, promptText } from '@/utils/feedback'

const emit = defineEmits<{ changed: [] }>()

const devices = ref<Device[]>([])
const accounts = ref<Account[]>([])
const loading = ref(false)
const busy = ref<string | null>(null)
const pairingOpen = ref(false)
const showRevoked = ref(false)

async function load() {
  loading.value = true
  try {
    const [d, a] = await Promise.all([devicesApi.listDevices(), accountsApi.listAccounts().catch(() => ({ total: 0, rows: [] as Account[] }))])
    devices.value = d.rows
    accounts.value = a.rows
  } catch (e) {
    notifyFailure('读取设备', e)
  } finally {
    loading.value = false
  }
}
onMounted(load)

const visibleDevices = computed(() => devices.value.filter((d) => showRevoked.value || !d.revoked_at))
const onlineCount = computed(() => devices.value.filter((d) => !d.revoked_at && d.online).length)
const activeCount = computed(() => devices.value.filter((d) => !d.revoked_at).length)

function replace(updated: Device | null | undefined) {
  if (!updated || typeof updated !== 'object' || !('id' in updated)) return void load()
  const i = devices.value.findIndex((x) => x.id === updated.id)
  if (i >= 0) devices.value[i] = updated
  else void load()
}

async function bind(d: Device, accountId: number | null) {
  const target = accounts.value.find((a) => a.id === accountId)
  const ok = await confirmAction({
    message: accountId
      ? `确定要把「${d.name}」绑定到「${target?.name ?? accountId}」吗？该浏览器需登录这个买家账号，身份一致后才会领取它的任务。`
      : `确定要解除「${d.name}」的账号绑定吗？解除后它不再领取任何任务。`,
    danger: !accountId,
    confirmText: accountId ? '绑定' : '解除绑定',
  })
  if (!ok) return
  busy.value = `bind-${d.id}`
  try {
    replace(await devicesApi.updateDevice(d.id, { bound_account_id: accountId }))
    notifySuccess(accountId ? `已绑定到「${target?.name ?? accountId}」` : '已解除绑定')
    emit('changed')
  } catch (e) {
    notifyFailure(accountId ? '绑定账号' : '解除绑定', e)
  } finally {
    busy.value = null
  }
}

async function rename(d: Device) {
  const name = await promptText({ title: '重命名设备', message: '设备名称用来区分电脑与浏览器环境，如 Mac-AdsPower-01。', placeholder: d.name, required: true, confirmText: '保存' })
  if (!name || name === d.name) return
  busy.value = `rename-${d.id}`
  try {
    replace(await devicesApi.updateDevice(d.id, { name }))
    notifySuccess('设备已重命名')
  } catch (e) {
    notifyFailure('重命名设备', e)
  } finally {
    busy.value = null
  }
}

async function revoke(d: Device) {
  const ok = await confirmAction({
    message: `确定要撤销「${d.name}」的授权吗？它的设备令牌立即失效，正在执行的批次会被停止；之后需要重新配对。`,
    danger: true,
    confirmText: '撤销授权',
  })
  if (!ok) return
  busy.value = `revoke-${d.id}`
  try {
    replace(await devicesApi.revokeDevice(d.id))
    notifySuccess('已撤销设备授权')
    emit('changed')
  } catch (e) {
    notifyFailure('撤销设备', e)
  } finally {
    busy.value = null
  }
}

async function adopt(d: Device) {
  if (!d.bound_account || !d.identity?.customer_id) return
  const ok = await confirmAction({
    message: `确定要把「${d.bound_account.name}」的买家号 ID 设为插件识别到的 ${d.identity.customer_id} 吗？请先确认该浏览器登录的就是这个买家账号。`,
    confirmText: '采用',
  })
  if (!ok) return
  busy.value = `adopt-${d.id}`
  try {
    await accountsApi.adoptCustomerId(d.bound_account.id, d.id)
    notifySuccess('已采用插件识别的买家号 ID')
    await load()
    emit('changed')
  } catch (e) {
    notifyFailure('采用买家号 ID', e)
  } finally {
    busy.value = null
  }
}

function canAdopt(d: Device): boolean {
  return !d.revoked_at && !!d.bound_account && !!d.identity?.customer_id && d.identity_status !== 'match'
}
</script>

<template>
  <div>
    <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
      <p class="text-sm text-gray-500 dark:text-dark-400">
        {{ formatNumber(onlineCount) }}/{{ formatNumber(activeCount) }} 台在线。一个浏览器环境（AdsPower 环境或 Chrome 配置）对应一台设备；同一账号同时只有一台设备能取得执行权。
      </p>
      <div class="flex items-center gap-3">
        <el-checkbox v-model="showRevoked">显示已撤销</el-checkbox>
        <el-button @click="load"><AppIcon name="refresh" size="sm" class="mr-1.5" />刷新</el-button>
        <el-button type="primary" @click="pairingOpen = true"><AppIcon name="key" size="sm" class="mr-1.5" />生成配对码</el-button>
      </div>
    </div>

    <el-card class="eq-table-card" shadow="always">
      <el-table v-loading="loading" :data="visibleDevices" row-key="id">
        <el-table-column label="设备" min-width="200">
          <template #default="{ row }">
            <div class="flex items-center gap-2">
              <span class="font-medium text-gray-900 dark:text-white">{{ row.name }}</span>
              <el-tag v-if="row.revoked_at" size="small" type="info" disable-transitions>已撤销</el-tag>
            </div>
            <div class="mt-0.5 text-xs text-gray-500 dark:text-dark-400">
              {{ row.browser || '未知浏览器' }}<template v-if="row.ext_version"> · 插件 {{ row.ext_version }}</template>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="在线" width="150">
          <template #default="{ row }">
            <StatusDot :color="row.revoked_at ? 'gray' : row.online ? 'green' : 'gray'" :label="row.revoked_at ? '已撤销' : row.online ? '在线' : '离线'" />
            <div class="mt-1 text-xs text-gray-500 dark:text-dark-400">{{ row.last_seen_at ? formatRelative(row.last_seen_at) : '从未联系' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="亚马逊身份" min-width="200">
          <template #default="{ row }">
            <el-tag size="small" :type="tagOf(IDENTITY_STATUS_TAG, row.identity_status)" disable-transitions>{{ labelOf(IDENTITY_STATUS_LABEL, row.identity_status) }}</el-tag>
            <div v-if="row.identity?.customer_id" class="mt-1 text-xs text-gray-500 dark:text-dark-400">
              识别到 <span class="font-mono">{{ row.identity.customer_id }}</span><template v-if="row.identity.at"> · {{ formatRelative(row.identity.at) }}</template>
            </div>
            <el-button v-if="canAdopt(row as Device)" text size="small" class="mt-1" :loading="busy === `adopt-${row.id}`" @click="adopt(row as Device)">采用此 ID</el-button>
          </template>
        </el-table-column>
        <el-table-column label="绑定账号" min-width="200">
          <template #default="{ row }">
            <el-select
              :model-value="row.bound_account?.id ?? null"
              size="small"
              class="w-full"
              clearable
              placeholder="未绑定"
              :disabled="!!row.revoked_at"
              :loading="busy === `bind-${row.id}`"
              @change="(v: number | null | '') => bind(row as Device, v === '' || v === undefined ? null : v)"
            >
              <el-option v-for="a in accounts" :key="a.id" :value="a.id" :label="a.name" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="配对时间" width="160">
          <template #default="{ row }"><span class="whitespace-nowrap">{{ formatDateTime(row.created_at) }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="150" align="right">
          <template #default="{ row }">
            <template v-if="!row.revoked_at">
              <el-button text size="small" :loading="busy === `rename-${row.id}`" @click="rename(row as Device)">重命名</el-button>
              <el-button text size="small" type="danger" class="!text-red-600 dark:!text-red-400" :loading="busy === `revoke-${row.id}`" @click="revoke(row as Device)">撤销</el-button>
            </template>
          </template>
        </el-table-column>
        <template #empty>
          <EmptyState title="还没有配对的设备" description="生成配对码，在 AdsPower 环境或 Chrome 里安装插件并输入配对码。" icon="cpu" compact>
            <template #actions><el-button type="primary" @click="pairingOpen = true">生成配对码</el-button></template>
          </EmptyState>
        </template>
      </el-table>
    </el-card>

    <PairingDialog v-model="pairingOpen" @closed="load" />
  </div>
</template>
