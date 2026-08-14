<template>
  <el-card shadow="never">
    <div class="toolbar">
      <span class="page-title">评标汇总</span>
      <el-select
        v-model="lotId"
        placeholder="选择标段"
        style="width: 300px"
        filterable
        @change="loadSummary"
      >
        <el-option
          v-for="l in lotOptions"
          :key="l.lot_id"
          :label="`${l.lot_code} · ${l.name}`"
          :value="l.lot_id"
        />
      </el-select>
    </div>

    <template v-if="summary">
      <!-- 标段信息 + 维度权重 -->
      <el-descriptions :column="4" border size="small" class="info-box">
        <el-descriptions-item label="项目">{{ summary.lot.project_code }} · {{ summary.lot.project_name }}</el-descriptions-item>
        <el-descriptions-item label="标段">{{ summary.lot.lot_code }} · {{ summary.lot.name }}</el-descriptions-item>
        <el-descriptions-item label="预算(万元)">{{ fmtWan(summary.lot.budget) }}</el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag size="small" :type="statusType[summary.lot.status] || 'info'">{{ statusLabel[summary.lot.status] || summary.lot.status }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="评分维度" :span="4">
          <template v-for="(d, i) in summary.dimensions" :key="d.dimension_id">
            <el-tag size="small" effect="plain" style="margin-right: 6px">
              {{ d.name }}（满分 {{ d.max_score }} · 权重 {{ (d.weight * 100).toFixed(1) }}%）
            </el-tag>
          </template>
        </el-descriptions-item>
      </el-descriptions>

      <!-- 评分汇总表 -->
      <el-table v-loading="loading" :data="summary.bids" border stripe class="summary-table">
        <el-table-column label="排名" width="70" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.rank === 1" type="danger" effect="dark" size="small">第 1 名</el-tag>
            <span v-else>第 {{ row.rank }} 名</span>
          </template>
        </el-table-column>
        <el-table-column label="供应商" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.supplier_name }}</template>
        </el-table-column>
        <el-table-column label="报价(万元)" width="120" align="right">
          <template #default="{ row }">{{ fmtWan(row.bid_amount) }}</template>
        </el-table-column>
        <el-table-column
          v-for="d in summary.dimensions"
          :key="d.dimension_id"
          :label="d.name"
          width="110"
          align="center"
        >
          <template #default="{ row }">
            <span v-if="scoreOf(row, d.dimension_id) != null">{{ scoreOf(row, d.dimension_id) }}<span class="dim-max">/{{ d.max_score }}</span></span>
            <el-tag v-else size="small" type="info">未评审</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="综合得分" width="110" align="center" fixed="right">
          <template #default="{ row }">
            <span class="total-score" :class="{ top: row.rank === 1 }">{{ row.weighted_total }}</span>
          </template>
        </el-table-column>
      </el-table>

      <!-- 操作 -->
      <div class="actions">
        <el-button type="primary" plain :loading="downloadLoading" @click="doDownload">下载评审报告</el-button>
        <el-button
          v-if="summary.lot.status === 'UNDER_REVIEW'"
          type="success"
          :loading="completeLoading"
          @click="doComplete"
        >结束评审</el-button>
        <template v-else-if="summary.lot.status === 'EVALUATED'">
          <el-tag type="success" effect="plain">评审已完成</el-tag>
          <el-button type="danger" plain :loading="awardLoading" @click="doAward">推送定标</el-button>
        </template>
      </div>
    </template>

    <el-empty v-else description="请选择标段查看评标汇总" />
  </el-card>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { listLots } from '../../api/lots'
import { getLotSummary, completeReview, downloadReport, submitForAward } from '../../api/closeouts'

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

const lotOptions = ref([])
const lotId = ref('')
const summary = ref(null)
const loading = ref(false)
const downloadLoading = ref(false)
const completeLoading = ref(false)
const awardLoading = ref(false)

function scoreOf(row, dimensionId) {
  const ds = row.dimension_scores.find((d) => d.dimension_id === dimensionId)
  return ds ? ds.score : null
}

function fmtWan(v) {
  if (v == null) return '-'
  return (Number(v) / 10000).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

async function loadLots() {
  const data = await listLots({ page: 1, page_size: 100 })
  lotOptions.value = data.items || []
  // 默认选中第一个非 BIDDING 标段（有评审数据的）
  const ready = lotOptions.value.find((l) => l.status !== 'BIDDING')
  if (ready) {
    lotId.value = ready.lot_id
    loadSummary()
  }
}

async function loadSummary() {
  if (!lotId.value) return
  loading.value = true
  try {
    summary.value = await getLotSummary(lotId.value)
  } finally {
    loading.value = false
  }
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

async function doDownload() {
  downloadLoading.value = true
  try {
    const blob = await downloadReport(lotId.value)
    saveBlob(blob, `lot_${lotId.value}_report.pdf`)
  } catch {
    // 错误 toast 由拦截器处理
  } finally {
    downloadLoading.value = false
  }
}

async function doAward() {
  if (!summary.value?.lot?.project_id) return
  try {
    await ElMessageBox.confirm('推送定标后项目进入已定标状态，评审结果不可再变更。确定定标？', '推送定标', {
      type: 'warning',
    })
  } catch {
    return // 取消
  }
  awardLoading.value = true
  try {
    const res = await submitForAward(summary.value.lot.project_id)
    ElMessage.success(`定标成功（${res.status}）`)
    await loadSummary()
  } finally {
    awardLoading.value = false
  }
}

async function doComplete() {
  try {
    await ElMessageBox.confirm('结束评审后标段进入已评审状态，评审记录将锁定。确定结束？', '结束评审', {
      type: 'warning',
    })
  } catch {
    return // 取消
  }
  completeLoading.value = true
  try {
    const res = await completeReview(lotId.value)
    ElMessage.success(`评审已结束（${res.status}），报告已生成`)
    await loadSummary()
  } finally {
    completeLoading.value = false
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
.page-title {
  font-weight: 600;
}
.info-box {
  margin-bottom: 14px;
}
.summary-table {
  margin-bottom: 14px;
}
.dim-max {
  color: var(--el-text-color-placeholder);
  font-size: 12px;
}
.total-score {
  font-weight: 600;
}
.total-score.top {
  color: var(--el-color-danger);
}
.actions {
  display: flex;
  gap: 12px;
}
</style>
