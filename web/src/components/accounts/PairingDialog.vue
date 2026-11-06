<script setup lang="ts">
/** 生成配对码：8 位、10 分钟有效、一次性。显示倒计时、复制按钮与插件安装步骤。 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { PairingCode } from '@/api/types'
import * as devicesApi from '@/api/devices'
import AppIcon from '@/components/AppIcon.vue'
import { formatDateTime } from '@/utils/format'
import { notifyFailure, toast } from '@/utils/feedback'

const visible = defineModel<boolean>({ default: false })
const emit = defineEmits<{ closed: [] }>()

const pairing = ref<PairingCode | null>(null)
const busy = ref(false)
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval> | undefined

const remaining = computed(() => {
  if (!pairing.value) return 0
  return Math.max(0, Math.floor((Date.parse(pairing.value.expires_at) - now.value) / 1000))
})
const remainingText = computed(() => {
  const s = remaining.value
  return `${Math.floor(s / 60)} 分 ${String(s % 60).padStart(2, '0')} 秒`
})
const expired = computed(() => !!pairing.value && remaining.value <= 0)
const serverUrl = computed(() => (typeof window !== 'undefined' ? window.location.origin : 'http://127.0.0.1:8790'))

async function generate() {
  busy.value = true
  try {
    pairing.value = await devicesApi.createPairingCode()
    now.value = Date.now()
  } catch (e) {
    pairing.value = null
    notifyFailure('生成配对码', e)
  } finally {
    busy.value = false
  }
}

watch(visible, (open) => {
  clearInterval(timer)
  if (open) {
    void generate()
    timer = setInterval(() => (now.value = Date.now()), 1000)
  } else {
    pairing.value = null
    emit('closed')
  }
})
onBeforeUnmount(() => clearInterval(timer))

async function copy() {
  if (!pairing.value) return
  const code = pairing.value.code
  try {
    await navigator.clipboard.writeText(code)
    toast('配对码已复制')
  } catch {
    // 非安全上下文或无剪贴板权限时退回到选中文本
    const el = document.createElement('textarea')
    el.value = code
    document.body.appendChild(el)
    el.select()
    try {
      document.execCommand('copy')
      toast('配对码已复制')
    } catch {
      notifyFailure('复制', '浏览器不允许访问剪贴板，请手动抄写')
    } finally {
      el.remove()
    }
  }
}
</script>

<template>
  <el-dialog v-model="visible" title="配对新设备" width="600px" append-to-body>
    <div v-loading="busy" class="rounded-2xl bg-gray-50 px-6 py-6 text-center dark:bg-dark-900">
      <template v-if="pairing">
        <div class="text-xs text-gray-500 dark:text-dark-400">配对码</div>
        <div
          class="mt-2 select-all font-mono text-4xl font-bold tracking-[0.3em] text-gray-900 dark:text-white"
          :class="expired ? 'line-through opacity-50' : ''"
        >
          {{ pairing.code }}
        </div>
        <div class="mt-3 text-sm" :class="expired ? 'text-red-600 dark:text-red-400' : 'text-gray-500 dark:text-dark-400'">
          <template v-if="expired">已过期，请重新生成</template>
          <template v-else>剩余 {{ remainingText }} · {{ formatDateTime(pairing.expires_at) }} 失效 · 只能使用一次</template>
        </div>
        <div class="mt-4 flex justify-center gap-2">
          <el-button :disabled="expired" @click="copy"><AppIcon name="copy" size="sm" class="mr-1.5" />复制配对码</el-button>
          <el-button :loading="busy" @click="generate"><AppIcon name="refresh" size="sm" class="mr-1.5" />重新生成</el-button>
        </div>
      </template>
      <div v-else-if="!busy" class="text-sm text-gray-500 dark:text-dark-400">没有拿到配对码，请重新生成。</div>
    </div>

    <ol class="mt-6 space-y-3 text-sm text-gray-700 dark:text-gray-300">
      <li class="flex gap-3">
        <span class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary-100 text-xs font-semibold text-primary-700 dark:bg-primary-900/30 dark:text-primary-400">1</span>
        <span>在 AdsPower 环境或本机 Chrome 打开「扩展程序 → 开发者模式 → 加载已解压的扩展程序」，选择仓库里构建好的 <span class="code">extension/dist</span> 文件夹。一个浏览器环境装一份插件，对应一台设备。</span>
      </li>
      <li class="flex gap-3">
        <span class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary-100 text-xs font-semibold text-primary-700 dark:bg-primary-900/30 dark:text-primary-400">2</span>
        <span>打开插件面板，服务端地址填 <span class="code">{{ serverUrl }}</span>，输入上面的配对码并给设备起名（如 Mac-AdsPower-01）。</span>
      </li>
      <li class="flex gap-3">
        <span class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary-100 text-xs font-semibold text-primary-700 dark:bg-primary-900/30 dark:text-primary-400">3</span>
        <span>配对成功后回到本页，把设备绑定到对应的买家账号；在该浏览器登录这个买家账号，插件会上报身份，显示「身份一致」后才能领取任务。</span>
      </li>
    </ol>
    <template #footer><el-button @click="visible = false">完成</el-button></template>
  </el-dialog>
</template>
