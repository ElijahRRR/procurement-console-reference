<script setup lang="ts">
/** 登录页。成功后回到 redirect 指定的站内地址。 */
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { FormInstance, FormRules } from 'element-plus'
import AuthShell from '@/components/AuthShell.vue'
import * as auth from '@/api/auth'
import { safeRedirect } from '@/router/guard'
import { setMe } from '@/stores/session'
import { failureText } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const formRef = ref<FormInstance>()
const form = reactive({ username: '', password: '' })
const busy = ref(false)
const error = ref('')
const rules: FormRules = {
  username: [{ required: true, message: '请填写用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请填写密码', trigger: 'blur' }],
}

async function submit() {
  error.value = ''
  if (!(await formRef.value?.validate().catch(() => false))) return
  busy.value = true
  try {
    const me = await auth.login({ username: form.username.trim(), password: form.password })
    setMe(me)
    await router.replace(safeRedirect(route.query.redirect))
  } catch (e) {
    error.value = failureText('登录', e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <AuthShell title="登录" description="使用所有者账号登录控制台。">
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="mb-4" />
    <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="submit">
      <el-form-item label="用户名" prop="username">
        <el-input v-model="form.username" autocomplete="username" placeholder="用户名" />
      </el-form-item>
      <el-form-item label="密码" prop="password">
        <el-input v-model="form.password" type="password" show-password autocomplete="current-password" placeholder="密码" />
      </el-form-item>
      <el-button type="primary" native-type="submit" class="w-full" :loading="busy">登录</el-button>
    </el-form>
  </AuthShell>
</template>
