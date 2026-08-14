<template>
  <div class="login-page">
    <el-card class="login-card">
      <div class="login-brand">
        <el-icon :size="40" color="#1f3864"><DataAnalysis /></el-icon>
        <h2>AI 辅助评审系统</h2>
        <p class="subtitle">专家匹配 · AI 辅助评标 · 围串标检测</p>
      </div>

      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @keyup.enter="onSubmit">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="请输入用户名" :prefix-icon="User" clearable />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="请输入密码"
            :prefix-icon="Lock"
            show-password
          />
        </el-form-item>
        <el-button type="primary" class="login-btn" :loading="loading" @click="onSubmit">
          登 录
        </el-button>
      </el-form>

      <el-alert type="info" :closable="false" class="demo-tip">
        <template #title>
          <div class="demo-accounts">
            <div>演示账号（密码均为 <b>Smart@2026</b>）：</div>
            <div>管理员 <code>admin</code> ｜ 项目经理 <code>pm1</code></div>
            <div>评审专家 <code>expert_01</code> ｜ 供应商 <code>supplier_01</code></div>
          </div>
        </template>
      </el-alert>
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const formRef = ref()
const loading = ref(false)
const form = ref({ username: '', password: '' })

const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

async function onSubmit() {
  try {
    await formRef.value.validate()
  } catch {
    return
  }
  loading.value = true
  try {
    const home = await auth.login(form.value.username, form.value.password)
    ElMessage.success('登录成功')
    const redirect = route.query.redirect
    router.push(typeof redirect === 'string' && redirect.startsWith('/') ? redirect : home)
  } catch {
    // 错误 toast 已在 axios 拦截器统一处理
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #16294b 0%, #1f3864 100%);
}
.login-card {
  width: 400px;
  padding: 8px 12px;
  border-radius: 4px;
  border-top: 3px solid #1f3864;
  box-shadow: 0 8px 24px rgba(22, 41, 75, 0.25);
}
.login-brand {
  text-align: center;
  margin-bottom: 20px;
}
.login-brand h2 {
  margin: 8px 0 4px;
  color: var(--el-text-color-primary);
}
.subtitle {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.login-btn {
  width: 100%;
  margin-top: 4px;
}
.demo-tip {
  margin-top: 16px;
}
.demo-accounts {
  font-size: 12px;
  line-height: 1.8;
}
.demo-accounts code {
  background: var(--el-fill-color-dark);
  padding: 0 4px;
  border-radius: 3px;
}
</style>
