<script setup lang="ts">
/** 订单来源：飞书连接、文件导入、写回队列三个分区（el-tabs）。当前分区记在地址栏 ?tab=。 */
import { computed, ref, watch } from 'vue'
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

/** 页面标题随分区变化（顶栏已显示「订单来源」） */
const HEAD: Record<Tab, { title: string; description: string }> = {
  feishu: { title: '飞书多维表格连接', description: '按设定的间隔读取订单表，结果写回结果表；同一平台订单号在工作区内只对应一笔任务。' },
  file: { title: '上传订单文件', description: '上传 XLSX / CSV，核对字段对应与预览后提交；同一平台订单号在工作区内只对应一笔任务。' },
  writebacks: { title: '飞书写回队列', description: '真实采购结果按写回对应排队写入结果表；模拟结果永不写回。' },
}
const head = computed(() => HEAD[tab.value])
</script>

<template>
  <div>
    <PageHeader :title="head.title" :description="head.description" />
    <el-tabs v-model="tab">
      <el-tab-pane label="飞书连接" name="feishu" lazy><FeishuPanel /></el-tab-pane>
      <el-tab-pane label="文件导入" name="file" lazy><ImportPanel /></el-tab-pane>
      <el-tab-pane label="写回队列" name="writebacks" lazy><WritebackPanel /></el-tab-pane>
    </el-tabs>
  </div>
</template>
