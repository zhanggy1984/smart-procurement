<template>
  <div>
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>我的投标（{{ items.length }}）</span>
          <div class="legend">
            <el-tag v-for="(t, s) in STATUS_TAG" :key="s" :type="t" size="small" effect="plain" class="legend-tag">
              {{ STATUS_LABEL[s] }}
            </el-tag>
          </div>
        </div>
      </template>

      <el-table :data="sorted" v-loading="loading" @row-click="goDetail" :row-class-name="rowClass">
        <el-table-column label="标段" min-width="170">
          <template #default="{ row }">
            <div class="lot-cell">
              <div class="lot-name">{{ row.lot_name }}</div>
              <div class="lot-sub">{{ row.project_name }} · {{ row.lot_code }}</div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="STATUS_TAG[row.result_status]" size="small">{{ STATUS_LABEL[row.result_status] }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="报价" width="120">
          <template #default="{ row }">{{ fmtWan(row.bid_amount) }}</template>
        </el-table-column>
        <el-table-column label="排名" width="80">
          <template #default="{ row }">
            <span v-if="row.rank">{{ row.rank }}</span>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="综合得分" width="100">
          <template #default="{ row }">
            <span v-if="row.weighted_total != null">{{ row.weighted_total }}</span>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="中标方" min-width="140">
          <template #default="{ row }">
            <span v-if="row.winner_supplier_name">{{ row.winner_supplier_name }}</span>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click.stop="goDetail(row)">结果详情</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && !items.length" description="暂无投标记录">
        <p class="empty-guide">在「招标市场」选择标段上传标书参与投标后，投标结果将在这里展示。</p>
        <el-button type="primary" size="small" @click="$router.push('/supplier/market')">去市场投标</el-button>
      </el-empty>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { getMyResults } from '../../api/suppliers'

const router = useRouter()
const loading = ref(false)
const items = ref([])

const STATUS_LABEL = {
  WINNER: '已中标',
  LOSER: '未中标',
  UNDER_REVIEW: '评审中',
  DISQUALIFIED: '废标',
}
const STATUS_TAG = {
  WINNER: 'success',
  LOSER: 'danger',
  UNDER_REVIEW: 'warning',
  DISQUALIFIED: 'danger',
}

// 终态（已中标/未中标）排前，评审中靠后
const SORT_ORDER = { WINNER: 0, LOSER: 1, DISQUALIFIED: 2, UNDER_REVIEW: 3 }
const sorted = computed(() =>
  [...items.value].sort((a, b) => (SORT_ORDER[a.result_status] ?? 9) - (SORT_ORDER[b.result_status] ?? 9)),
)

function fmtWan(v) {
  if (v == null) return '-'
  return `${(v / 10000).toFixed(1)} 万`
}

function rowClass({ row }) {
  return row.result_status === 'WINNER' ? 'winner-row' : ''
}

function goDetail(row) {
  router.push(`/supplier/results/${row.bid_id}`)
}

async function load() {
  loading.value = true
  try {
    const data = await getMyResults()
    items.value = data.items
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: 600;
}
.legend {
  display: flex;
  gap: 6px;
}
.legend-tag {
  border: none;
}
.lot-cell .lot-name {
  font-weight: 600;
}
.lot-cell .lot-sub {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.muted {
  color: var(--el-text-color-placeholder);
}
:deep(.winner-row) {
  --el-table-tr-bg-color: var(--el-color-success-light-9);
}
.empty-guide {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.7;
  margin: 0 0 10px;
}
</style>
