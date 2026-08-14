<template>
  <div>
    <!-- 批量导入 -->
    <ImportView
      title="专家批量导入"
      :action="importExperts"
      accept=".xlsx,.xls"
      template-type="expert"
      tips="Excel 列头：姓名/单位/地区/从业年限/专业标签/身份证号/邮箱/电话。可先点右上角「下载上传模板」参考格式。导入自动创建专家登录账号。"
      :success-message="fmtResult"
    />

    <!-- 专家列表（P6.2 补：管理页数据源） -->
    <el-card shadow="never" class="list-card">
      <div class="toolbar">
        <span class="card-title">专家管理（{{ total }}）</span>
        <el-input
          v-model="keyword"
          placeholder="搜索姓名 / 单位 / 地区"
          clearable
          :prefix-icon="Search"
          class="search-input"
          @keyup.enter="load(1)"
          @clear="load(1)"
        />
      </div>
      <el-table v-loading="loading" :data="rows" border stripe>
        <el-table-column prop="name" label="姓名" width="100" />
        <el-table-column prop="organization" label="单位" min-width="170" show-overflow-tooltip />
        <el-table-column prop="region" label="地区" width="90" />
        <el-table-column prop="experience" label="从业年限" width="90" />
        <el-table-column label="专业标签" min-width="160">
          <template #default="{ row }">
            <el-tag v-for="t in row.tags" :key="t" size="small" class="tag">{{ t }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'ACTIVE' ? 'success' : 'info'">
              {{ STATUS_LABEL[row.status] || row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无专家" />
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
import ImportView from '../../components/ImportView.vue'
import { importExperts, listExperts } from '../../api/experts'

const STATUS_LABEL = { ACTIVE: '正常', INACTIVE: '停用', BLACKLISTED: '拉黑' }

const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const keyword = ref('')
const loading = ref(false)

async function load(p = page.value) {
  loading.value = true
  try {
    const data = await listExperts({ page: p, page_size: pageSize.value, keyword: keyword.value || undefined })
    rows.value = data.items || []
    total.value = data.total || 0
  } finally {
    loading.value = false
  }
}

const fmtResult = (d) => {
  const lines = [`导入成功 ${d.imported} 条，跳过（信用代码重复）${d.skipped} 条`]
  if (d.errors?.length) lines.push('行级错误：\n' + d.errors.map((e) => `  - ${e}`).join('\n'))
  return lines.join('\n')
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
  width: 260px;
}
.pager {
  margin-top: 14px;
  justify-content: flex-end;
}
.tag {
  margin-right: 4px;
}
</style>
