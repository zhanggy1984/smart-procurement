<template>
  <el-card shadow="never">
    <div class="toolbar">
      <el-input
        v-model="keyword"
        placeholder="搜索用户名 / 姓名"
        clearable
        :prefix-icon="Search"
        class="search-input"
        @keyup.enter="load(1)"
        @clear="load(1)"
      />
      <el-button type="primary" :icon="Plus" @click="openCreate">新建用户</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" border stripe>
      <el-table-column prop="username" label="用户名" min-width="120" />
      <el-table-column prop="display_name" label="姓名" min-width="120" />
      <el-table-column prop="role" label="角色" min-width="130">
        <template #default="{ row }">
          <el-tag size="small" effect="plain">{{ roleLabel[row.role] || row.role }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="email" label="邮箱" min-width="160" show-overflow-tooltip />
      <el-table-column prop="phone" label="手机" min-width="120" />
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.is_active ? 'success' : 'info'">
            {{ row.is_active ? '启用' : '禁用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="创建时间" width="170">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="180" fixed="right">
        <template #default="{ row }">
          <el-switch
            :model-value="row.is_active"
            @change="(v) => toggleActive(row, v)"
            style="margin-right: 10px"
          />
          <el-select
            :model-value="row.role"
            size="small"
            style="width: 110px"
            @change="(v) => changeRole(row, v)"
          >
            <el-option v-for="(label, val) in roleLabel" :key="val" :label="label" :value="val" />
          </el-select>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="暂无用户" />
      </template>
    </el-table>

    <el-pagination
      v-model:current-page="page"
      :page-size="pageSize"
      :total="total"
      layout="total, prev, pager, next"
      class="pager"
      @current-change="load"
    />

    <!-- 新建用户 -->
    <el-dialog v-model="createVisible" title="新建用户" width="480px">
      <el-form ref="createForm" :model="form" :rules="formRules" label-width="80px">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" />
        </el-form-item>
        <el-form-item label="姓名" prop="display_name">
          <el-input v-model="form.display_name" />
        </el-form-item>
        <el-form-item label="角色" prop="role">
          <el-select v-model="form.role" style="width: 100%">
            <el-option v-for="(label, val) in roleLabel" :key="val" :label="label" :value="val" />
          </el-select>
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input v-model="form.password" type="password" show-password placeholder="≥8位，含大小写字母和数字" />
        </el-form-item>
        <el-form-item label="邮箱" prop="email">
          <el-input v-model="form.email" />
        </el-form-item>
        <el-form-item label="手机" prop="phone">
          <el-input v-model="form.phone" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { listUsers, createUser, updateUserStatus } from '../../api/users'

const roleLabel = {
  ADMIN: '管理员',
  PROJECT_MANAGER: '项目经理',
  REVIEW_EXPERT: '评审专家',
  SUPPLIER: '供应商',
}

const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const keyword = ref('')
const loading = ref(false)
const createVisible = ref(false)
const saving = ref(false)

const form = ref({ username: '', display_name: '', role: 'REVIEW_EXPERT', password: '', email: '', phone: '' })
const formRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  display_name: [{ required: true, message: '请输入姓名', trigger: 'blur' }],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 8, message: '至少 8 位', trigger: 'blur' },
  ],
}

async function load(p = page.value) {
  loading.value = true
  try {
    const data = await listUsers({ page: p, page_size: pageSize.value, keyword: keyword.value || undefined })
    rows.value = data.items || []
    total.value = data.total || 0
  } finally {
    loading.value = false
  }
}

function openCreate() {
  Object.assign(form.value, { username: '', display_name: '', role: 'REVIEW_EXPERT', password: '', email: '', phone: '' })
  createVisible.value = true
}

async function submitCreate() {
  saving.value = true
  try {
    await createUser(form.value)
    ElMessage.success('创建成功')
    createVisible.value = false
    load()
  } catch {
    // 错误 toast 由拦截器处理
  } finally {
    saving.value = false
  }
}

async function toggleActive(row, v) {
  try {
    await updateUserStatus(row.user_id, { is_active: v })
    row.is_active = v
    ElMessage.success(v ? '已启用' : '已禁用')
  } catch {
    load()
  }
}

async function changeRole(row, v) {
  try {
    await updateUserStatus(row.user_id, { role: v })
    row.role = v
    ElMessage.success('角色已更新')
  } catch {
    load()
  }
}

function fmtTime(t) {
  return t ? t.replace('T', ' ').slice(0, 19) : '-'
}

onMounted(() => load())
</script>

<style scoped>
.toolbar {
  display: flex;
  justify-content: space-between;
  margin-bottom: 14px;
}
.search-input {
  width: 280px;
}
.pager {
  margin-top: 14px;
  justify-content: flex-end;
}
</style>
