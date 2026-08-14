<template>
  <div>
    <!-- 批量导入 -->
    <ImportView
      title="工商信息冲突导入"
      :action="importConflicts"
      accept=".csv"
      template-type="conflict"
      tips="CSV 列头：姓名/企业名称/统一社会信用代码/关系类型/职位/持股比例，UTF-8 编码。可先点右上角「下载上传模板」参考格式。未匹配到系统的企业计入待办（冷数据），确认后可导入。"
      :success-message="fmtResult"
    />

    <!-- 工商信息冷数据列表（P6.2 补：管理页数据源） -->
    <el-card shadow="never" class="list-card">
      <div class="toolbar">
        <span class="card-title">工商信息冷数据（{{ total }}）</span>
        <el-select v-model="statusFilter" placeholder="全部状态" clearable style="width: 140px" @change="load(1)">
          <el-option label="待处理" value="PENDING" />
          <el-option label="已激活" value="ACTIVATED" />
        </el-select>
      </div>
      <el-table v-loading="loading" :data="rows" border stripe>
        <el-table-column prop="person_name" label="姓名" width="100" />
        <el-table-column prop="company_name" label="企业名称" min-width="180" show-overflow-tooltip />
        <el-table-column prop="credit_code" label="统一信用代码" width="170" />
        <el-table-column prop="relation_type" label="关系类型" width="100" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'PENDING' ? 'warning' : 'success'">
              {{ row.status === 'PENDING' ? '待处理' : '已激活' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="导入时间" width="170">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无冷数据" />
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
import ImportView from '../../components/ImportView.vue'
import { importConflicts, listPendingConflicts } from '../../api/conflicts'

const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const statusFilter = ref('')
const loading = ref(false)

async function load(p = page.value) {
  loading.value = true
  try {
    const data = await listPendingConflicts({
      page: p,
      page_size: pageSize.value,
      status: statusFilter.value || undefined,
    })
    rows.value = data.items || []
    total.value = data.total || 0
  } finally {
    loading.value = false
  }
}

const fmtResult = (d) => {
  const lines = [`解析 ${d.total} 条关系：命中 ${d.matched} 条，待确认 ${d.pending} 条`]
  if (d.skipped) lines.push(`跳过 ${d.skipped} 条`)
  return lines.join('\n')
}

const fmtTime = (t) => (t ? String(t).replace('T', ' ').slice(0, 19) : '-')

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
.pager {
  margin-top: 14px;
  justify-content: flex-end;
}
</style>
