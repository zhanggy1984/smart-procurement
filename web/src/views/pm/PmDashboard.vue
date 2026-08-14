<template>
  <div>
    <PageHeader title="项目经理工作台" :desc="`欢迎，${auth.displayName}。管理项目、标段、评审进度与围串标待办。`" />

    <el-row :gutter="16" class="quick-grid">
      <el-col v-for="item in quickLinks" :key="item.path" :span="6" :xs="12">
        <el-card shadow="hover" class="quick-card" @click="$router.push(item.path)">
          <el-icon :size="28" color="#1f3864"><component :is="item.icon" /></el-icon>
          <div class="quick-title">{{ item.title }}</div>
          <div class="quick-desc">{{ item.desc }}</div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import PageHeader from '../../components/PageHeader.vue'
import { useAuthStore } from '../../stores/auth'

const auth = useAuthStore()

const quickLinks = [
  { path: '/pm/projects', title: '项目管理', desc: '创建项目 · 标段 · 维度配置', icon: 'Folder' },
  { path: '/pm/tasks', title: '围串标待办', desc: 'MEDIUM+ 初筛待确认', icon: 'Warning' },
  { path: '/pm/summary', title: '评标汇总', desc: '评分汇总 · 报告 PDF · 定标', icon: 'Document' },
]
</script>

<style scoped>
.quick-grid {
  margin-top: 16px;
}
.quick-card {
  cursor: pointer;
  text-align: center;
  padding: 8px 0;
}
.quick-title {
  margin-top: 10px;
  font-weight: 600;
}
.quick-desc {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
