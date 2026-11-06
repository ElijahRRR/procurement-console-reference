<script setup lang="ts">
/** 根组件：Element Plus 中文语言包 + 路由出口。 */
import zhCn from 'element-plus/es/locale/lang/zh-cn'
</script>

<template>
  <el-config-provider :locale="zhCn">
    <RouterView />
  </el-config-provider>
</template>
