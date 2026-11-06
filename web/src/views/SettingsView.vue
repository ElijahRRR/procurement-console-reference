<script setup lang="ts">
/**
 * 设置：全局默认配送上限、模拟模式（本阶段只读）、物流同步时间（第二个 PR 启用）、文件导入的准入规则、清除模拟结果。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import type { FeishuAdmission, Settings } from '@/api/types'
import * as settingsApi from '@/api/settings'
import * as simulationApi from '@/api/simulation'
import AppIcon from '@/components/AppIcon.vue'
import PageHeader from '@/components/PageHeader.vue'
import { ADMISSION_FIELDS, FILE_ADMISSION_DEFAULTS, admissionEquals, defaultsHint, fullAdmission } from '@/utils/admission'
import { resetSummary } from '@/utils/results'
import { confirmAction, notifyFailure, notifySuccess, notifySummary } from '@/utils/feedback'
import { loadAppFlags } from '@/stores/app'

const settings = ref<Settings | null>(null)
const loading = ref(false)
const saving = ref(false)
const resetting = ref(false)
const savingAdmission = ref(false)
const maxDays = ref<number | undefined>(10)
const fileAdmission = reactive<FeishuAdmission>(fullAdmission(null, FILE_ADMISSION_DEFAULTS))

/** 服务端当前生效的文件准入规则（缺省 + 工作区改过的项） */
const savedAdmission = computed(() => fullAdmission(settings.value?.file_admission, FILE_ADMISSION_DEFAULTS))
const admissionDirty = computed(() => !admissionEquals(fileAdmission, savedAdmission.value))
const admissionIsDefault = computed(() => admissionEquals(savedAdmission.value, FILE_ADMISSION_DEFAULTS))

function applySettings(s: Settings) {
  settings.value = s
  maxDays.value = s.default_max_delivery_days
  Object.assign(fileAdmission, fullAdmission(s.file_admission, FILE_ADMISSION_DEFAULTS))
}

async function load() {
  loading.value = true
  try {
    applySettings(await settingsApi.getSettings())
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
    const next = await settingsApi.updateSettings({ default_max_delivery_days: maxDays.value })
    settings.value = next
    maxDays.value = next.default_max_delivery_days
    notifySuccess('默认配送上限已保存')
  } catch (e) {
    notifyFailure('保存默认配送上限', e)
  } finally {
    saving.value = false
  }
}

async function saveAdmission() {
  const ok = await confirmAction({
    message: '确定要保存文件导入的准入规则吗？之后的预览与提交都按新规则判定，已提交的批次不受影响。',
  })
  if (!ok) return
  savingAdmission.value = true
  try {
    const next = await settingsApi.updateSettings({ file_admission: { ...fileAdmission } })
    settings.value = next
    Object.assign(fileAdmission, fullAdmission(next.file_admission, FILE_ADMISSION_DEFAULTS))
    notifySuccess('文件导入的准入规则已保存')
  } catch (e) {
    notifyFailure('保存准入规则', e)
  } finally {
    savingAdmission.value = false
  }
}

async function restoreAdmission() {
  const ok = await confirmAction({ message: '确定要把文件导入的准入规则恢复为缺省值吗？' })
  if (!ok) return
  savingAdmission.value = true
  try {
    const next = await settingsApi.updateSettings({ file_admission: null })
    settings.value = next
    Object.assign(fileAdmission, fullAdmission(next.file_admission, FILE_ADMISSION_DEFAULTS))
    notifySuccess('已恢复缺省的准入规则')
  } catch (e) {
    notifyFailure('恢复准入规则', e)
  } finally {
    savingAdmission.value = false
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
    notifySummary(resetSummary(res))
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
    <PageHeader title="全局设置" description="设置项的修改以服务端为准，插件在下一次心跳拿到最新的有效配置。" />

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
        <template #header>
          <div class="flex flex-wrap items-center justify-between gap-3">
            <h3 class="text-base font-semibold text-gray-900 dark:text-white">文件导入的准入规则</h3>
            <el-tag v-if="admissionIsDefault" size="small" type="info" disable-transitions>缺省值</el-tag>
          </div>
        </template>
        <p class="mb-4 text-xs text-gray-500 dark:text-dark-400">
          上传的表格按这里的规则判定准入；飞书连接各自在连接里设置。文件是快照，上游取消要靠更新后的文件里的「售后状态」表达，所以缺省把「已取消」视为上游取消。
        </p>
        <el-form label-position="top">
          <div class="grid gap-x-6 md:grid-cols-2">
            <el-form-item v-for="f in ADMISSION_FIELDS" :key="f.key" :label="f.label">
              <el-select
                v-model="fileAdmission[f.key]"
                multiple
                filterable
                allow-create
                default-first-option
                :reserve-keyword="false"
                class="w-full"
                placeholder="输入后回车添加"
                :aria-label="f.label"
              />
              <p class="mt-1.5 text-xs text-gray-500 dark:text-dark-400">{{ f.hint }}{{ defaultsHint(FILE_ADMISSION_DEFAULTS[f.key]) }}</p>
            </el-form-item>
          </div>
          <div class="flex items-center justify-between gap-4 rounded-xl border border-gray-100 px-4 py-3 dark:border-dark-700">
            <div>
              <p class="text-sm font-medium text-gray-900 dark:text-white">导入历史已采购的行</p>
              <p class="text-xs text-gray-500 dark:text-dark-400">已带 AMZ 订单号的行记为已采购（不重新下单），用于物流同步与对账。文件导入默认开启。</p>
            </div>
            <el-switch v-model="fileAdmission.import_historical" aria-label="导入历史已采购的行" />
          </div>
        </el-form>
        <div class="mt-4 flex flex-wrap justify-end gap-2">
          <el-button :disabled="admissionIsDefault && !admissionDirty" :loading="savingAdmission" @click="restoreAdmission">恢复缺省值</el-button>
          <el-button type="primary" :disabled="!settings || !admissionDirty" :loading="savingAdmission" @click="saveAdmission">保存准入规则</el-button>
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
