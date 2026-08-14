<template>
  <div>
    <el-page-header class="page-head" @back="router.push('/supplier')">
      <template #content>
        <span class="page-title">{{ project?.name }}</span>
      </template>
    </el-page-header>

    <el-card shadow="never" v-if="project" class="proj-card">
      <el-descriptions :column="4" border>
        <el-descriptions-item label="项目编号">{{ project.project_code }}</el-descriptions-item>
        <el-descriptions-item label="项目类型">{{ typeLabel(project.type) }}</el-descriptions-item>
        <el-descriptions-item label="地区">{{ project.region || '-' }}</el-descriptions-item>
        <el-descriptions-item label="项目预算">{{ fmtWan(project.budget) }}</el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">标段列表（{{ lots.length }}）</div>
      </template>
      <el-table :data="lots" v-loading="loading">
        <el-table-column prop="lot_code" label="标段编号" width="120" />
        <el-table-column prop="name" label="标段名称" min-width="180" />
        <el-table-column label="预算" width="130">
          <template #default="{ row }">{{ fmtWan(row.budget) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="lotStatusType(row.status)" size="small">{{ lotStatusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="我的状态" width="110">
          <template #default="{ row }">
            <el-tag v-if="myLotSet.has(row.lot_id)" type="success" size="small">已投标</el-tag>
            <el-tag v-else-if="row.status === 'BIDDING'" type="info" size="small" effect="plain">可投标</el-tag>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="router.push(`/supplier/lots/${row.lot_id}`)">
              标段详情
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getProject } from '../../api/projects'
import { getMyBids } from '../../api/suppliers'

const route = useRoute()
const router = useRouter()
const loading = ref(false)
const project = ref(null)
const lots = ref([])
const myLotSet = ref(new Set())

const TYPE_LABEL = { SERVICE: '服务类', ENGINEERING: '工程类' }
const LOT_STATUS_LABEL = { BIDDING: '招标中', UNDER_REVIEW: '评审中', EVALUATED: '已评审', AWARDED: '已定标' }
const LOT_STATUS_TYPE = { BIDDING: 'primary', UNDER_REVIEW: 'warning', EVALUATED: 'success', AWARDED: 'info' }
const typeLabel = (t) => TYPE_LABEL[t] || t || '-'
const lotStatusLabel = (s) => LOT_STATUS_LABEL[s] || s
const lotStatusType = (s) => LOT_STATUS_TYPE[s] || 'info'

function fmtWan(v) {
  if (v == null) return '-'
  return `${(v / 10000).toFixed(1)} 万`
}

async function load() {
  loading.value = true
  try {
    const pid = route.params.projectId
    const data = await getProject(pid)
    project.value = data
    lots.value = data.lots || []
    // 已投 lot 集合（标段状态标记）
    const my = await getMyBids({ page: 1, page_size: 200 })
    myLotSet.value = new Set(my.items.map((b) => b.lot_id))
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.page-head {
  margin-bottom: 16px;
}
.page-title {
  font-weight: 600;
}
.proj-card {
  margin-bottom: 16px;
}
.card-header {
  font-weight: 600;
}
</style>
