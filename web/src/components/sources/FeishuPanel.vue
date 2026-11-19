<script setup lang="ts">
/**
 * 飞书连接：未配置应用时说明如何配置；已配置时列出连接（立即同步、编辑、停用 / 启用、写回验收、读取记录）。
 * 写回未验收时显示「回填待验证」，队列只积压不发送。
 * 最近一次成功读取有说整个连接的问题（last_stats.problems，如来源表里找不到买家号列）时，卡片上显示一条提醒，
 * 状态点仍是「读取正常」；读取记录对话框按读取逐条列出（列表而不是表格，手机宽度下也完整可见），每次读取的统计下面
 * 列出它的问题（连接级的作为提醒行，记录级的与立即同步的提示同一格式，超过 5 条可展开）。
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
import { SYNC_PROBLEM_SHOWN, problemLines, statsText, syncSummary, unsampledProblems, verifySummary } from '@/utils/results'
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
/** 读取记录对话框里展开了全部问题记录的读取（读取记录 id） */
const expandedRuns = ref<Set<number>>(new Set())

/** 输入：连接 → 输出：最近一次成功读取里说整个连接的问题（如来源表里找不到买家号列）；没有为空列表 */
const connectionProblems = (c: FeishuConnection) => problemLines(c.last_stats?.problems).whole

/** 输入：读取记录 → 输出：这次读取的问题：连接级的原因、要显示的记录级行（没展开时只列前几条）、没显示的条数、没有保留样本的条数 */
function runProblems(row: SyncRun) {
  const { whole, records } = problemLines(row.stats?.problems)
  const expanded = expandedRuns.value.has(row.id)
  const shown = expanded ? records : records.slice(0, SYNC_PROBLEM_SHOWN)
  return { whole, shown, hidden: records.length - shown.length, expanded, canCollapse: expanded && records.length > SYNC_PROBLEM_SHOWN, unsampled: unsampledProblems(row.stats) }
}

function toggleRun(id: number) {
  const next = new Set(expandedRuns.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expandedRuns.value = next
}

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
  expandedRuns.value = new Set()
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
          <el-alert
            v-for="(w, i) in connectionProblems(c)"
            :key="`whole-${i}`"
            :title="w"
            type="warning"
            :closable="false"
            show-icon
            class="mt-3"
            data-testid="connection-problem"
          />
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
      <!-- 列表而不是表格：每次读取的问题行要在手机宽度下也完整可见（表格在窄屏只能在框里横向滚动） -->
      <div v-loading="runsLoading" class="min-h-[5rem]">
        <ul
          v-if="runs.length"
          class="max-h-[60vh] divide-y divide-gray-100 overflow-y-auto rounded-xl border border-gray-100 dark:divide-dark-700 dark:border-dark-700"
          data-testid="sync-runs"
        >
          <li v-for="row in runs" :key="row.id" class="px-4 py-3">
            <div class="flex flex-wrap items-center gap-x-4 gap-y-1">
              <span class="whitespace-nowrap text-sm font-medium text-gray-900 dark:text-white">{{ formatDateTime(row.started_at) }}</span>
              <span class="whitespace-nowrap text-xs text-gray-500 dark:text-dark-400">{{ labelOf(SYNC_TRIGGER_LABEL, row.trigger) }}</span>
              <StatusDot
                :color="row.ok === true ? 'green' : row.ok === false ? 'red' : 'blue'"
                :label="row.ok === true ? '成功' : row.ok === false ? '失败' : '进行中'"
              />
            </div>
            <p v-if="row.error" class="mt-1 break-all text-sm text-red-600 dark:text-red-400">{{ row.error }}</p>
            <template v-else>
              <p class="mt-1 text-sm text-gray-700 dark:text-gray-300">{{ statsText(row.stats) || '—' }}</p>
              <div v-for="v in [runProblems(row)]" :key="row.id" data-testid="run-problems">
                <p v-for="(w, i) in v.whole" :key="`whole-${i}`" class="mt-1.5 flex items-start gap-1 text-xs leading-5 text-amber-700 dark:text-amber-400">
                  <AppIcon name="exclamationTriangle" size="sm" class="mt-0.5" />
                  <span class="min-w-0">{{ w }}</span>
                </p>
                <ul v-if="v.shown.length" class="mt-1.5 space-y-0.5 text-xs leading-5 text-gray-500 dark:text-dark-400">
                  <li v-for="(line, i) in v.shown" :key="`rec-${i}`" class="break-all">{{ line }}</li>
                </ul>
                <el-button v-if="v.hidden > 0" text type="primary" size="small" class="!px-0" @click="toggleRun(row.id)">展开其余 {{ formatNumber(v.hidden) }} 条</el-button>
                <el-button v-else-if="v.canCollapse" text type="primary" size="small" class="!px-0" @click="toggleRun(row.id)">收起</el-button>
                <p v-if="v.unsampled > 0" class="mt-0.5 text-xs leading-5 text-gray-500 dark:text-dark-400">
                  {{ v.shown.length ? '另有 ' : '' }}{{ formatNumber(v.unsampled) }} 条有问题的记录没有保留明细
                </p>
              </div>
            </template>
          </li>
        </ul>
        <p v-else-if="!runsLoading" class="py-6 text-center text-sm text-gray-500 dark:text-dark-400">还没有读取记录</p>
      </div>
      <template #footer><el-button @click="runsOpen = false">关闭</el-button></template>
    </el-dialog>
  </div>
</template>
