<script setup lang="ts">
/** 账号与设备：买家账号（金额区间、额度、开关、暂停执行）与插件设备（配对、绑定、身份）。当前分区记在 ?tab=。 */
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PageHeader from '@/components/PageHeader.vue'
import AccountsPanel from '@/components/accounts/AccountsPanel.vue'
import DevicesPanel from '@/components/accounts/DevicesPanel.vue'

const route = useRoute()
const router = useRouter()
const tab = ref<'accounts' | 'devices'>(route.query.tab === 'devices' ? 'devices' : 'accounts')
watch(tab, (t) => void router.replace({ query: { ...route.query, tab: t } }))
const accountsPanel = ref<InstanceType<typeof AccountsPanel>>()
</script>

<template>
  <div>
    <PageHeader
      :title="tab === 'devices' ? '插件设备' : '买家账号'"
      :description="tab === 'devices' ? '一个浏览器环境装一份插件，对应一台设备；绑定账号并显示「身份一致」后才能领取任务。' : '「采购开关」只管是否接收新分配；「暂停执行」才会让插件停止为该账号领取任务。'"
    />
    <el-tabs v-model="tab">
      <el-tab-pane label="账号" name="accounts"><AccountsPanel ref="accountsPanel" /></el-tab-pane>
      <el-tab-pane label="设备" name="devices" lazy><DevicesPanel @changed="accountsPanel?.reload()" /></el-tab-pane>
    </el-tabs>
  </div>
</template>
