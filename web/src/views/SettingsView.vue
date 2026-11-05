<script setup lang="ts">
/** 设置：全局默认配送上限、模拟模式（本阶段只读）、物流同步时间（第二个 PR 启用）、清除模拟结果。 */
import { onMounted, ref } from 'vue'
import type { Settings } from '@/api/types'
import * as settingsApi from '@/api/settings'
import * as simulationApi from '@/api/simulation'
import AppIcon from '@/components/AppIcon.vue'
import PageHeader from '@/components/PageHeader.vue'
import { formatNumber } from '@/utils/format'
import { confirmAction, notifyFailure, notifySuccess } from '@/utils/feedback'
import { loadAppFlags } from '@/stores/app'

const settings = ref<Settings | null>(null)
const loading = ref(false)
const saving = ref(false)
const resetting = ref(false)
const maxDays = ref<number | undefined>(10)

async function load() {
  loading.value = true
  try {
    settings.value = await settingsApi.getSettings()
    maxDays.value = settings.value.default_max_delivery_days
  } catch (e) {
    notifyFailure('读取设置', e)
  } finally {
    loading.value = false
  }
}
onMounted(load)

async function saveDays() {
  if (!maxDays.value) return
  const ok = await confirmAction({
    message: `确定要把全局默认配送上限改为 ${maxDays.value} 天吗？没有单独设置的账号都会按新值做护栏校验。`,
  })
  if (!ok) return
  saving.value = true
  try {
    settings.value = await settingsApi.updateSettings({ default_max_delivery_days: maxDays.value })
    maxDays.value = settings.value.default_max_delivery_days
    notifySuccess('默认配送上限已保存')
  } catch (e) {
    notifyFailure('保存默认配送上限', e)
  } finally {
    saving.value = false
  }
}

async function resetSimulation() {
  const ok = await confirmAction({
    title: '清除模拟结果',
    message: '确定要清除全部模拟结果吗？所有模拟尝试会标记为已中止，受影响的任务回到采购开始前的状态，被模拟结果暂停的账号恢复执行。真实数据不受影响。',
    danger: true,
    confirmText: '清除',
  })
  if (!ok) return
  resetting.value = true
  try {
    const res = await simulationApi.resetSimulation()
    const parts: string[] = []
    if (typeof res?.attempts === 'number') parts.push(`尝试 ${formatNumber(res.attempts)} 次`)
    if (typeof res?.orders === 'number') parts.push(`任务 ${formatNumber(res.orders)} 笔`)
    if (typeof res?.accounts === 'number') parts.push(`恢复账号 ${formatNumber(res.accounts)} 个`)
    notifySuccess('已清除模拟结果', parts.join('，'))
    void loadAppFlags()
  } catch (e) {
    notifyFailure('清除模拟结果', e)
  } finally {
    resetting.value = false
  }
}
</script>

<template>
  <div>
    <PageHeader title="设置" description="设置项的修改以服务端为准，插件在下一次心跳拿到最新的有效配置。" />

    <div v-loading="loading" class="grid max-w-4xl grid-cols-1 gap-6">
      <el-card shadow="always">
        <template #header><h3 class="text-base font-semibold text-gray-900 dark:text-white">执行</h3></template>
        <div class="divide-y divide-gray-100 dark:divide-dark-700">
          <div class="flex flex-wrap items-center justify-between gap-4 pb-5">
            <div class="min-w-0 max-w-xl">
              <p class="text-sm font-medium text-gray-900 dark:text-white">默认最长配送天数</p>
              <p class="mt-0.5 text-xs text-gray-500 dark:text-dark-400">
                结算页预计送达天数超过这个值时护栏拦截（等于放行）。账号可单独设置覆盖值，未设置的继承这里。默认 10 天，范围 1–60 天。
              </p>
            </div>
            <div class="flex items-center gap-3">
              <el-input-number v-model="maxDays" :min="1" :max="60" style="width: 9rem" />
              <el-button type="primary" :loading="saving" :disabled="!settings || maxDays === settings.default_max_delivery_days" @click="saveDays">保存</el-button>
            </div>
          </div>
          <div class="flex flex-wrap items-center justify-between gap-4 py-5">
            <div class="min-w-0 max-w-xl">
              <p class="text-sm font-medium text-gray-900 dark:text-white">只允许模拟执行</p>
              <p class="mt-0.5 text-xs text-gray-500 dark:text-dark-400">
                本阶段服务端拒绝一切非模拟的执行批次，插件不会真实下单；模拟结果不写回飞书。真实下单之后单独加开关，这里暂时不能关闭。
              </p>
            </div>
            <el-tooltip content="本阶段不可修改" placement="top">
              <el-switch :model-value="settings?.simulation_only ?? true" disabled aria-label="只允许模拟执行" />
            </el-tooltip>
          </div>
          <div class="flex flex-wrap items-center justify-between gap-4 pt-5">
            <div class="min-w-0 max-w-xl">
              <p class="flex items-center gap-2 text-sm font-medium text-gray-900 dark:text-white">
                物流同步时间
                <el-tag size="small" type="info" disable-transitions>下一个 PR</el-tag>
              </p>
              <p class="mt-0.5 text-xs text-gray-500 dark:text-dark-400">每天按北京时间同步已采购订单的包裹物流。默认 08:30，第二个 PR 启用。</p>
            </div>
            <el-time-select :model-value="settings?.logistics_sync_time ?? '08:30'" disabled start="00:00" end="23:30" step="00:30" style="width: 9rem" />
          </div>
        </div>
      </el-card>

      <el-card shadow="always">
        <template #header><h3 class="text-base font-semibold text-gray-900 dark:text-white">模拟数据</h3></template>
        <div class="flex flex-wrap items-center justify-between gap-4">
          <div class="min-w-0 max-w-xl">
            <p class="text-sm font-medium text-gray-900 dark:text-white">清除模拟结果</p>
            <p class="mt-0.5 text-xs text-gray-500 dark:text-dark-400">
              把所有模拟尝试标为已中止（注明「已清除模拟结果」），受影响的任务按来源数据回到采购开始前的状态，释放模拟占用的额度，被模拟结果暂停的账号恢复执行。
            </p>
          </div>
          <el-button type="danger" :loading="resetting" @click="resetSimulation"><AppIcon name="trash" size="sm" class="mr-1.5" />清除模拟结果</el-button>
        </div>
      </el-card>
    </div>
  </div>
</template>
