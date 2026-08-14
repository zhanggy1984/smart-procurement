<template>
  <div>
    <!-- 批量导入 -->
    <ImportView
      title="供应商批量导入"
      :action="importSuppliers"
      accept=".xlsx,.xls"
      template-type="supplier"
      tips="Excel 列头：企业名称/统一社会信用代码/法定代表人/所属行业/企业规模。可先点右上角「下载上传模板」参考格式。导入自动创建供应商登录账号。"
      :success-message="fmtResult"
    />

    <!-- P7.4：供应商列表 + 拉黑/解除管理 -->
    <el-card shadow="never" class="list-card">
      <div class="toolbar">
        <span class="card-title">供应商管理（{{ total }}）</span>
        <el-input
          v-model="keyword"
          placeholder="搜索企业名称 / 信用代码"
          clearable
          :prefix-icon="Search"
          class="search-input"
          @keyup.enter="load(1)"
          @clear="load(1)"
        />
      </div>
      <el-table v-loading="loading" :data="rows" border stripe>
        <el-table-column prop="name" label="企业名称" min-width="170" show-overflow-tooltip />
        <el-table-column prop="uniform_credit_code" label="统一信用代码" width="180" />
        <el-table-column prop="legal_person" label="法人" width="100" />
        <el-table-column prop="industry" label="行业" width="110" />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="row.blacklisted ? 'danger' : 'success'">
              {{ row.blacklisted ? '已拉黑' : row.status === 'ACTIVE' ? '正常' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button
              link
              :type="row.blacklisted ? 'success' : 'danger'"
              @click="toggleBlacklist(row)"
            >{{ row.blacklisted ? '解除拉黑' : '拉黑' }}</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无供应商" />
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
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { Search } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import ImportView from '../../components/ImportView.vue'
import { importSuppliers, listSuppliers, updateSupplierStatus } from '../../api/suppliers'

const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const keyword = ref('')
const loading = ref(false)

const fmtResult = (d) => `导入成功 ${d.imported} 条，跳过（统一信用代码重复）${d.skipped} 条`

async function load(p = page.value) {
  loading.value = true
  try {
    const data = await listSuppliers({ page: p, page_size: pageSize.value, keyword: keyword.value || undefined })
    rows.value = data.items || []
    total.value = data.total || 0
  } finally {
    loading.value = false
  }
}

// P7.4：拉黑/解除（级联评审 SUSPENDED / 还原）
async function toggleBlacklist(row) {
  const action = row.blacklisted ? '解除拉黑' : '拉黑'
  try {
    await ElMessageBox.confirm(
      row.blacklisted
        ? `确认解除供应商「${row.name}」的拉黑？被挂起的评审将按原状态还原。`
        : `确认拉黑供应商「${row.name}」？非已定标项目的关联评审将挂起，未封存标书将废标。`,
      action, { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await updateSupplierStatus(row.supplier_id, { blacklisted: !row.blacklisted })
    ElMessage.success(`${action}成功`)
    load()
  } catch {
    // 错误 toast 由拦截器处理
  }
}

onMounted(() => load())
</script>

<style scoped>
.list-card {
  margin-top: 16px;
}
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
}
.card-title {
  font-weight: 600;
}
.search-input {
  width: 280px;
}
.pager {
  margin-top: 14px;
  justify-content: flex-end;
}
</style>
