<script setup lang="ts">
/**
 * 账号与浏览器：「账号」页签是买家账号（买家号 ID、金额区间、额度、开关、暂停执行）与待登记的买家号；
 * 「浏览器」页签是装了插件的浏览器（自动登记，只读，可改名与移除）。当前页签记在 ?tab=。
 */
import { nextTick, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PageHeader from '@/components/PageHeader.vue'
import AccountsPanel from '@/components/accounts/AccountsPanel.vue'
import BrowsersPanel from '@/components/accounts/BrowsersPanel.vue'
import { accountsTab, type AccountsTab } from '@/utils/accounts'

const route = useRoute()
const router = useRouter()
const tab = ref<AccountsTab>(accountsTab(route.query.tab))
watch(tab, (t) => void router.replace({ query: { ...route.query, tab: t } }))
const accountsPanel = ref<InstanceType<typeof AccountsPanel>>()
const browsersPanel = ref<InstanceType<typeof BrowsersPanel>>()

/** 输入：浏览器页签里某个未登记的买家号 ID → 输出：无（切到账号页签，打开已填好 ID 的账号对话框） */
async function registerBuyer(customerId: string) {
  tab.value = 'accounts'
  await nextTick()
  accountsPanel.value?.openRegister(customerId)
}
</script>

<template>
  <div>
    <PageHeader
      :title="tab === 'browsers' ? '浏览器' : '买家账号'"
      :description="tab === 'browsers' ? '装了插件的浏览器在打开亚马逊页面后自动出现在这里。插件读出当前登录的买家号，按它领取对应账号的任务。' : '「采购开关」只管是否接收新分配；「暂停执行」才会让插件停止为该账号领取任务。'"
    />
    <el-tabs v-model="tab">
      <el-tab-pane label="账号" name="accounts">
        <AccountsPanel ref="accountsPanel" :active="tab === 'accounts'" @changed="browsersPanel?.reload()" />
      </el-tab-pane>
      <el-tab-pane label="浏览器" name="browsers" lazy>
        <BrowsersPanel ref="browsersPanel" :active="tab === 'browsers'" @changed="accountsPanel?.reload()" @register="registerBuyer" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>
