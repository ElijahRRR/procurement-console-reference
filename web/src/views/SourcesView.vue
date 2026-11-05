<script setup lang="ts">
/** 订单来源：飞书连接、文件导入、写回队列三个分区（el-tabs）。当前分区记在地址栏 ?tab=。 */
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PageHeader from '@/components/PageHeader.vue'
import FeishuPanel from '@/components/sources/FeishuPanel.vue'
import ImportPanel from '@/components/sources/ImportPanel.vue'
import WritebackPanel from '@/components/sources/WritebackPanel.vue'

const TABS = ['feishu', 'file', 'writebacks'] as const
type Tab = (typeof TABS)[number]

const route = useRoute()
const router = useRouter()
const initial = (TABS as readonly string[]).includes(String(route.query.tab)) ? (route.query.tab as Tab) : 'feishu'
const tab = ref<Tab>(initial)
watch(tab, (t) => void router.replace({ query: { ...route.query, tab: t } }))
</script>

<template>
  <div>
    <PageHeader title="订单来源" description="订单来自飞书多维表格或手动上传的表格；同一平台订单号在工作区内只对应一笔任务。" />
    <el-tabs v-model="tab">
      <el-tab-pane label="飞书连接" name="feishu" lazy><FeishuPanel /></el-tab-pane>
      <el-tab-pane label="文件导入" name="file" lazy><ImportPanel /></el-tab-pane>
      <el-tab-pane label="写回队列" name="writebacks" lazy><WritebackPanel /></el-tab-pane>
    </el-tabs>
  </div>
</template>
