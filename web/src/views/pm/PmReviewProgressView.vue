<template>
  <el-card shadow="never">
    <div class="toolbar">
      <span class="page-title">评审进度</span>
      <div class="toolbar-right">
        <el-button
          v-if="data?.lot?.status === 'UNDER_REVIEW'"
          type="primary"
          plain
          @click="openMatch"
        >专家匹配</el-button>
        <el-select
          v-model="lotId"
          placeholder="选择标段"
          style="width: 300px"
          filterable
          @change="loadProgress"
        >
          <el-option
            v-for="l in lotOptions"
            :key="l.lot_id"
            :label="`${l.lot_code} · ${l.name}`"
            :value="l.lot_id"
          />
        </el-select>
      </div>
    </div>

    <template v-if="data">
      <!-- 标段信息 + 进度条 -->
      <div class="head-row">
        <div class="lot-info">
          <span class="lot-code">{{ data.lot.lot_code }}</span>
          <span class="lot-name">{{ data.lot.name }}</span>
          <el-tag size="small" :type="statusType[data.lot.status] || 'info'">{{ statusLabel[data.lot.status] || data.lot.status }}</el-tag>
        </div>
        <div class="progress-box">
          <span class="progress-text">{{ data.progress.done }} / {{ data.progress.total }} 已评审</span>
          <el-progress :percentage="data.progress.percent" :stroke-width="12" style="width: 260px" />
        </div>
      </div>

      <!-- 标书 × 维度 进度矩阵 -->
      <el-table v-loading="loading" :data="data.bids" border stripe>
        <el-table-column label="供应商" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.supplier_name }}</template>
        </el-table-column>
        <el-table-column
          v-for="d in dims"
          :key="d.dimension_id"
          :label="`${d.name}（满分 ${d.max_score}）`"
          min-width="150"
          align="center"
        >
          <template #default="{ row }">
            <template v-if="cellOf(row, d.dimension_id)">
              <div class="cell-score">{{ cellOf(row, d.dimension_id).score != null ? cellOf(row, d.dimension_id).score : '-' }}</div>
              <el-tag size="small" :type="reviewTag[cellOf(row, d.dimension_id).review_status] || 'info'">
                {{ reviewLabel[cellOf(row, d.dimension_id).review_status] || '未分配' }}
              </el-tag>
              <div v-if="cellOf(row, d.dimension_id).expert_name" class="cell-expert">
                {{ cellOf(row, d.dimension_id).expert_name }}
              </div>
            </template>
            <el-tag v-else size="small" type="info">未分配</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="statusType[row.status] || 'info'">{{ statusLabel[row.status] || row.status }}</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </template>

    <el-empty v-else description="请选择标段查看评审进度" />

    <!-- 专家匹配 -->
    <el-dialog v-model="matchVisible" title="专家匹配" width="620px">
      <el-alert
        type="info"
        :closable="false"
        title="选择本标段专业标签（受控词表内），系统按专业/经验/评分质量/地区加权匹配，并自动检测回避冲突后指派。"
        style="margin-bottom: 12px"
      />
      <el-checkbox-group v-model="matchTags" class="tag-group">
        <el-checkbox v-for="t in EXPERT_TAGS" :key="t" :value="t" class="tag-check">{{ t }}</el-checkbox>
      </el-checkbox-group>

      <template v-if="matchResult">
        <el-divider />
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="匹配专家">{{ matchResult.assigned.length }} 位</el-descriptions-item>
          <el-descriptions-item label="冲突排除">
            {{ matchResult.excluded_conflict?.length ? matchResult.excluded_conflict.join('、') : '无' }}
          </el-descriptions-item>
        </el-descriptions>
        <el-alert
          v-if="matchResult.insufficient"
          type="warning"
          :closable="false"
          title="可用专家不足，部分维度可能覆盖不到，建议补充专家后再匹配"
          style="margin-top: 10px"
        />
        <el-table :data="matchResult.assigned" border size="small" style="margin-top: 12px">
          <el-table-column prop="expert_id" label="专家" width="110" />
          <el-table-column prop="name" label="姓名" min-width="110" />
          <el-table-column prop="score" label="匹配分" width="90" align="center" />
          <el-table-column label="负责维度" min-width="200">
            <template #default="{ row }">{{ dimNamesOf(row.dimension_ids) }}</template>
          </el-table-column>
        </el-table>
      </template>

      <template #footer>
        <el-button @click="matchVisible = false">关闭</el-button>
        <el-button type="primary" :loading="matchLoading" :disabled="!matchTags.length" @click="doMatch">
          执行匹配
        </el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<script setup>
import { computed, ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { listLots, getLotReviews, matchExperts } from '../../api/lots'

// 专家专业标签受控词表（与 app/core/constants.py EXPERT_TAGS 一致）
const EXPERT_TAGS = [
  '教育信息化', '软件开发', '系统集成', '网络安全', '人工智能', '大数据', '云计算',
  '物联网', '电子政务', '智慧城市', '安防监控', '通信工程', '医疗信息化', '金融科技',
  '能源信息化', '交通信息化',
]

const statusLabel = {
  BIDDING: '投标中',
  PRE_SCREEN: '初筛待办',
  UNDER_REVIEW: '评审中',
  EVALUATED: '已评审',
  ABANDONED: '已废弃',
  DISQUALIFIED: '已废标',
}
const statusType = {
  UNDER_REVIEW: 'success',
  EVALUATED: 'success',
  BIDDING: 'primary',
  PRE_SCREEN: 'warning',
  ABANDONED: 'info',
  DISQUALIFIED: 'danger',
}
const reviewLabel = {
  CONFIRMED: '已提交',
  MANUAL_ADJUSTED: '人工调整',
  DRAFT: '进行中',
}
const reviewTag = {
  CONFIRMED: 'success',
  MANUAL_ADJUSTED: 'warning',
  DRAFT: 'warning',
}

const lotOptions = ref([])
const lotId = ref('')
const data = ref(null)
const loading = ref(false)

const matchVisible = ref(false)
const matchTags = ref([])
const matchLoading = ref(false)
const matchResult = ref(null)

const dims = computed(() => data.value?.bids?.[0]?.cells?.map((c) => ({
  dimension_id: c.dimension_id,
  name: c.dimension_name,
  max_score: c.max_score,
})) || [])

function dimNamesOf(ids) {
  if (!ids || !ids.length) return '-'
  return ids.map((id) => dims.value.find((d) => d.dimension_id === id)?.name || id).join('、')
}

function openMatch() {
  matchVisible.value = true
  matchResult.value = null
  matchTags.value = []
}

async function doMatch() {
  if (!lotId.value || !matchTags.value.length) return
  matchLoading.value = true
  try {
    matchResult.value = await matchExperts(lotId.value, matchTags.value)
    ElMessage.success(
      `匹配完成：${matchResult.value.assigned.length} 位专家，${matchResult.value.excluded_conflict?.length || 0} 位冲突排除`,
    )
    loadProgress()
  } catch {
    // 错误 toast 由拦截器处理
  } finally {
    matchLoading.value = false
  }
}

function cellOf(row, dimensionId) {
  return row.cells.find((c) => c.dimension_id === dimensionId)
}

async function loadLots() {
  const res = await listLots({ page: 1, page_size: 100 })
  lotOptions.value = res.items || []
  const ready = lotOptions.value.find((l) => l.status === 'UNDER_REVIEW' || l.status === 'EVALUATED')
  if (ready) {
    lotId.value = ready.lot_id
    loadProgress()
  }
}

async function loadProgress() {
  if (!lotId.value) return
  loading.value = true
  try {
    data.value = await getLotReviews(lotId.value)
  } finally {
    loading.value = false
  }
}

onMounted(loadLots)
</script>

<style scoped>
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
}
.toolbar-right {
  display: flex;
  align-items: center;
  gap: 10px;
}
.page-title {
  font-weight: 600;
}
.tag-group {
  width: 100%;
}
.tag-check {
  margin-right: 12px;
}
.head-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
  padding: 12px 14px;
  background: var(--el-fill-color-lighter);
  border-radius: 6px;
}
.lot-info {
  display: flex;
  align-items: center;
  gap: 10px;
}
.lot-code {
  font-weight: 600;
}
.lot-name {
  color: var(--el-text-color-regular);
}
.progress-box {
  display: flex;
  align-items: center;
  gap: 12px;
}
.progress-text {
  font-weight: 600;
}
.cell-score {
  font-weight: 600;
  font-size: 14px;
}
.cell-expert {
  color: var(--el-text-color-placeholder);
  font-size: 12px;
  margin-top: 2px;
}
</style>
