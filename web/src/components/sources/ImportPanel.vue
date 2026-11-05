<script setup lang="ts">
/**
 * 文件导入：上传 .xlsx / .csv（10 MB 以内）→ 选工作表与字段对应（可套用 / 保存个人模板）→ 预览统计与逐行结论 → 提交或放弃。
 * 下方是批次列表：导出结果、下载问题行。不修改用户的原文件。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import type { UploadRawFile, UploadRequestOptions } from 'element-plus'
import type { ImportBatch, ImportPreview, ImportRow, ImportTemplate, ImportVerdict, UploadResult } from '@/api/types'
import { IMPORT_VERDICTS } from '@/api/types'
import * as imports from '@/api/imports'
import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import { formatDateTime, formatNumber } from '@/utils/format'
import { BATCH_STATUS_LABEL, BATCH_STATUS_TAG, IMPORT_VERDICT_LABEL, VERDICT_TAG, labelOf, tagOf } from '@/utils/labels'
import { confirmAction, notifyFailure, notifySuccess, promptText, toast } from '@/utils/feedback'

const emit = defineEmits<{ changed: [] }>()

const MAX_BYTES = 10 * 1024 * 1024

const upload = ref<UploadResult | null>(null)
const uploading = ref(false)
const sheet = ref('')
const mapping = reactive<Record<string, string | null>>({})
const templates = ref<ImportTemplate[]>([])
const templateId = ref<number | null>(null)
const preview = ref<ImportPreview | null>(null)
const previewing = ref(false)
const verdict = ref<ImportVerdict | ''>('')
const rows = ref<ImportRow[]>([])
const rowsTotal = ref(0)
const rowsPage = ref(1)
const rowsLoading = ref(false)
const committing = ref(false)

const batches = ref<ImportBatch[]>([])
const batchesTotal = ref(0)
const batchesPage = ref(1)
const batchesLoading = ref(false)

const headers = computed(() => upload.value?.sheets.find((s) => s.name === sheet.value)?.headers ?? upload.value?.headers ?? [])
const systemFields = computed(() => upload.value?.system_fields ?? [])
const mappedCount = computed(() => headers.value.filter((h) => mapping[h]).length)
const requiredMissing = computed(() =>
  systemFields.value.filter((f) => f.required && !Object.values(mapping).includes(f.key)).map((f) => f.label),
)

async function loadBatches() {
  batchesLoading.value = true
  try {
    const res = await imports.listImports({ page: batchesPage.value, page_size: 10 })
    batches.value = res.rows
    batchesTotal.value = res.total
  } catch (e) {
    notifyFailure('读取导入批次', e)
  } finally {
    batchesLoading.value = false
  }
}

async function loadTemplates() {
  try {
    templates.value = (await imports.listTemplates()).rows
  } catch {
    templates.value = []
  }
}

onMounted(() => {
  void loadBatches()
  void loadTemplates()
})

function beforeUpload(file: UploadRawFile): boolean {
  const name = file.name.toLowerCase()
  if (name.endsWith('.xls')) {
    notifyFailure('上传', '不支持 .xls，请在 Excel 里另存为 .xlsx 后再上传')
    return false
  }
  if (!name.endsWith('.xlsx') && !name.endsWith('.csv')) {
    notifyFailure('上传', '只支持 .xlsx 与 .csv 文件')
    return false
  }
  if (file.size > MAX_BYTES) {
    notifyFailure('上传', '文件超过 10 MB')
    return false
  }
  return true
}

function applyMapping(m: Record<string, string | null>) {
  for (const k of Object.keys(mapping)) delete mapping[k]
  for (const h of headers.value) mapping[h] = m[h] ?? null
}

async function doUpload(opts: UploadRequestOptions) {
  uploading.value = true
  preview.value = null
  try {
    const res = await imports.uploadImport(opts.file as File)
    upload.value = res
    sheet.value = res.sheet || res.sheets[0]?.name || ''
    applyMapping(res.suggested_mapping ?? {})
    templateId.value = null
    if (res.templates?.length && !templates.value.length) await loadTemplates()
    toast(`已读取「${res.filename}」，请核对字段对应`)
    void loadBatches()
  } catch (e) {
    notifyFailure('上传', e)
  } finally {
    uploading.value = false
  }
}

function onSheetChange() {
  applyMapping(upload.value?.sheet === sheet.value ? (upload.value?.suggested_mapping ?? {}) : {})
  preview.value = null
}

function onTemplate(id: number | null) {
  const t = templates.value.find((x) => x.id === id)
  if (!t) return
  for (const h of headers.value) {
    if (h in t.mapping) mapping[h] = t.mapping[h] ?? null
  }
  toast(`已套用模板「${t.name}」`)
}

async function saveTemplate() {
  const name = await promptText({ title: '存为模板', message: '给这套字段对应起个名字，下次上传同样格式的文件时可以直接套用。', placeholder: '如 小蜜蜂导出', required: true, confirmText: '保存' })
  if (!name) return
  try {
    const t = await imports.createTemplate(name, { ...mapping })
    notifySuccess(`已保存模板「${t?.name ?? name}」`)
    await loadTemplates()
  } catch (e) {
    notifyFailure('保存模板', e)
  }
}

async function removeTemplate(t: ImportTemplate) {
  const ok = await confirmAction({ message: `确定要删除模板「${t.name}」吗？`, danger: true, confirmText: '删除' })
  if (!ok) return
  try {
    await imports.deleteTemplate(t.id)
    if (templateId.value === t.id) templateId.value = null
    await loadTemplates()
  } catch (e) {
    notifyFailure('删除模板', e)
  }
}

async function runPreview() {
  if (!upload.value) return
  previewing.value = true
  try {
    const res = await imports.previewImport(upload.value.batch_id, sheet.value, { ...mapping })
    preview.value = res
    verdict.value = ''
    rowsPage.value = 1
    rows.value = res.rows?.rows ?? []
    rowsTotal.value = res.rows?.total ?? 0
    void loadBatches()
  } catch (e) {
    notifyFailure('生成预览', e)
  } finally {
    previewing.value = false
  }
}

async function loadRows() {
  if (!upload.value) return
  rowsLoading.value = true
  try {
    const res = await imports.listImportRows(upload.value.batch_id, { verdict: verdict.value, page: rowsPage.value, page_size: 20 })
    rows.value = res.rows
    rowsTotal.value = res.total
  } catch (e) {
    notifyFailure('读取预览行', e)
  } finally {
    rowsLoading.value = false
  }
}

const stats = computed(() => preview.value?.stats ?? {})
const statCards = computed(() => [
  { key: 'new', label: '新单', tone: 'text-emerald-700 dark:text-emerald-400' },
  { key: 'update', label: '更新', tone: 'text-primary-700 dark:text-primary-400' },
  { key: 'unchanged', label: '无变化', tone: 'text-gray-900 dark:text-white' },
  { key: 'conflict', label: '冲突', tone: 'text-red-600 dark:text-red-400' },
  { key: 'historical', label: '历史', tone: 'text-amber-700 dark:text-amber-400' },
  { key: 'invalid', label: '无效', tone: 'text-red-600 dark:text-red-400' },
])
const verdictOptions = computed(() => [
  { value: '', label: `全部 ${formatNumber(stats.value.total)}` },
  ...IMPORT_VERDICTS.map((v) => ({ value: v, label: `${IMPORT_VERDICT_LABEL[v]} ${formatNumber((stats.value as Record<string, number>)[v] ?? 0)}` })),
])

async function commit() {
  if (!upload.value || !preview.value) return
  const s = stats.value
  const ok = await confirmAction({
    message: `确定要提交这批订单吗？新单 ${formatNumber(s.new ?? 0)}、更新 ${formatNumber(s.update ?? 0)} 笔会进入任务流（其中待确认 ${formatNumber(s.pending_review ?? 0)}、资料待补齐 ${formatNumber(s.needs_info ?? 0)} 笔不会下发采购），无效行不导入。`,
    confirmText: '提交',
  })
  if (!ok) return
  committing.value = true
  try {
    const batch = await imports.commitImport(upload.value.batch_id)
    notifySuccess('已提交导入', `批次 ${batch?.filename ?? upload.value.filename} 已进入任务流，分配会自动运行。`)
    upload.value = null
    preview.value = null
    await loadBatches()
    emit('changed')
  } catch (e) {
    notifyFailure('提交导入', e)
  } finally {
    committing.value = false
  }
}

async function discard() {
  if (!upload.value) return
  const ok = await confirmAction({ message: '确定要放弃这次上传吗？已上传的文件副本与预览会作废。', danger: true, confirmText: '放弃' })
  if (!ok) return
  try {
    await imports.discardImport(upload.value.batch_id)
    upload.value = null
    preview.value = null
    await loadBatches()
  } catch (e) {
    notifyFailure('放弃导入', e)
  }
}

function batchStats(b: ImportBatch): string {
  const s = b.stats
  if (!s) return '—'
  const parts = [
    ['新单', s.new],
    ['更新', s.update],
    ['无变化', s.unchanged],
    ['冲突', s.conflict],
    ['历史', s.historical],
    ['无效', s.invalid],
  ].filter(([, v]) => typeof v === 'number' && v > 0) as [string, number][]
  return parts.length ? parts.map(([k, v]) => `${k} ${formatNumber(v)}`).join(' · ') : `共 ${formatNumber(s.total ?? 0)} 行`
}

function rowPreview(r: ImportRow): string {
  const n = r.normalized ?? {}
  const parts = [n.asin, n.quantity !== undefined && n.quantity !== null ? `× ${n.quantity}` : null, n.price_cap ? `限价 $${n.price_cap}` : null]
  return parts.filter(Boolean).join(' ') || '—'
}
</script>

<template>
  <div class="space-y-6">
    <el-card shadow="always">
      <template #header>
        <div class="flex flex-wrap items-center justify-between gap-3">
          <h3 class="text-base font-semibold text-gray-900 dark:text-white">上传订单文件</h3>
          <el-button size="small" @click="imports.downloadTemplate()"><AppIcon name="download" size="sm" class="mr-1" />下载标准模板</el-button>
        </div>
      </template>

      <el-upload
        v-if="!upload"
        drag
        :show-file-list="false"
        accept=".xlsx,.csv"
        :before-upload="beforeUpload"
        :http-request="doUpload"
        :disabled="uploading"
      >
        <div v-loading="uploading" class="flex flex-col items-center py-4">
          <div class="mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-primary-50 text-primary-600 dark:bg-primary-900/20 dark:text-primary-400">
            <AppIcon name="upload" size="lg" />
          </div>
          <p class="text-sm text-gray-700 dark:text-gray-300">把文件拖到这里，或<span class="font-medium text-primary-600 dark:text-primary-400">点击选择</span></p>
          <p class="mt-1 text-xs text-gray-500 dark:text-dark-400">支持 XLSX、CSV（UTF-8 / GBK），单个文件不超过 10 MB。只有「平台订单号」必填。</p>
        </div>
      </el-upload>

      <div v-else>
        <div class="mb-4 flex flex-wrap items-center gap-3">
          <AppIcon name="document" class="text-gray-400 dark:text-dark-500" />
          <span class="text-sm font-medium text-gray-900 dark:text-white">{{ upload.filename }}</span>
          <span class="text-xs text-gray-500 dark:text-dark-400">批次 {{ upload.batch_id }}</span>
          <div class="ml-auto flex flex-wrap items-center gap-3">
            <el-select v-model="sheet" size="small" style="width: 12rem" @change="onSheetChange">
              <el-option v-for="s in upload.sheets" :key="s.name" :value="s.name" :label="`${s.name}（${formatNumber(s.row_count)} 行）`" />
            </el-select>
            <el-select v-model="templateId" size="small" style="width: 12rem" clearable placeholder="套用个人模板" @change="onTemplate">
              <el-option v-for="t in templates" :key="t.id" :value="t.id" :label="t.name">
                <div class="flex items-center justify-between gap-3">
                  <span>{{ t.name }}</span>
                  <button type="button" class="text-xs text-red-600 dark:text-red-400" @click.stop="removeTemplate(t)">删除</button>
                </div>
              </el-option>
            </el-select>
            <el-button size="small" @click="saveTemplate">存为模板</el-button>
          </div>
        </div>

        <p class="mb-3 text-xs text-gray-500 dark:text-dark-400">
          已对应 {{ formatNumber(mappedCount) }}/{{ formatNumber(headers.length) }} 列。没有对应的列不导入；除平台订单号外不强制必填，缺人审结果的行停在「待确认」，缺执行字段的停在「资料待补齐」。
        </p>
        <el-alert v-if="requiredMissing.length" :title="`还没有对应：${requiredMissing.join('、')}`" type="warning" show-icon :closable="false" class="mb-3" />

        <el-table :data="headers.map((h) => ({ header: h }))" size="small" max-height="340" class="eq-table-card">
          <el-table-column label="文件表头" min-width="200">
            <template #default="{ row }"><span class="text-gray-900 dark:text-gray-100">{{ row.header }}</span></template>
          </el-table-column>
          <el-table-column label="系统字段" min-width="240">
            <template #default="{ row }">
              <el-select v-model="mapping[row.header]" size="small" class="w-full" clearable filterable placeholder="不导入">
                <el-option v-for="f in systemFields" :key="f.key" :value="f.key" :label="f.required ? `${f.label}（必填）` : f.label" />
              </el-select>
            </template>
          </el-table-column>
        </el-table>

        <div class="mt-4 flex flex-wrap justify-end gap-2">
          <el-button @click="discard">放弃</el-button>
          <el-button type="primary" :loading="previewing" :disabled="!!requiredMissing.length" @click="runPreview">生成预览</el-button>
        </div>
      </div>
    </el-card>

    <el-card v-if="preview" shadow="always">
      <template #header>
        <div class="flex flex-wrap items-center justify-between gap-3">
          <h3 class="text-base font-semibold text-gray-900 dark:text-white">预览 · 共 {{ formatNumber(stats.total) }} 行</h3>
          <span class="text-sm text-gray-500 dark:text-dark-400">
            其中待确认 {{ formatNumber(stats.pending_review ?? 0) }} 笔，资料待补齐 {{ formatNumber(stats.needs_info ?? 0) }} 笔
          </span>
        </div>
      </template>
      <div>
      <div class="mb-4 grid grid-cols-3 gap-3 md:grid-cols-6">
        <div v-for="c in statCards" :key="c.key" class="rounded-xl bg-gray-50 px-3 py-2.5 dark:bg-dark-800">
          <div class="text-xs text-gray-500 dark:text-dark-400">{{ c.label }}</div>
          <div class="text-lg font-semibold" :class="c.tone">{{ formatNumber((stats as Record<string, number>)[c.key] ?? 0) }}</div>
        </div>
      </div>
      <div class="mb-3 overflow-x-auto">
        <el-segmented v-model="verdict" :options="verdictOptions" @change="() => { rowsPage = 1; loadRows() }" />
      </div>
      <el-table v-loading="rowsLoading" :data="rows" size="small" class="eq-table-card">
        <el-table-column label="行" prop="row_no" width="64" />
        <el-table-column label="平台订单号" min-width="190">
          <template #default="{ row }">
            <span v-if="row.upstream_order_no" class="code">{{ row.upstream_order_no }}</span>
            <span v-else class="text-gray-400 dark:text-dark-500">—</span>
          </template>
        </el-table-column>
        <el-table-column label="商品" min-width="180">
          <template #default="{ row }"><span class="text-gray-700 dark:text-gray-300">{{ rowPreview(row as ImportRow) }}</span></template>
        </el-table-column>
        <el-table-column label="结论" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="tagOf(VERDICT_TAG, row.verdict)" disable-transitions>{{ labelOf(IMPORT_VERDICT_LABEL, row.verdict) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="问题" min-width="220">
          <template #default="{ row }"><span class="text-gray-700 dark:text-gray-300">{{ row.issues?.join('；') || '—' }}</span></template>
        </el-table-column>
        <template #empty><span class="text-sm text-gray-500 dark:text-dark-400">没有符合条件的行</span></template>
      </el-table>
      <div class="mt-3 flex flex-wrap items-center justify-between gap-3">
        <el-pagination v-model:current-page="rowsPage" background size="small" layout="prev, pager, next" :page-size="20" :total="rowsTotal" @current-change="loadRows" />
        <div>
          <el-button @click="discard">放弃</el-button>
          <el-button type="primary" :loading="committing" @click="commit">提交导入</el-button>
        </div>
      </div>
      </div>
    </el-card>

    <el-card class="eq-table-card" shadow="always">
      <template #header>
        <div class="flex items-center justify-between">
          <h3 class="text-base font-semibold text-gray-900 dark:text-white">导入批次</h3>
          <el-button text size="small" @click="loadBatches"><AppIcon name="refresh" size="sm" class="mr-1" />刷新</el-button>
        </div>
      </template>
      <el-table v-loading="batchesLoading" :data="batches">
        <el-table-column label="文件" min-width="220">
          <template #default="{ row }">
            <div class="text-gray-900 dark:text-gray-100">{{ row.filename }}</div>
            <div class="text-xs text-gray-500 dark:text-dark-400">批次 {{ row.id }}<template v-if="row.sheet_name"> · {{ row.sheet_name }}</template></div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="tagOf(BATCH_STATUS_TAG, row.status)" disable-transitions>{{ labelOf(BATCH_STATUS_LABEL, row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="结论" min-width="240">
          <template #default="{ row }"><span class="text-gray-700 dark:text-gray-300">{{ batchStats(row as ImportBatch) }}</span></template>
        </el-table-column>
        <el-table-column label="上传" width="170">
          <template #default="{ row }">
            <div>{{ formatDateTime(row.created_at) }}</div>
            <div class="text-xs text-gray-500 dark:text-dark-400">{{ row.uploaded_by_name || '—' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="190" align="right">
          <template #default="{ row }">
            <el-button text size="small" :disabled="row.status !== 'committed'" @click="imports.downloadOrdersExport({ batch_id: row.id })">导出结果</el-button>
            <el-button text size="small" :disabled="row.status === 'uploaded'" @click="imports.downloadProblems(row.id)">问题行</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <EmptyState title="还没有导入批次" description="上传文件并提交后，这里可以按批次导出采购结果。" icon="upload" compact />
        </template>
      </el-table>
      <div v-if="batchesTotal > 10" class="flex justify-end border-t border-gray-200 px-4 py-3 dark:border-dark-700">
        <el-pagination v-model:current-page="batchesPage" background layout="prev, pager, next" :page-size="10" :total="batchesTotal" @current-change="loadBatches" />
      </div>
    </el-card>
  </div>
</template>
