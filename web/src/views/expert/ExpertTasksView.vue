<template>
  <el-card shadow="never">
    <div class="toolbar">
      <span class="page-title">我的任务</span>
      <el-radio-group v-model="statusTab" size="small">
        <el-radio-button value="ALL">全部</el-radio-button>
        <el-radio-button value="PENDING_DECLARATION">待申报</el-radio-button>
        <el-radio-button value="IN_PROGRESS">待评审</el-radio-button>
        <el-radio-button value="CONFLICT_DECLARED">已申报回避</el-radio-button>
      </el-radio-group>
    </div>

    <div v-loading="loading">
      <template v-if="assignments.length">
        <el-card v-for="a in visible" :key="a.assignment_id" shadow="never" class="task-card">
          <!-- 任务头 -->
          <div class="task-head">
            <div class="task-title">
              <span class="lot-name">{{ a.lot_name }}</span>
              <span class="project-name">{{ a.project_name }}</span>
              <el-tag size="small" :type="statusType[a.status] || 'info'">{{ statusLabel[a.status] || a.status }}</el-tag>
            </div>
            <div class="dim-chips">
              <el-tag v-for="d in a.dimensions" :key="d.dimension_id" size="small" effect="plain">
                {{ d.name }}（满分 {{ d.max_score }}）
              </el-tag>
            </div>
            <el-button
              v-if="a.status === 'PENDING_DECLARATION'"
              type="primary"
              size="small"
              @click="$router.push({ path: '/expert/declarations', query: { assignment_id: a.assignment_id } })"
            >去申报</el-button>
          </div>

          <!-- 标书 × 维度 评审矩阵 -->
          <el-table :data="a.bids" border size="small">
            <el-table-column label="供应商" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">{{ row.supplier_name }}</template>
            </el-table-column>
            <el-table-column
              v-for="d in a.dimensions"
              :key="d.dimension_id"
              :label="d.name"
              min-width="130"
              align="center"
            >
              <template #default="{ row }">
                <div v-if="cellOf(row, d.dimension_id)">
                  <div v-if="isDone(cellOf(row, d.dimension_id).review_status)">
                    <span class="cell-score">{{ cellOf(row, d.dimension_id).score ?? '-' }}</span>
                    <el-tag size="small" type="success" effect="plain">已提交</el-tag>
                  </div>
                  <template v-else>
                    <el-button
                      link
                      type="primary"
                      size="small"
                      @click="goReview(row, d, a)"
                    >{{ cellOf(row, d.dimension_id).review_status === 'DRAFT' ? '继续评审' : '去评审' }}</el-button>
                  </template>
                </div>
                <el-button v-else link type="primary" size="small" @click="goReview(row, d, a)">去评审</el-button>
              </template>
            </el-table-column>
            <el-table-column label="进度" width="90" align="center">
              <template #default="{ row }">
                <el-progress
                  :percentage="rowDonePct(row, a)"
                  :stroke-width="8"
                  :show-text="false"
                />
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </template>
      <el-empty v-else description="暂无评审任务">
        <p class="empty-guide">PM 完成专家匹配并指派后，评审任务将出现在这里。<br>新任务需先完成回避申报，再按维度逐项评分。</p>
      </el-empty>
    </div>
  </el-card>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { myAssignments } from '../../api/declarations'

const router = useRouter()

const statusLabel = {
  PENDING_DECLARATION: '待申报',
  IN_PROGRESS: '待评审',
  CONFLICT_DECLARED: '已申报回避',
}
const statusType = {
  PENDING_DECLARATION: 'warning',
  IN_PROGRESS: 'success',
  CONFLICT_DECLARED: 'danger',
}

const assignments = ref([])
const loading = ref(false)
const statusTab = ref('ALL')

const visible = computed(() => {
  if (statusTab.value === 'ALL') return assignments.value
  return assignments.value.filter((a) => a.status === statusTab.value)
})

function cellOf(row, dimensionId) {
  return row.dimensions.find((d) => d.dimension_id === dimensionId)
}

function isDone(status) {
  return status === 'CONFIRMED' || status === 'MANUAL_ADJUSTED'
}

function rowDonePct(row, a) {
  const total = a.dimensions.length
  if (!total) return 0
  const done = row.dimensions.filter((d) => isDone(d.review_status)).length
  return Math.round((done / total) * 100)
}

function goReview(row, dim, a) {
  const d = a.dimensions.find((x) => x.dimension_id === dim.dimension_id)
  const cell = cellOf(row, dim.dimension_id)
  router.push({
    path: '/expert/review',
    query: {
      bid_id: row.bid_id,
      supplier_name: row.supplier_name,
      dimension_id: dim.dimension_id,
      dimension_name: d.name,
      max_score: d.max_score,
      lot_name: a.lot_name,
      review_id: cell?.review_id || '',
    },
  })
}

async function load() {
  loading.value = true
  try {
    const data = await myAssignments()
    assignments.value = data.assignments || []
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
}
.page-title {
  font-weight: 600;
}
.task-card {
  margin-bottom: 14px;
  border: 1px solid var(--el-border-color-lighter);
}
.task-head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.task-title {
  display: flex;
  align-items: center;
  gap: 8px;
}
.lot-name {
  font-weight: 600;
}
.project-name {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.dim-chips {
  flex: 1;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.cell-score {
  font-weight: 600;
  margin-right: 6px;
}
.empty-guide {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.7;
  margin: 0;
}
</style>
