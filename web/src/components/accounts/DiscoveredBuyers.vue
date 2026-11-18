<script setup lang="ts">
/**
 * 待登记的买家号：插件在浏览器里读到、还没登记到任何账号的买家号 ID（GET /accounts/discovered）。
 * 有条目才显示；页面可见时每 10 秒自动刷新。每行三个动作：
 * 「登记」（交给上层打开账号对话框，ID 与名称已填）、「填到已有账号」（选一个还没登记 ID 的账号，确认后 PATCH）、「忽略」。
 * 底部「已忽略 N 个」可展开，每行「恢复」。
 */
import { computed, onMounted, ref } from 'vue'
import type { Account, DiscoveredBuyer } from '@/api/types'
import * as accountsApi from '@/api/accounts'
import AppIcon from '@/components/AppIcon.vue'
import StatusDot from '@/components/StatusDot.vue'
import { useVisiblePolling } from '@/composables/useVisiblePolling'
import { IGNORE_CONFIRM_TEXT, fillConfirmText, fillTargets, splitDiscovered, suggestedFillTarget } from '@/utils/accounts'
import { copyText } from '@/utils/clipboard'
import { failureText, formatDateTime, formatNumber, formatRelative } from '@/utils/format'
import { confirmAction, notifyFailure, notifySuccess, toast } from '@/utils/feedback'

const props = withDefaults(defineProps<{ accounts: Account[]; /** 所在页签是否激活：没激活时不自动刷新 */ active?: boolean }>(), { active: true })
const emit = defineEmits<{
  /** 点了「登记」：上层打开账号对话框 */
  register: [item: DiscoveredBuyer]
  /** 把 ID 填到了已有账号上：上层重新读账号列表 */
  changed: []
}>()

const items = ref<DiscoveredBuyer[]>([])
const loadError = ref('')
const busy = ref<string | null>(null)
const showIgnored = ref(false)
/** 正在进行的读取次数、最新一次读取的序号：较早发出的读取晚回来时不覆盖较新的结果 */
let inFlight = 0
let latest = 0

const split = computed(() => splitDiscovered(items.value))
const pending = computed(() => split.value.pending)
const ignored = computed(() => split.value.ignored)

/**
 * 输入：是否是自动刷新 → 输出：无（重新读列表）。自动刷新在还有读取没回来时跳过；动作之后的读取总是发出，
 * 并让更早发出的读取作废。失败只在本区内写一行，不弹通知。
 */
async function load(auto = false) {
  if (auto && inFlight > 0) return
  const mine = ++latest
  inFlight += 1
  try {
    const list = await accountsApi.listDiscovered(true)
    if (mine !== latest) return
    items.value = list
    loadError.value = ''
  } catch (e) {
    if (mine === latest) loadError.value = failureText('读取待登记的买家号', e)
  } finally {
    inFlight -= 1
  }
}
onMounted(() => load())
useVisiblePolling(() => load(true), { enabled: () => props.active })
defineExpose({ reload: () => load() })

const targetsOf = (item: DiscoveredBuyer) => fillTargets(props.accounts, item)
const suggestedOf = (item: DiscoveredBuyer) => suggestedFillTarget(props.accounts, item)

async function copy(id: string) {
  if (await copyText(id)) toast('买家号 ID 已复制')
  else notifyFailure('复制买家号 ID', '浏览器不允许访问剪贴板，请手动选中后复制')
}

async function fill(item: DiscoveredBuyer, accountId: unknown) {
  const target = props.accounts.find((a) => a.id === Number(accountId))
  if (!target) return
  const ok = await confirmAction({ message: fillConfirmText(item.customer_id, target.name), confirmText: '登记' })
  if (!ok) return
  busy.value = `fill-${item.customer_id}`
  try {
    await accountsApi.updateAccount(target.id, { amazon_customer_id: item.customer_id })
    notifySuccess(`已把买家号 ID 登记到「${target.name}」`, '登录着这个买家号的浏览器现在可以领取它的任务。')
    emit('changed')
    await load()
  } catch (e) {
    notifyFailure('登记买家号', e)
  } finally {
    busy.value = null
  }
}

async function ignore(item: DiscoveredBuyer) {
  const ok = await confirmAction({ message: IGNORE_CONFIRM_TEXT, confirmText: '忽略' })
  if (!ok) return
  busy.value = `ignore-${item.customer_id}`
  try {
    await accountsApi.ignoreDiscovered(item.customer_id)
    notifySuccess('已忽略这个买家号')
    await load()
  } catch (e) {
    notifyFailure('忽略买家号', e)
  } finally {
    busy.value = null
  }
}

async function restore(item: DiscoveredBuyer) {
  busy.value = `restore-${item.customer_id}`
  try {
    await accountsApi.unignoreDiscovered(item.customer_id)
    notifySuccess('已恢复，买家号回到待登记列表')
    await load()
  } catch (e) {
    notifyFailure('恢复买家号', e)
  } finally {
    busy.value = null
  }
}
</script>

<template>
  <div v-if="pending.length || ignored.length || loadError" class="mb-6" data-testid="discovered-buyers">
    <el-alert v-if="loadError && !pending.length && !ignored.length" type="warning" show-icon :closable="false" :title="loadError">
      <template #default><el-button size="small" class="mt-2" @click="load()">重试</el-button></template>
    </el-alert>

    <el-card v-else class="eq-table-card" shadow="always">
      <template v-if="pending.length" #header>
        <div class="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
          <div class="flex items-center gap-2">
            <AppIcon name="userPlus" class="text-amber-600 dark:text-amber-400" />
            <h3 class="text-base font-semibold text-gray-900 dark:text-white">待登记的买家号</h3>
            <el-tag size="small" type="warning" disable-transitions>{{ formatNumber(pending.length) }} 个</el-tag>
          </div>
          <p class="text-xs font-normal text-gray-500 dark:text-dark-400">插件在浏览器里读到了这些买家号。登记后才会分到任务；这里每 10 秒自动刷新。</p>
        </div>
      </template>

      <ul v-if="pending.length" class="divide-y divide-gray-100 dark:divide-dark-700">
        <li v-for="item in pending" :key="item.customer_id" class="flex flex-wrap items-center gap-x-6 gap-y-3 px-6 py-4">
          <div class="min-w-0 flex-1 basis-56">
            <div class="flex items-center gap-1">
              <span class="code pc-code-wrap select-all">{{ item.customer_id }}</span>
              <el-tooltip content="复制买家号 ID" placement="top">
                <el-button text size="small" aria-label="复制买家号 ID" @click="copy(item.customer_id)"><AppIcon name="copy" size="sm" /></el-button>
              </el-tooltip>
            </div>
            <p v-if="suggestedOf(item)" class="mt-1 text-xs text-amber-700 dark:text-amber-400">
              来源里这个买家号叫「{{ item.name_hint }}」，已有同名账号还没登记买家号 ID，可以直接填到它上面。
            </p>
            <p v-else-if="item.name_hint" class="mt-1 text-xs text-gray-500 dark:text-dark-400">来源里这个买家号叫「{{ item.name_hint }}」。</p>
          </div>
          <div class="min-w-0 basis-48">
            <div class="text-xs text-gray-500 dark:text-dark-400">所在浏览器</div>
            <div class="mt-1 flex flex-wrap gap-x-3 gap-y-1">
              <StatusDot v-for="b in item.browsers" :key="b.id" :color="b.online ? 'green' : 'gray'" :label="b.online ? b.name : `${b.name}（离线）`" />
              <span v-if="!item.browsers.length" class="text-sm text-gray-400 dark:text-dark-500">—</span>
            </div>
          </div>
          <div class="basis-28">
            <div class="text-xs text-gray-500 dark:text-dark-400">最近上报</div>
            <div class="mt-1 whitespace-nowrap text-sm text-gray-700 dark:text-gray-300" :title="formatDateTime(item.last_reported_at)">{{ formatRelative(item.last_reported_at) }}</div>
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <el-button type="primary" size="small" @click="emit('register', item)">登记</el-button>
            <el-tooltip :disabled="!!targetsOf(item).length" content="没有还没登记买家号 ID 的账号" placement="top">
              <span>
                <el-dropdown trigger="click" :disabled="!targetsOf(item).length" max-height="320px" @command="(id: unknown) => fill(item, id)">
                  <el-button size="small" :disabled="!targetsOf(item).length" :loading="busy === `fill-${item.customer_id}`">
                    填到已有账号<AppIcon name="chevronDown" size="sm" class="ml-1" />
                  </el-button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item v-for="a in targetsOf(item)" :key="a.id" :command="a.id">
                        {{ a.name }}<span v-if="a.name === item.name_hint" class="ml-2 text-xs text-gray-500 dark:text-dark-400">与来源里的名称相同</span>
                      </el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
              </span>
            </el-tooltip>
            <el-button text size="small" :loading="busy === `ignore-${item.customer_id}`" @click="ignore(item)">忽略</el-button>
          </div>
        </li>
      </ul>

      <div v-if="ignored.length" class="px-6 py-3" :class="pending.length ? 'border-t border-gray-100 dark:border-dark-700' : ''">
        <button
          type="button"
          class="inline-flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700 dark:text-dark-400 dark:hover:text-gray-200"
          :aria-expanded="showIgnored"
          @click="showIgnored = !showIgnored"
        >
          <AppIcon :name="showIgnored ? 'chevronDown' : 'chevronRight'" size="sm" />已忽略 {{ formatNumber(ignored.length) }} 个
        </button>
        <ul v-if="showIgnored" class="mt-2 divide-y divide-gray-100 dark:divide-dark-700">
          <li v-for="item in ignored" :key="item.customer_id" class="flex flex-wrap items-center justify-between gap-x-6 gap-y-2 py-2.5">
            <div class="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1">
              <span class="code pc-code-wrap select-all">{{ item.customer_id }}</span>
              <StatusDot v-for="b in item.browsers" :key="b.id" :color="b.online ? 'green' : 'gray'" :label="b.online ? b.name : `${b.name}（离线）`" />
              <span class="whitespace-nowrap text-xs text-gray-500 dark:text-dark-400" :title="formatDateTime(item.last_reported_at)">最近上报 {{ formatRelative(item.last_reported_at) }}</span>
            </div>
            <el-button size="small" :loading="busy === `restore-${item.customer_id}`" @click="restore(item)">恢复</el-button>
          </li>
        </ul>
        <p v-if="showIgnored" class="mt-2 text-xs text-gray-500 dark:text-dark-400">已忽略的买家号不会分到任务；恢复后回到上面的待登记列表。</p>
      </div>

      <p v-if="loadError" class="border-t border-gray-100 px-6 py-2 text-xs text-amber-700 dark:border-dark-700 dark:text-amber-400">{{ loadError }}。显示的是上一次读到的内容。</p>
    </el-card>
  </div>
</template>
