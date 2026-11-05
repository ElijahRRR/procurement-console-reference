<script setup lang="ts">
/**
 * 飞书连接：未配置应用时说明如何配置；已配置时列出连接（立即同步、编辑、停用 / 启用、写回验收、读取记录）。
 * 写回未验收时显示「回填待验证」，队列只积压不发送。
 */
import { onMounted, ref } from 'vue'
import type { FeishuConnection, SyncRun } from '@/api/types'
import * as feishu from '@/api/feishu'
import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import StatusDot from '@/components/StatusDot.vue'
import FeishuWizard from './FeishuWizard.vue'
import { formatDateTime, formatNumber, formatRelative } from '@/utils/format'
import { SYNC_TRIGGER_LABEL, WRITEBACK_STATE_LABEL, WRITEBACK_STATE_TAG, labelOf, tagOf } from '@/utils/labels'
import { statsText, syncSummary, verifySummary } from '@/utils/results'
import { confirmAction, notifyFailure, notifySuccess, notifySummary, promptText } from '@/utils/feedback'

const emit = defineEmits<{ changed: [] }>()

const configured = ref<boolean | null>(null)
const statusError = ref('')
const connections = ref<FeishuConnection[]>([])
const loading = ref(false)
const busyId = ref<string | null>(null)
const wizardOpen = ref(false)
const editing = ref<FeishuConnection | null>(null)
const runsOpen = ref(false)
const runsFor = ref<FeishuConnection | null>(null)
const runs = ref<SyncRun[]>([])
const runsLoading = ref(false)
/** 写回验收后没能恢复原值的测试记录（连接 id → 提醒）：记录里还留着测试值，提醒一直显示到手动关掉 */
const restoreProblems = ref<Record<number, string>>({})

async function load() {
  loading.value = true
  statusError.value = ''
  try {
    const st = await feishu.getFeishuStatus()
    configured.value = !!st.configured
  } catch (e) {
    configured.value = null
    statusError.value = e instanceof Error ? e.message : String(e)
  }
  try {
    connections.value = (await feishu.listConnections()).rows
  } catch (e) {
    connections.value = []
    if (configured.value) notifyFailure('读取飞书连接', e)
  } finally {
    loading.value = false
  }
}
onMounted(load)

function openCreate() {
  editing.value = null
  wizardOpen.value = true
}
function openEdit(c: FeishuConnection) {
  editing.value = c
  wizardOpen.value = true
}

async function sync(c: FeishuConnection) {
  busyId.value = `sync-${c.id}`
  try {
    // 读飞书失败时服务端返回错误（失败已记进读取记录），走 catch；成功返回本次的统计与问题记录样本
    notifySummary(syncSummary(c.name, await feishu.syncConnection(c.id)))
    await load()
    emit('changed')
  } catch (e) {
    notifyFailure('同步', e)
  } finally {
    busyId.value = null
  }
}

async function toggleEnabled(c: FeishuConnection) {
  const enabling = !c.enabled
  const ok = await confirmAction({
    message: enabling
      ? `确定要启用「${c.name}」吗？启用后按 ${c.poll_minutes} 分钟间隔读取。`
      : `确定要停用「${c.name}」吗？停用后不再读取和写回，已导入的任务不受影响。`,
    danger: !enabling,
    confirmText: enabling ? '启用' : '停用',
  })
  if (!ok) return
  busyId.value = `toggle-${c.id}`
  try {
    await feishu.updateConnection(c.id, { enabled: enabling })
    notifySuccess(enabling ? '连接已启用' : '连接已停用')
    await load()
  } catch (e) {
    notifyFailure(enabling ? '启用连接' : '停用连接', e)
  } finally {
    busyId.value = null
  }
}

async function verify(c: FeishuConnection) {
  const recordId = await promptText({
    title: '写回验收',
    message: '填写结果表里一条专用测试记录的记录 ID（如 recDEMO0001）。服务端会写入测试值并读回比对，一致才启用写回。',
    placeholder: 'rec…',
    required: true,
    confirmText: '开始验收',
  })
  if (recordId === null) return
  busyId.value = `verify-${c.id}`
  try {
    const res = await feishu.verifyWriteback(c.id, recordId)
    const s = verifySummary(res, recordId)
    notifySummary(s, s.ok && !s.restoreProblem ? undefined : 'error')
    const next = { ...restoreProblems.value }
    if (s.restoreProblem) next[c.id] = s.restoreProblem
    else delete next[c.id]
    restoreProblems.value = next
    // 返回里带着验收后的连接（writeback_state、writeback_verified_at），先换上再整体刷新
    if (res.connection && res.connection.id === c.id) {
      connections.value = connections.value.map((x) => (x.id === c.id ? res.connection : x))
    }
    await load()
  } catch (e) {
    notifyFailure('写回验收', e)
  } finally {
    busyId.value = null
  }
}

async function openRuns(c: FeishuConnection) {
  runsFor.value = c
  runsOpen.value = true
  runsLoading.value = true
  try {
    runs.value = (await feishu.listSyncRuns(c.id)).rows
  } catch (e) {
    runs.value = []
    notifyFailure('读取同步记录', e)
  } finally {
    runsLoading.value = false
  }
}

function dismissRestoreProblem(id: number) {
  const next = { ...restoreProblems.value }
  delete next[id]
  restoreProblems.value = next
}

function onSaved() {
  void load()
  emit('changed')
}
</script>

<template>
  <div v-loading="loading && configured === null">
    <el-alert v-if="statusError" :title="`读取飞书状态失败：${statusError}`" type="error" show-icon :closable="false" class="mb-4" />

    <el-card v-if="configured === false" shadow="always">
      <EmptyState title="未配置飞书应用" icon="key">
        飞书 App ID 与 App Secret 只保存在服务端。在 <span class="code text-xs">backend/.env</span> 写入
        <span class="code text-xs">FEISHU_APP_ID</span> 与 <span class="code text-xs">FEISHU_APP_SECRET</span> 后重启服务（<span class="code text-xs">python cli.py serve</span>），这里就能连接多维表格。文件导入不受影响。
        <template #actions>
          <el-button @click="load"><AppIcon name="refresh" size="sm" class="mr-1.5" />重新检查</el-button>
        </template>
      </EmptyState>
    </el-card>

    <template v-else-if="configured">
      <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
        <p class="text-sm text-gray-500 dark:text-dark-400">每个连接对应一张订单表与一张结果表，按设定的间隔读取；也可以随时立即同步。</p>
        <el-button type="primary" @click="openCreate"><AppIcon name="plus" size="sm" class="mr-1.5" />连接飞书表</el-button>
      </div>

      <el-card v-if="!connections.length" shadow="always">
        <EmptyState title="还没有飞书连接" description="粘贴多维表格链接，选择订单表与结果表，设置字段对应和准入规则后即可接单。" icon="link">
          <template #actions><el-button type="primary" @click="openCreate">连接飞书表</el-button></template>
        </EmptyState>
      </el-card>

      <div v-else class="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <el-card v-for="c in connections" :key="c.id" shadow="always">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div class="min-w-0">
              <div class="flex flex-wrap items-center gap-2">
                <h3 class="truncate text-base font-semibold text-gray-900 dark:text-white">{{ c.name }}</h3>
                <el-tag v-if="!c.enabled" type="info" size="small" disable-transitions>已停用</el-tag>
                <el-tag :type="tagOf(WRITEBACK_STATE_TAG, c.writeback_state)" size="small" disable-transitions>
                  {{ labelOf(WRITEBACK_STATE_LABEL, c.writeback_state) }}
                </el-tag>
              </div>
              <div class="mt-1.5 flex flex-wrap gap-1.5 text-xs">
                <span class="code text-xs">{{ c.source_table_id }}</span>
                <span v-if="c.result_table_id !== c.source_table_id" class="text-gray-500 dark:text-dark-400">写回到</span>
                <span v-if="c.result_table_id !== c.source_table_id" class="code text-xs">{{ c.result_table_id }}</span>
              </div>
            </div>
            <StatusDot
              :color="!c.enabled ? 'gray' : c.last_error ? 'red' : c.last_sync_ok_at ? 'green' : 'yellow'"
              :label="!c.enabled ? '已停用' : c.last_error ? '读取失败' : c.last_sync_ok_at ? '读取正常' : '尚未读取'"
            />
          </div>

          <dl class="pc-kv mt-4">
            <dt>读取间隔</dt>
            <dd>每 {{ formatNumber(c.poll_minutes) }} 分钟</dd>
            <dt>最近读取</dt>
            <dd>
              {{ formatDateTime(c.last_sync_at) }}
              <span v-if="c.last_sync_at" class="text-xs text-gray-500 dark:text-dark-400">（{{ formatRelative(c.last_sync_at) }}）</span>
            </dd>
            <dt>最近成功</dt>
            <dd>{{ formatDateTime(c.last_sync_ok_at) }}</dd>
            <dt>上次结果</dt>
            <dd>{{ statsText(c.last_stats) || '—' }}</dd>
            <template v-if="c.writeback_enabled">
              <dt>写回验收</dt>
              <dd>{{ c.writeback_verified_at ? `${formatDateTime(c.writeback_verified_at)} 通过` : '回填待验证：写回只积压不发送' }}</dd>
            </template>
          </dl>
          <el-alert v-if="c.last_error" :title="c.last_error" type="error" :closable="false" show-icon class="mt-3" />
          <el-alert v-if="restoreProblems[c.id]" :title="restoreProblems[c.id]" type="error" show-icon :closable="false" class="mt-3">
            <template #default>
              <el-button size="small" class="mt-2" @click="dismissRestoreProblem(c.id)">关闭提醒</el-button>
            </template>
          </el-alert>

          <div class="mt-4 flex flex-wrap gap-2 border-t border-gray-100 pt-4 dark:border-dark-700">
            <el-button size="small" type="primary" :disabled="!c.enabled" :loading="busyId === `sync-${c.id}`" @click="sync(c)">
              <AppIcon name="sync" size="sm" class="mr-1" />立即同步
            </el-button>
            <el-button size="small" @click="openEdit(c)"><AppIcon name="edit" size="sm" class="mr-1" />编辑</el-button>
            <el-button v-if="c.writeback_enabled" size="small" :loading="busyId === `verify-${c.id}`" @click="verify(c)">
              <AppIcon name="shield" size="sm" class="mr-1" />写回验收
            </el-button>
            <el-button size="small" @click="openRuns(c)"><AppIcon name="clock" size="sm" class="mr-1" />查看读取记录</el-button>
            <el-button
              size="small"
              :type="c.enabled ? 'danger' : undefined"
              :plain="c.enabled"
              :loading="busyId === `toggle-${c.id}`"
              @click="toggleEnabled(c)"
            >
              {{ c.enabled ? '停用' : '启用' }}
            </el-button>
          </div>
        </el-card>
      </div>
    </template>

    <FeishuWizard v-model="wizardOpen" :connection="editing" @saved="onSaved" />

    <el-dialog v-model="runsOpen" :title="`读取记录 · ${runsFor?.name ?? ''}`" width="760px" append-to-body>
      <el-table v-loading="runsLoading" :data="runs" size="small" max-height="440" class="eq-table-card">
        <el-table-column label="开始" width="150">
          <template #default="{ row }">{{ formatDateTime(row.started_at) }}</template>
        </el-table-column>
        <el-table-column label="方式" width="90">
          <template #default="{ row }">{{ labelOf(SYNC_TRIGGER_LABEL, row.trigger) }}</template>
        </el-table-column>
        <el-table-column label="结果" width="90">
          <template #default="{ row }">
            <StatusDot
              :color="row.ok === true ? 'green' : row.ok === false ? 'red' : 'blue'"
              :label="row.ok === true ? '成功' : row.ok === false ? '失败' : '进行中'"
            />
          </template>
        </el-table-column>
        <el-table-column label="统计 / 错误" min-width="280">
          <template #default="{ row }">
            <span v-if="row.error" class="text-red-600 dark:text-red-400">{{ row.error }}</span>
            <span v-else class="text-gray-700 dark:text-gray-300">{{ statsText(row.stats) || '—' }}</span>
          </template>
        </el-table-column>
        <template #empty><span class="text-sm text-gray-500 dark:text-dark-400">还没有读取记录</span></template>
      </el-table>
      <template #footer><el-button @click="runsOpen = false">关闭</el-button></template>
    </el-dialog>
  </div>
</template>
