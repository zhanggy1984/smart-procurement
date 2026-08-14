<template>
  <div>
    <PageHeader title="管理员工作台" :desc="`欢迎，${auth.displayName}。管理用户、专家、供应商与工商冲突数据。`" />

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
  { path: '/admin/experts', title: '专家管理', desc: 'Excel 批量导入 · 状态管理', icon: 'User' },
  { path: '/admin/suppliers', title: '供应商管理', desc: 'Excel 导入 · 黑名单管理', icon: 'OfficeBuilding' },
  { path: '/admin/conflicts', title: '工商信息', desc: '企查查 CSV 冲突导入', icon: 'Connection' },
  { path: '/admin/users', title: '用户管理', desc: '账号创建与角色分配', icon: 'Avatar' },
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
