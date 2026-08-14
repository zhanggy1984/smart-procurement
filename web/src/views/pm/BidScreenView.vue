<template>
  <el-card shadow="never">
    <div class="toolbar">
      <span class="page-title">围串标待办</span>
      <el-radio-group v-model="statusFilter" size="small" @change="load(1)">
        <el-radio-button value="">全部</el-radio-button>
        <el-radio-button value="PRE_SCREEN">初筛待办</el-radio-button>
        <el-radio-button value="BIDDING">投标中</el-radio-button>
        <el-radio-button value="UNDER_REVIEW">评审中</el-radio-button>
      </el-radio-group>
    </div>

    <el-alert
      type="info"
      :closable="false"
      title="投标期结束后，点击「关闭投标」执行围串标初筛（关系图谱 + 报价集中度 + 标书相似度）。MEDIUM 以上进入待办确认。"
      style="margin-bottom: 14px"
    />

    <el-table v-loading="loading" :data="rows" border stripe>
      <el-table-column prop="lot_code" label="标段编码" width="130" />
      <el-table-column prop="name" label="标段名称" min-width="170" show-overflow-tooltip />
      <el-table-column label="所属项目" min-width="150" show-overflow-tooltip>
        <template #default="{ row }">{{ row.project_code }} · {{ row.project_name }}</template>
      </el-table-column>
      <el-table-column label="预算(万元)" width="110" align="right">
        <template #default="{ row }">{{ fmtWan(row.budget) }}</template>
      </el-table-column>
      <el-table-column prop="bid_count" label="标书数" width="80" align="center">
        <template #default="{ row }">
          <el-badge :value="row.bid_count" :hidden="row.bid_count === 0" />
        </template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag size="small" :type="statusType[row.status]">{{ statusLabel[row.status] || row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="180" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="row.status === 'BIDDING'"
            link
            type="warning"
            @click="confirmClose(row)"
          >关闭投标</el-button>
          <template v-else-if="row.status === 'PRE_SCREEN'">
            <el-button link type="primary" :loading="confirmingLot === row.lot_id" @click="doConfirm(row)">确认放行</el-button>
            <el-button link type="danger" @click="$router.push({ path: `/pm/lots/${row.lot_id}/bids`, query: { name: row.name, screen: '1' } })">废标</el-button>
          </template>
          <el-button
            v-else-if="row.bid_count > 0"
            link
            type="primary"
            @click="$router.push({ path: `/pm/lots/${row.lot_id}/bids`, query: { name: row.name } })"
          >标书</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="暂无标段">
          <p class="empty-guide">初筛标记 MEDIUM+ 风险的标段会进入待办，此处显示待确认的标段。</p>
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

    <!-- 关闭投标确认 -->
    <el-dialog v-model="closeVisible" :title="`关闭投标 · ${closing?.lot_code || ''}`" width="480px">
      <p>将对该标段执行围串标初筛（关系图谱粗检 + 报价集中度 + 标书相似度），确定风险等级。</p>
      <p>标段：{{ closing?.name }}（标书 {{ closing?.bid_count }} 份）</p>
      <template #footer>
        <el-button @click="closeVisible = false">取消</el-button>
        <el-button type="primary" :loading="closingLoading" @click="doClose">确认执行</el-button>
      </template>
    </el-dialog>

    <!-- 初筛结果 -->
    <el-dialog v-model="resultVisible" title="围串标初筛结果" width="560px">
      <div v-if="result">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="风险等级">
            <el-tag size="small" :type="riskType[result.risk]">{{ result.risk }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="综合评分">{{ result.total_score }}</el-descriptions-item>
          <el-descriptions-item label="关系图谱检">{{ result.scores?.graph ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="报价检">{{ result.scores?.price ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="文本检">{{ result.scores?.text ?? '-' }}</el-descriptions-item>
        </el-descriptions>
        <el-alert
          v-if="result.risk === 'LOW'"
          type="success"
          :closable="false"
          title="LOW 风险，自动通过，标段进入评审"
          style="margin-top: 12px"
        />
        <el-alert
          v-else
          type="warning"
          :closable="false"
          title="MEDIUM 以上风险，已加入待办，需人工确认"
          style="margin-top: 12px"
        />
        <div v-if="evidenceText" class="evidence">
          <div class="struct-title">检测证据</div>
          <pre class="evidence-pre">{{ evidenceText }}</pre>
        </div>
      </div>
    </el-dialog>

    <!-- P7.4：初筛待办确认结果（深度检测） -->
    <el-dialog v-model="confirmResultVisible" title="初筛待办确认结果" width="560px">
      <div v-if="confirmResult">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="风险等级">
            <el-tag size="small" :type="riskType[confirmResult.risk]">{{ confirmResult.risk }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="综合评分">{{ confirmResult.total_score }}</el-descriptions-item>
          <el-descriptions-item label="文本相似检">{{ confirmResult.scores?.text ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="关系图谱检">{{ confirmResult.scores?.graph ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="报价检">{{ confirmResult.scores?.price ?? '-' }}</el-descriptions-item>
        </el-descriptions>
        <el-alert
          v-if="confirmResult.released"
          type="success"
          :closable="false"
          title="深度检测通过，已放行进入评审"
          style="margin-top: 12px"
        />
        <el-alert
          v-else
          type="error"
          :closable="false"
          title="深度检测高风险，未放行。请到标书列表标记废标或人工复核。"
          style="margin-top: 12px"
        />
        <div v-if="confirmResult.evidence" class="evidence">
          <div class="struct-title">深度检测证据</div>
          <pre class="evidence-pre">{{ JSON.stringify(confirmResult.evidence, null, 2) }}</pre>
        </div>
      </div>
      <template #footer>
        <el-button @click="confirmResultVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { listLots, closeBidding, confirmPrescreen } from '../../api/lots'

const statusLabel = {
  BIDDING: '投标中',
  PRE_SCREEN: '初筛待办',
  UNDER_REVIEW: '评审中',
  EVALUATED: '已评审',
  ABANDONED: '已废弃',
  DISQUALIFIED: '已废标',
}
const statusType = {
  BIDDING: 'primary',
  PRE_SCREEN: 'warning',
  UNDER_REVIEW: 'success',
  EVALUATED: 'success',
  ABANDONED: 'info',
  DISQUALIFIED: 'danger',
}
const riskType = { LOW: 'success', MEDIUM: 'warning', HIGH: 'danger', CRITICAL: 'danger' }

const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const statusFilter = ref('')
const loading = ref(false)

const closeVisible = ref(false)
const closing = ref(null)
const closingLoading = ref(false)
const resultVisible = ref(false)
const result = ref(null)

// P7.4：初筛待办确认放行（深度检测）
const confirmingLot = ref('')
const confirmResultVisible = ref(false)
const confirmResult = ref(null)

const evidenceText = computed(() => {
  const ev = result.value?.evidence
  if (!ev) return ''
  return JSON.stringify(ev, null, 2)
})

async function load(p = page.value) {
  loading.value = true
  try {
    const data = await listLots({ page: p, page_size: pageSize.value, status: statusFilter.value || undefined })
    rows.value = data.items || []
    total.value = data.total || 0
  } finally {
    loading.value = false
  }
}

function confirmClose(row) {
  closing.value = row
  closeVisible.value = true
}

async function doClose() {
  closingLoading.value = true
  try {
    const data = await closeBidding(closing.value.lot_id)
    result.value = data
    closeVisible.value = false
    resultVisible.value = true
    load()
  } catch {
    // 错误 toast 由拦截器处理
  } finally {
    closingLoading.value = false
  }
}

// P7.4：确认放行（深度检测 → 放行 / 建议废标）
async function doConfirm(row) {
  confirmingLot.value = row.lot_id
  try {
    confirmResult.value = await confirmPrescreen(row.lot_id)
    confirmResultVisible.value = true
    load()
  } catch {
    // 错误 toast 由拦截器处理
  } finally {
    confirmingLot.value = ''
  }
}

function fmtWan(v) {
  if (v == null) return '-'
  return (Number(v) / 10000).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
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
.pager {
  margin-top: 14px;
  justify-content: flex-end;
}
.empty-guide {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.7;
  margin: 0;
}
.evidence {
  margin-top: 14px;
}
.evidence-pre {
  background: var(--el-fill-color-light);
  border-radius: 6px;
  padding: 12px;
  font-size: 12px;
  max-height: 240px;
  overflow: auto;
  white-space: pre-wrap;
}
</style>
