<script setup lang="ts">
/**
 * 新建 / 编辑买家账号：买家号 ID、金额区间、每日额度、优先级、配送上限（留空继承全局）、执行模式与时间、卡尾号、开关。
 * 带 preset 打开 = 登记一个插件读到的买家号：买家号 ID 已填好（不可改），名称用来源里的名称预填，提交走新建接口。
 */
import { computed, reactive, ref, watch } from 'vue'
import type { FormInstance, FormRules } from 'element-plus'
import type { Account, AccountInput, PurchaseMode } from '@/api/types'
import * as accountsApi from '@/api/accounts'
import { customerIdProblem, normalizeCustomerId } from '@/utils/accounts'
import { blankToNull, hasAtMostTwoDecimals, normalizeMoneyInput, parseAmount } from '@/utils/format'
import { notifyFailure, notifySuccess } from '@/utils/feedback'

export interface AccountPreset {
  /** 要登记的买家号 ID */
  customerId: string
  /** 来源数据里这个 ID 用过的「买家号」名称；没有为 null */
  name?: string | null
}

const props = defineProps<{ account: Account | null; preset?: AccountPreset | null; defaultMaxDays: number | null }>()
const visible = defineModel<boolean>({ default: false })
const emit = defineEmits<{ saved: [account: Account] }>()

const formRef = ref<FormInstance>()
const busy = ref(false)
const form = reactive({
  name: '',
  amazon_customer_id: '',
  min_amount: '0.00',
  max_amount: '',
  daily_limit: 20,
  priority: 2,
  inherit_days: true,
  max_delivery_days: 10 as number | undefined,
  purchase_mode: 'manual' as PurchaseMode,
  schedule_times: [] as string[],
  expected_card_last4: '',
  purchase_enabled: true,
  sync_enabled: true,
  notes: '',
})

const editing = computed(() => !!props.account)
/** 登记一个插件读到的买家号（新建账号，ID 已定） */
const registering = computed(() => !props.account && !!props.preset?.customerId)
/** 编辑时把已登记的买家号 ID 改成了别的值（或清空） */
const customerIdChanged = computed(
  () => editing.value && !!props.account?.amazon_customer_id && normalizeCustomerId(form.amazon_customer_id) !== props.account.amazon_customer_id,
)
const title = computed(() => {
  if (props.account) return `编辑账号 · ${props.account.name}`
  return registering.value ? `登记买家号 · ${props.preset?.customerId}` : '新建买家账号'
})
const actionName = computed(() => (editing.value ? '保存账号' : registering.value ? '登记买家号' : '创建账号'))

const TIME_OPTIONS = Array.from({ length: 48 }, (_, i) => `${String(Math.floor(i / 2)).padStart(2, '0')}:${i % 2 ? '30' : '00'}`)

watch(visible, (open) => {
  if (!open) return
  const a = props.account
  const preset = a ? null : (props.preset ?? null)
  Object.assign(form, {
    name: a?.name ?? preset?.name?.trim() ?? '',
    amazon_customer_id: a?.amazon_customer_id ?? preset?.customerId ?? '',
    min_amount: a?.min_amount ?? '0.00',
    max_amount: a?.max_amount ?? '',
    daily_limit: a?.daily_limit ?? 20,
    priority: a?.priority ?? 2,
    inherit_days: a ? a.max_delivery_days === null : true,
    max_delivery_days: a?.max_delivery_days ?? a?.effective_max_delivery_days ?? props.defaultMaxDays ?? 10,
    purchase_mode: a?.purchase_mode ?? 'manual',
    schedule_times: [...(a?.schedule_times ?? [])],
    expected_card_last4: a?.expected_card_last4 ?? '',
    purchase_enabled: a?.purchase_enabled ?? true,
    sync_enabled: a?.sync_enabled ?? true,
    notes: a?.notes ?? '',
  })
  formRef.value?.clearValidate()
})

const moneyValidator = (allowEmpty: boolean) => (_r: unknown, v: string, cb: (e?: Error) => void) => {
  if (!v || !String(v).trim()) return allowEmpty ? cb() : cb(new Error('请填写金额'))
  const n = parseAmount(v)
  if (n === null || n < 0) return cb(new Error('请填写不小于 0 的金额，如 20.00'))
  if (!hasAtMostTwoDecimals(v)) return cb(new Error('金额最多两位小数（不会替你四舍五入）'))
  cb()
}

const rules: FormRules = {
  name: [{ required: true, message: '请填写账号名称（与飞书「买家号」取值一致）', trigger: 'blur' }],
  amazon_customer_id: [
    {
      validator: (_r: unknown, v: string, cb: (e?: Error) => void) => {
        const problem = customerIdProblem(v)
        if (problem) return cb(new Error(problem))
        cb()
      },
      trigger: 'blur',
    },
  ],
  min_amount: [{ validator: moneyValidator(false), trigger: 'blur' }],
  max_amount: [
    { validator: moneyValidator(true), trigger: 'blur' },
    {
      validator: (_r: unknown, v: string, cb: (e?: Error) => void) => {
        const hi = parseAmount(v)
        const lo = parseAmount(form.min_amount)
        if (hi !== null && lo !== null && hi < lo) return cb(new Error('上限不能小于下限'))
        cb()
      },
      trigger: 'blur',
    },
  ],
  expected_card_last4: [{ pattern: /^\d{4}$/, message: '卡尾号为 4 位数字', trigger: 'blur' }],
  schedule_times: [
    {
      validator: (_r: unknown, v: string[], cb: (e?: Error) => void) => {
        if (form.purchase_mode === 'scheduled' && !v.length) return cb(new Error('定时执行至少设置一个时间'))
        if (v.some((t) => !/^([01]\d|2[0-3]):[0-5]\d$/.test(t))) return cb(new Error('时间格式为 HH:MM（北京时间）'))
        cb()
      },
      trigger: 'change',
    },
  ],
}

async function submit() {
  if (!(await formRef.value?.validate().catch(() => false))) return
  const customerId = normalizeCustomerId(form.amazon_customer_id)
  const body: AccountInput = {
    name: form.name.trim(),
    min_amount: normalizeMoneyInput(form.min_amount) ?? '0.00',
    max_amount: normalizeMoneyInput(form.max_amount),
    daily_limit: form.daily_limit,
    priority: form.priority,
    max_delivery_days: form.inherit_days ? null : (form.max_delivery_days ?? null),
    purchase_mode: form.purchase_mode,
    schedule_times: [...form.schedule_times].sort(),
    expected_card_last4: blankToNull(form.expected_card_last4),
    purchase_enabled: form.purchase_enabled,
    sync_enabled: form.sync_enabled,
    notes: blankToNull(form.notes),
  }
  // 编辑时只有改了买家号 ID 才带上它：账号正在执行时服务端不允许改 ID，别的字段照样能保存
  if (!props.account || customerId !== (props.account.amazon_customer_id ?? null)) body.amazon_customer_id = customerId
  busy.value = true
  try {
    const saved = props.account ? await accountsApi.updateAccount(props.account.id, body) : await accountsApi.createAccount(body)
    notifySuccess(editing.value ? '账号已保存' : registering.value ? '买家号已登记' : '账号已创建', '分配会按新的配置重新计算。')
    emit('saved', saved)
    visible.value = false
  } catch (e) {
    notifyFailure(actionName.value, e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <el-dialog v-model="visible" :title="title" width="760px" top="5vh" append-to-body :close-on-click-modal="false">
    <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
      <div class="grid gap-x-6 md:grid-cols-2">
        <el-form-item label="账号名称" prop="name">
          <el-input v-model="form.name" placeholder="与飞书「买家号」一致，如 环境-DEMO-01" />
        </el-form-item>
        <el-form-item :label="registering ? '买家号 ID' : '买家号 ID（可选）'" prop="amazon_customer_id">
          <el-input v-model="form.amazon_customer_id" class="font-mono" :readonly="registering" placeholder="A 开头，如 A1DEMO00000" aria-label="买家号 ID" />
        </el-form-item>
      </div>
      <p class="-mt-3 mb-4 text-xs text-gray-500 dark:text-dark-400">
        <template v-if="registering">买家号 ID 由插件从浏览器登录的亚马逊页面读出。登记后，登录着这个买家号的浏览器就能领取这个账号的任务；请确认名称、金额区间与每日单数。</template>
        <template v-else>亚马逊 customerId。留空也可以：登录着这个买家号的浏览器装上插件后，会出现在上面的待登记列表里。</template>
        <span v-if="customerIdChanged" class="mt-1 block text-amber-700 dark:text-amber-400">
          修改买家号 ID 后，这个账号的任务改由登录着新买家号的浏览器执行；账号正在执行或有结果待核实的采购时不能修改。
        </span>
      </p>
      <div class="grid gap-x-6 md:grid-cols-4">
        <el-form-item label="金额下限（USD）" prop="min_amount">
          <el-input v-model="form.min_amount" placeholder="0.00"><template #prefix>$</template></el-input>
        </el-form-item>
        <el-form-item label="金额上限（USD）" prop="max_amount">
          <el-input v-model="form.max_amount" placeholder="留空表示不限"><template #prefix>$</template></el-input>
        </el-form-item>
        <el-form-item label="每日最多单数" prop="daily_limit">
          <el-input-number v-model="form.daily_limit" :min="0" :max="500" :controls="false" style="width: 100%" />
        </el-form-item>
        <el-form-item label="优先级（0–9）" prop="priority">
          <el-input-number v-model="form.priority" :min="0" :max="9" style="width: 100%" />
        </el-form-item>
      </div>
      <p class="-mt-3 mb-4 text-xs text-gray-500 dark:text-dark-400">
        金额区间按商品单价 × 数量匹配（端点含，不含税费运费）。每日单数按北京时间统计，一次成功计 1 单。优先级 9 最高，默认 2。
      </p>
      <div class="grid gap-x-6 md:grid-cols-2">
        <el-form-item label="最长配送天数">
          <div class="flex w-full items-center gap-3">
            <el-checkbox v-model="form.inherit_days">继承全局默认<template v-if="defaultMaxDays !== null">（{{ defaultMaxDays }} 天）</template></el-checkbox>
            <el-input-number v-if="!form.inherit_days" v-model="form.max_delivery_days" :min="1" :max="60" style="width: 9rem" />
          </div>
        </el-form-item>
        <el-form-item label="期望卡尾号（可选）" prop="expected_card_last4">
          <el-input v-model="form.expected_card_last4" maxlength="4" placeholder="设置后结算卡尾号必须相同" />
        </el-form-item>
      </div>
      <div class="grid gap-x-6 md:grid-cols-2">
        <el-form-item label="执行模式">
          <el-radio-group v-model="form.purchase_mode">
            <el-radio-button value="manual">手动执行</el-radio-button>
            <el-radio-button value="scheduled">定时执行</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="执行时间（北京时间）" prop="schedule_times">
          <el-select
            v-model="form.schedule_times"
            multiple
            filterable
            allow-create
            default-first-option
            class="w-full"
            :disabled="form.purchase_mode !== 'scheduled'"
            placeholder="如 14:00"
          >
            <el-option v-for="t in TIME_OPTIONS" :key="t" :value="t" :label="t" />
          </el-select>
        </el-form-item>
      </div>
      <p class="-mt-3 mb-4 text-xs text-gray-500 dark:text-dark-400">
        定时执行：到点后，由登录着这个买家号并开着亚马逊页面的浏览器自动开始；同一个时间点每天只执行一次。
      </p>
      <div class="mb-5 divide-y divide-gray-100 rounded-xl border border-gray-100 dark:divide-dark-700 dark:border-dark-700">
        <div class="flex items-center justify-between gap-4 px-4 py-3">
          <div>
            <p class="text-sm font-medium text-gray-900 dark:text-white">采购开关</p>
            <p class="text-xs text-gray-500 dark:text-dark-400">关闭后不再接收新分配，已分配的任务照常执行；它不是停止执行按钮。默认开启。</p>
          </div>
          <el-switch v-model="form.purchase_enabled" />
        </div>
        <div class="flex items-center justify-between gap-4 px-4 py-3">
          <div>
            <p class="text-sm font-medium text-gray-900 dark:text-white">同步开关</p>
            <p class="text-xs text-gray-500 dark:text-dark-400">控制该账号的订单与物流同步（第二个 PR 启用）。默认开启。</p>
          </div>
          <el-switch v-model="form.sync_enabled" />
        </div>
      </div>
      <el-form-item label="备注">
        <el-input v-model="form.notes" type="textarea" :rows="2" placeholder="可选" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="busy" @click="submit">{{ editing ? '保存' : registering ? '登记' : '创建' }}</el-button>
    </template>
  </el-dialog>
</template>
