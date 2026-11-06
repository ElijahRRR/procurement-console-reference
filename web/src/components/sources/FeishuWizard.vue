<script setup lang="ts">
/**
 * 飞书连接向导：粘贴链接 → 选数据表 → 字段对应（含写回列，不可写列禁用）→ 准入规则 → 样本预览 → 保存。
 * 链接只用来定位表；应用凭证在服务端。预览只读，不修改飞书数据。
 * 来源字段可对应多列（有序）：前一列为空时读下一列，如 ASIN 缺失时用「来源码」（utils/feishuMap）。
 */
import { computed, reactive, ref, watch } from 'vue'
import type {
  FeishuAdmission,
  FeishuConnection,
  FeishuField,
  FeishuPreview,
  FeishuTable,
  SystemFields,
} from '@/api/types'
import * as feishu from '@/api/feishu'
import AppIcon from '@/components/AppIcon.vue'
import { displayValue, failureText, formatNumber } from '@/utils/format'
import { FEISHU_VERDICT_LABEL, SYNC_STAT_LABEL, VERDICT_TAG, labelOf, tagOf } from '@/utils/labels'
import { ADMISSION_FIELDS, FEISHU_ADMISSION_DEFAULTS, defaultsHint, fullAdmission } from '@/utils/admission'
import { fillSuggested, missingRequired as missingRequiredOf, toEditableMap, toFieldMap } from '@/utils/feishuMap'
import { notifyFailure, notifySuccess } from '@/utils/feedback'

const props = defineProps<{ connection: FeishuConnection | null }>()
const visible = defineModel<boolean>({ default: false })
const emit = defineEmits<{ saved: [conn: FeishuConnection] }>()

const STEPS = ['粘贴链接', '选择数据表', '字段对应', '准入规则', '样本预览'] as const
const step = ref(0)
const busy = ref(false)
const error = ref('')

const form = reactive({
  name: '',
  link: '',
  app_token: '',
  source_table_id: '',
  source_view_id: '' as string,
  result_table_id: '',
  /** {系统字段: [列名…]}：有序，前一列为空时读下一列 */
  field_map: {} as Record<string, string[]>,
  writeback_map: {} as Record<string, string>,
  writeback_enabled: false,
  poll_minutes: 10,
  admission: fullAdmission(null, FEISHU_ADMISSION_DEFAULTS) as FeishuAdmission,
})

const tables = ref<FeishuTable[]>([])
const systemFields = ref<SystemFields | null>(null)
const sourceFields = ref<FeishuField[]>([])
const resultFields = ref<FeishuField[]>([])
const preview = ref<FeishuPreview | null>(null)

const editing = computed(() => !!props.connection)

function reset() {
  step.value = 0
  error.value = ''
  preview.value = null
  tables.value = []
  sourceFields.value = []
  resultFields.value = []
  const c = props.connection
  Object.assign(form, {
    name: c?.name ?? '',
    link: c?.link ?? '',
    app_token: c?.app_token ?? '',
    source_table_id: c?.source_table_id ?? '',
    source_view_id: c?.source_view_id ?? '',
    result_table_id: c?.result_table_id ?? '',
    field_map: toEditableMap(c?.field_map),
    writeback_map: { ...(c?.writeback_map ?? {}) },
    writeback_enabled: c?.writeback_enabled ?? false,
    poll_minutes: c?.poll_minutes ?? 10,
    admission: fullAdmission(c?.admission, FEISHU_ADMISSION_DEFAULTS),
  })
}

watch(visible, (open) => {
  if (open) reset()
})

/** 输入：动作名、异步函数 → 输出：成功为 true；失败在向导内显示「动作失败：原因」 */
async function guarded(action: string, fn: () => Promise<void>): Promise<boolean> {
  busy.value = true
  error.value = ''
  try {
    await fn()
    return true
  } catch (e) {
    error.value = failureText(action, e)
    return false
  } finally {
    busy.value = false
  }
}

async function resolveLink() {
  const link = form.link.trim()
  if (!link) {
    error.value = '请粘贴多维表格链接'
    return false
  }
  return guarded('解析链接', async () => {
    const res = await feishu.resolveLink(link)
    form.app_token = res.app_token
    tables.value = res.tables ?? []
    if (!form.source_table_id || !tables.value.some((t) => t.table_id === form.source_table_id)) {
      form.source_table_id = res.table_id ?? tables.value[0]?.table_id ?? ''
    }
    if (res.view_id && !form.source_view_id) form.source_view_id = res.view_id
    if (!form.result_table_id) form.result_table_id = form.source_table_id
    if (!form.name) form.name = tables.value.find((t) => t.table_id === form.source_table_id)?.name ?? '飞书订单表'
  })
}

async function loadFields() {
  return guarded('读取字段', async () => {
    const [sys, src] = await Promise.all([
      systemFields.value ? Promise.resolve(systemFields.value) : feishu.getSystemFields(),
      feishu.getFields(form.app_token, form.source_table_id),
    ])
    systemFields.value = sys
    sourceFields.value = src.fields ?? []
    if (form.result_table_id && form.result_table_id !== form.source_table_id) {
      resultFields.value = (await feishu.getFields(form.app_token, form.result_table_id)).fields ?? []
    } else {
      resultFields.value = sourceFields.value
    }
    // 新建时用服务端建议的对应（多个候选列保留为有序列表）；编辑时保留已保存的对应，只补空缺
    fillSuggested(form.field_map, src.suggested_field_map)
    for (const [k, v] of Object.entries(src.suggested_writeback_map ?? {})) {
      if (!form.writeback_map[k] && v) form.writeback_map[k] = v
    }
  })
}

const missingRequired = computed(() => missingRequiredOf(systemFields.value?.source_fields ?? [], form.field_map))

async function runPreview() {
  return guarded('样本预览', async () => {
    preview.value = await feishu.previewFeishu({
      app_token: form.app_token,
      table_id: form.source_table_id,
      view_id: form.source_view_id || null,
      field_map: toFieldMap(form.field_map),
      admission: form.admission,
      limit: 20,
    })
  })
}

function cleanMap(m: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(m).filter(([, v]) => !!v))
}

async function next() {
  if (step.value === 0) {
    if (await resolveLink()) step.value = 1
  } else if (step.value === 1) {
    if (!form.source_table_id) {
      error.value = '请选择订单所在的数据表'
      return
    }
    if (!form.result_table_id) form.result_table_id = form.source_table_id
    if (await loadFields()) step.value = 2
  } else if (step.value === 2) {
    if (missingRequired.value.length) {
      error.value = `请先对应必填字段：${missingRequired.value.join('、')}`
      return
    }
    error.value = ''
    step.value = 3
  } else if (step.value === 3) {
    if (await runPreview()) step.value = 4
  }
}

function prev() {
  error.value = ''
  if (step.value > 0) step.value -= 1
}

async function save() {
  const body = {
    name: form.name.trim() || '飞书订单表',
    link: form.link.trim(),
    app_token: form.app_token,
    source_table_id: form.source_table_id,
    source_view_id: form.source_view_id || null,
    result_table_id: form.result_table_id || form.source_table_id,
    field_map: toFieldMap(form.field_map),
    writeback_map: cleanMap(form.writeback_map),
    writeback_enabled: form.writeback_enabled,
    admission: form.admission,
    poll_minutes: form.poll_minutes,
  }
  busy.value = true
  error.value = ''
  try {
    const saved = props.connection
      ? await feishu.updateConnection(props.connection.id, body)
      : await feishu.createConnection({ ...body, enabled: true })
    notifySuccess(
      editing.value ? '连接已保存' : '连接已创建',
      form.writeback_enabled ? '写回需在测试记录上验收通过后才会发送，界面显示「回填待验证」。' : '',
    )
    emit('saved', saved)
    visible.value = false
  } catch (e) {
    error.value = failureText('保存连接', e)
    notifyFailure('保存连接', e)
  } finally {
    busy.value = false
  }
}

const fieldOptions = computed(() => sourceFields.value.map((f) => ({ value: f.field_name, label: f.field_name, type: f.ui_type ?? String(f.type) })))
const resultOptions = computed(() =>
  resultFields.value.map((f) => ({ value: f.field_name, label: f.field_name, writable: f.writable, type: f.ui_type ?? String(f.type) })),
)

const admissionFields = ADMISSION_FIELDS

const previewRows = computed(() =>
  (preview.value?.records ?? []).map((r) => ({
    ...r,
    order_no: displayValue(r.record?.upstream_order_no),
    asin: displayValue(r.record?.asin),
    quantity: displayValue(r.record?.quantity),
    price_cap: displayValue(r.record?.price_cap),
  })),
)
</script>

<template>
  <el-dialog v-model="visible" :title="editing ? '编辑飞书连接' : '连接飞书多维表格'" width="880px" top="5vh" append-to-body :close-on-click-modal="false">
    <el-steps :active="step" finish-status="success" align-center class="mb-6">
      <el-step v-for="s in STEPS" :key="s" :title="s" />
    </el-steps>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="mb-4" />

    <!-- 1. 链接 -->
    <div v-if="step === 0">
      <el-form label-position="top">
        <el-form-item label="多维表格链接">
          <el-input v-model="form.link" placeholder="https://<租户>.feishu.cn/base/<app_token>?table=<table_id>&view=<view_id>" clearable>
            <template #prefix><AppIcon name="link" size="sm" /></template>
          </el-input>
          <p class="mt-1.5 text-xs text-gray-500 dark:text-dark-400">
            在多维表格右上角「分享」里复制链接；知识库（/wiki/）链接也可以。链接只用来定位表，表格需先在「…→ 更多 → 添加文档应用」里添加本系统的飞书应用。
          </p>
        </el-form-item>
        <el-form-item label="连接名称">
          <el-input v-model="form.name" placeholder="如 订单中心 · 采购订单" />
        </el-form-item>
      </el-form>
    </div>

    <!-- 2. 数据表 -->
    <div v-else-if="step === 1">
      <el-form label-position="top">
        <div class="grid gap-x-6 md:grid-cols-2">
          <el-form-item label="订单所在的数据表">
            <el-select v-model="form.source_table_id" class="w-full" placeholder="选择数据表">
              <el-option v-for="t in tables" :key="t.table_id" :value="t.table_id" :label="t.name">
                <span>{{ t.name }}</span><span class="ml-2 font-mono text-xs text-gray-400 dark:text-dark-500">{{ t.table_id }}</span>
              </el-option>
            </el-select>
          </el-form-item>
          <el-form-item label="结果写回的数据表">
            <el-select v-model="form.result_table_id" class="w-full" placeholder="默认与订单表相同">
              <el-option v-for="t in tables" :key="t.table_id" :value="t.table_id" :label="t.name" />
            </el-select>
          </el-form-item>
          <el-form-item label="视图 ID（可选）">
            <el-input v-model="form.source_view_id" placeholder="只读取该视图里的记录" />
          </el-form-item>
          <el-form-item label="读取间隔（分钟）">
            <el-input-number v-model="form.poll_minutes" :min="1" :max="1440" :controls="false" style="width: 100%" />
            <p class="mt-1.5 text-xs text-gray-500 dark:text-dark-400">1–1440 分钟，默认 10 分钟。</p>
          </el-form-item>
        </div>
      </el-form>
    </div>

    <!-- 3. 字段对应 -->
    <div v-else-if="step === 2" class="space-y-6">
      <div>
        <h4 class="mb-2 text-sm font-semibold text-gray-900 dark:text-white">来源字段</h4>
        <p class="mb-3 text-xs text-gray-500 dark:text-dark-400">
          左侧是系统字段，右侧选择飞书表里对应的列。只读取用到的列。带 * 的必须对应。可以按顺序选多列：前一列为空时读下一列（如 ASIN 缺失时用「来源码」）。
        </p>
        <el-table :data="systemFields?.source_fields ?? []" size="small" max-height="300" class="eq-table-card">
          <el-table-column label="系统字段" min-width="160">
            <template #default="{ row }">
              <span class="text-gray-900 dark:text-gray-100">{{ row.label }}</span>
              <span v-if="row.required" class="ml-1 text-red-500">*</span>
            </template>
          </el-table-column>
          <el-table-column label="飞书列" min-width="240">
            <template #default="{ row }">
              <el-select
                :model-value="form.field_map[row.key] ?? []"
                size="small"
                class="w-full"
                multiple
                filterable
                clearable
                placeholder="不对应"
                :aria-label="`${row.label} 对应的飞书列`"
                @update:model-value="(v: string[]) => (form.field_map[row.key] = v)"
              >
                <el-option v-for="o in fieldOptions" :key="o.value" :value="o.value" :label="o.label">
                  <span>{{ o.label }}</span><span class="ml-2 text-xs text-gray-400 dark:text-dark-500">{{ o.type }}</span>
                </el-option>
              </el-select>
            </template>
          </el-table-column>
        </el-table>
      </div>
      <div>
        <div class="mb-2 flex items-center justify-between gap-4">
          <h4 class="text-sm font-semibold text-gray-900 dark:text-white">写回对应</h4>
          <div class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
            启用写回 <el-switch v-model="form.writeback_enabled" />
          </div>
        </div>
        <p class="mb-3 text-xs text-gray-500 dark:text-dark-400">
          采购结果写到结果表的这些列。公式、查找引用、关联与系统列不能作为写回目标。启用后需在测试记录上验收通过才会真正写回；模拟结果永不写回。
        </p>
        <el-table :data="systemFields?.result_fields ?? []" size="small" max-height="240" class="eq-table-card">
          <el-table-column label="结果字段" prop="label" min-width="160" />
          <el-table-column label="飞书列" min-width="240">
            <template #default="{ row }">
              <el-select v-model="form.writeback_map[row.key]" size="small" class="w-full" filterable clearable placeholder="不写回" :disabled="!form.writeback_enabled">
                <el-option v-for="o in resultOptions" :key="o.value" :value="o.value" :label="o.label" :disabled="!o.writable">
                  <span>{{ o.label }}</span>
                  <span class="ml-2 text-xs text-gray-400 dark:text-dark-500">{{ o.writable ? o.type : `${o.type} · 不可写` }}</span>
                </el-option>
              </el-select>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </div>

    <!-- 4. 准入 -->
    <div v-else-if="step === 3">
      <el-form label-position="top">
        <div class="grid gap-x-6 md:grid-cols-2">
          <el-form-item v-for="f in admissionFields" :key="f.key" :label="f.label">
            <el-select
              v-model="form.admission[f.key]"
              multiple
              filterable
              allow-create
              default-first-option
              :reserve-keyword="false"
              class="w-full"
              placeholder="输入后回车添加"
            />
            <p class="mt-1.5 text-xs text-gray-500 dark:text-dark-400">{{ f.hint }}{{ defaultsHint(FEISHU_ADMISSION_DEFAULTS[f.key]) }}</p>
          </el-form-item>
        </div>
        <div class="flex items-center justify-between gap-4 rounded-xl border border-gray-100 px-4 py-3 dark:border-dark-700">
          <div>
            <p class="text-sm font-medium text-gray-900 dark:text-white">导入历史已采购的行</p>
            <p class="text-xs text-gray-500 dark:text-dark-400">已带 AMZ 订单号的行记为已采购（不重新下单），用于物流同步与对账。默认关闭。</p>
          </div>
          <el-switch v-model="form.admission.import_historical" />
        </div>
      </el-form>
    </div>

    <!-- 5. 预览 -->
    <div v-else-if="step === 4">
      <p class="mb-3 text-sm text-gray-500 dark:text-dark-400">按当前对应与准入规则读取前 20 条记录的结论（只读，不写入任务）。</p>
      <div class="mb-4 flex flex-wrap gap-2">
        <span
          v-for="(n, k) in preview?.stats ?? {}"
          :key="k"
          class="inline-flex items-center gap-1.5 rounded-lg bg-gray-100 px-2.5 py-1 text-sm text-gray-700 dark:bg-dark-800 dark:text-gray-300"
        >
          {{ SYNC_STAT_LABEL[k] ?? k }} <span class="font-semibold text-gray-900 dark:text-white">{{ formatNumber(n) }}</span>
        </span>
      </div>
      <el-table :data="previewRows" size="small" max-height="360" class="eq-table-card">
        <el-table-column label="记录" width="130">
          <template #default="{ row }"><span class="font-mono text-xs text-gray-500 dark:text-dark-400">{{ row.record_id }}</span></template>
        </el-table-column>
        <el-table-column label="平台订单号" min-width="180">
          <template #default="{ row }"><span class="code">{{ row.order_no }}</span></template>
        </el-table-column>
        <el-table-column label="ASIN" prop="asin" width="120" />
        <el-table-column label="数量" prop="quantity" width="64" align="right" />
        <el-table-column label="限价" prop="price_cap" width="88" align="right" />
        <el-table-column label="结论" width="88">
          <template #default="{ row }">
            <el-tag size="small" :type="tagOf(VERDICT_TAG, row.verdict)" disable-transitions>{{ labelOf(FEISHU_VERDICT_LABEL, row.verdict) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="说明" min-width="200">
          <template #default="{ row }">
            <span class="text-gray-700 dark:text-gray-300">{{ [row.reason, ...(row.issues ?? [])].filter(Boolean).join('；') || '—' }}</span>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <template #footer>
      <div class="flex items-center justify-between">
        <el-button v-if="step > 0" :disabled="busy" @click="prev">上一步</el-button>
        <span v-else />
        <div>
          <el-button @click="visible = false">取消</el-button>
          <el-button v-if="step === 4" :loading="busy" @click="runPreview">重新预览</el-button>
          <el-button v-if="step < 4" type="primary" :loading="busy" @click="next">{{ step === 3 ? '预览样本' : '下一步' }}</el-button>
          <el-button v-else type="primary" :loading="busy" @click="save">保存连接</el-button>
        </div>
      </div>
    </template>
  </el-dialog>
</template>
