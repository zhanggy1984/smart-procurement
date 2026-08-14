<template>
  <div>
    <PageHeader title="评审专家工作台" :desc="`欢迎，${auth.displayName}。处理回避申报与 AI 辅助评审任务。`" />

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
  { path: '/expert/tasks', title: '我的任务', desc: '待评审 · 已评审', icon: 'Tickets' },
  { path: '/expert/declarations', title: '回避申报', desc: '逐供应商确认/申报冲突', icon: 'CircleCheck' },
  { path: '/expert/history', title: '评审历史', desc: '个人评分统计', icon: 'DataLine' },
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
