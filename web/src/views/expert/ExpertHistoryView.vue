<template>
  <el-card shadow="never">
    <div class="toolbar">
      <span class="page-title">评审历史</span>
    </div>

    <!-- 统计卡片 -->
    <el-row :gutter="14" class="stat-row">
      <el-col :span="6">
        <div class="stat-card">
          <div class="stat-num">{{ stats.total }}</div>
          <div class="stat-label">累计评审</div>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="stat-card">
          <div class="stat-num">{{ stats.avgScore }}</div>
          <div class="stat-label">平均得分</div>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="stat-card">
          <div class="stat-num">{{ stats.avgPct }}%</div>
          <div class="stat-label">平均得分率</div>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="stat-card">
          <div class="stat-num">{{ stats.manual }} / {{ stats.total }}</div>
          <div class="stat-label">人工调整 / 累计</div>
        </div>
      </el-col>
    </el-row>

    <el-table v-loading="loading" :data="rows" border stripe>
      <el-table-column prop="lot_name" label="标段" min-width="170" show-overflow-tooltip />
      <el-table-column prop="supplier_name" label="供应商" min-width="170" show-overflow-tooltip />
      <el-table-column prop="dimension_name" label="维度" width="120" />
      <el-table-column label="得分" width="110" align="center">
        <template #default="{ row }">{{ row.score }}<span class="max-hint"> / {{ row.max_score }}</span></template>
      </el-table-column>
      <el-table-column label="状态" width="110" align="center">
        <template #default="{ row }">
          <el-tag size="small" :type="row.status === 'MANUAL_ADJUSTED' ? 'warning' : 'success'">
            {{ row.status === 'MANUAL_ADJUSTED' ? '人工调整' : '采纳 AI' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="提交时间" width="180">
        <template #default="{ row }">{{ row.submitted_at }}</template>
      </el-table-column>
      <template #empty>
        <el-empty description="暂无评审记录">
          <p class="empty-guide">完成评审提交后，评分与人工调整记录将在这里累计统计。</p>
        </el-empty>
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
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { myReviews } from '../../api/reviews'

const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)

const stats = computed(() => {
  const n = rows.value.length || 0
  const scored = rows.value.filter((r) => r.score != null)
  const sum = scored.reduce((acc, r) => acc + Number(r.score), 0)
  const pctSum = scored.reduce((acc, r) => acc + (Number(r.score) / (r.max_score || 1)) * 100, 0)
  const manual = rows.value.filter((r) => r.status === 'MANUAL_ADJUSTED').length
  return {
    total: total.value,
    avgScore: scored.length ? (sum / scored.length).toFixed(1) : '-',
    avgPct: scored.length ? Math.round(pctSum / scored.length) : '-',
    manual,
  }
})

async function load(p = page.value) {
  loading.value = true
  try {
    const data = await myReviews({ page: p, page_size: pageSize.value })
    rows.value = data.items || []
    total.value = data.total || 0
  } finally {
    loading.value = false
  }
}

onMounted(() => load())
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
.stat-row {
  margin-bottom: 14px;
}
.stat-card {
  background: var(--el-fill-color-lighter);
  border-radius: 6px;
  padding: 14px;
  text-align: center;
}
.stat-num {
  font-size: 22px;
  font-weight: 600;
}
.stat-label {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  margin-top: 4px;
}
.max-hint {
  color: var(--el-text-color-placeholder);
  font-size: 12px;
}
.empty-guide {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.7;
  margin: 0;
}
.pager {
  margin-top: 14px;
  justify-content: flex-end;
}
</style>
