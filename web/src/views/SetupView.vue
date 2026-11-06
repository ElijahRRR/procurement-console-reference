<script setup lang="ts">
/** 首次设置：系统里还没有任何用户时创建所有者与工作区，完成后直接登录。 */
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import type { FormInstance, FormRules } from 'element-plus'
import AuthShell from '@/components/AuthShell.vue'
import * as auth from '@/api/auth'
import { setMe } from '@/stores/session'
import { failureText } from '@/utils/format'

const router = useRouter()
const formRef = ref<FormInstance>()
const form = reactive({ workspace_name: '', username: '', password: '', confirm: '' })
const busy = ref(false)
const error = ref('')

const rules: FormRules = {
  workspace_name: [{ required: true, message: '请填写工作区名称', trigger: 'blur' }],
  username: [
    { required: true, message: '请填写用户名', trigger: 'blur' },
    { pattern: /^[A-Za-z0-9_.-]{3,32}$/, message: '用户名为 3–32 位字母、数字、下划线、点或短横', trigger: 'blur' },
  ],
  password: [{ required: true, min: 8, message: '密码至少 8 位', trigger: 'blur' }],
  confirm: [
    {
      validator: (_r: unknown, v: string, cb: (e?: Error) => void) =>
        v === form.password ? cb() : cb(new Error('两次输入的密码不一致')),
      trigger: 'blur',
    },
  ],
}

async function submit() {
  error.value = ''
  if (!(await formRef.value?.validate().catch(() => false))) return
  busy.value = true
  try {
    const me = await auth.setup({
      username: form.username.trim(),
      password: form.password,
      workspace_name: form.workspace_name.trim(),
    })
    setMe(me)
    await router.replace('/')
  } catch (e) {
    error.value = failureText('创建所有者', e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <AuthShell title="首次设置" description="创建所有者账号与工作区。之后的订单来源、买家账号与设备都属于这个工作区。">
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="mb-4" />
    <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="submit">
      <el-form-item label="工作区名称" prop="workspace_name">
        <el-input v-model="form.workspace_name" placeholder="如 我的采购工作区" />
      </el-form-item>
      <el-form-item label="用户名" prop="username">
        <el-input v-model="form.username" autocomplete="username" placeholder="3–32 位字母或数字" />
      </el-form-item>
      <el-form-item label="密码" prop="password">
        <el-input v-model="form.password" type="password" show-password autocomplete="new-password" placeholder="至少 8 位" />
      </el-form-item>
      <el-form-item label="确认密码" prop="confirm">
        <el-input v-model="form.confirm" type="password" show-password autocomplete="new-password" placeholder="再输入一次" />
      </el-form-item>
      <el-button type="primary" native-type="submit" class="w-full" :loading="busy">创建并登录</el-button>
    </el-form>
  </AuthShell>
</template>
