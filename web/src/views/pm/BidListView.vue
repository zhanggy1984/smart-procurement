<template>
  <el-card shadow="never">
    <div class="toolbar">
      <el-button :icon="ArrowLeft" @click="$router.back()">返回</el-button>
      <span class="page-title">标书列表 · {{ route.query.name || route.params.lotId }}</span>
      <el-tag v-if="route.params.lotId" size="small">{{ route.params.lotId }}</el-tag>
    </div>

    <el-table v-loading="loading" :data="rows" border stripe>
      <el-table-column prop="supplier_name" label="供应商" min-width="170" show-overflow-tooltip />
      <el-table-column label="报价(万元)" width="120" align="right">
        <template #default="{ row }">{{ fmtWan(row.bid_amount) }}</template>
      </el-table-column>
      <el-table-column prop="duration" label="工期(天)" width="90">
        <template #default="{ row }">{{ row.duration ?? '-' }}</template>
      </el-table-column>
      <el-table-column prop="team_size" label="团队(人)" width="90">
        <template #default="{ row }">{{ row.team_size ?? '-' }}</template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag size="small" :type="statusType[row.status]">{{ statusLabel[row.status] || row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="解析进度" width="120">
        <template #default="{ row }">
          <span v-if="row.status === 'PARSED'" class="ok-text">已完成</span>
          <span v-else-if="row.status === 'PARSE_FAILED'" class="err-text">解析失败</span>
          <span v-else-if="row.status === 'FROZEN'">已封存</span>
          <span v-else>第 {{ row.parsing_step || 0 }} 步</span>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="提交时间" width="170">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="140" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openDetail(row)">详情</el-button>
          <el-button
            v-if="row.status === 'PARSED' || row.status === 'FROZEN'"
            link
            type="danger"
            @click="doDisqualify(row)"
          >废标</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="该标段暂无标书">
          <p class="empty-guide">供应商可在投标期内上传标书，关闭投标后自动执行围串标初筛。</p>
        </el-empty>
      </template>
    </el-table>

    <!-- 标书详情 -->
    <el-dialog v-model="detailVisible" title="标书详情" width="680px">
      <div v-loading="detailLoading">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="供应商">{{ detail.supplier_name }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag size="small" :type="statusType[detail.status]">{{ statusLabel[detail.status] || detail.status }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="报价(万元)">{{ fmtWan(detail.bid_amount) }}</el-descriptions-item>
          <el-descriptions-item label="工期/团队">
            {{ detail.duration ?? '-' }} 天 / {{ detail.team_size ?? '-' }} 人
          </el-descriptions-item>
          <el-descriptions-item label="解析进度">
            <span v-if="detail.status === 'PARSED'" class="ok-text">已完成</span>
            <span v-else-if="detail.status === 'PARSE_FAILED'" class="err-text">失败（第 {{ detail.parsing_step || 0 }} 步）</span>
            <span v-else>第 {{ detail.parsing_step || 0 }} 步</span>
          </el-descriptions-item>
          <el-descriptions-item label="提交时间">{{ fmtTime(detail.created_at) }}</el-descriptions-item>
        </el-descriptions>

        <el-alert
          v-if="detail.status === 'PARSE_FAILED'"
          type="error"
          :closable="false"
          title="解析失败"
          description="可重试解析（管理员操作）"
          style="margin-top: 12px"
        />

        <div v-if="detail.presigned_url" style="margin-top: 14px">
          <el-button type="primary" :icon="Download" tag="a" :href="detail.presigned_url" target="_blank" plain>
            下载原文件
          </el-button>
        </div>

        <div v-if="structuredRows.length" class="struct-block">
          <div class="struct-title">结构化数据</div>
          <el-table :data="structuredRows" border size="small">
            <el-table-column prop="key" label="字段" width="160" />
            <el-table-column prop="value" label="值" />
          </el-table>
        </div>
      </div>
    </el-dialog>
  </el-card>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { ArrowLeft, Download } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { listLotBids, getBid } from '../../api/bids'
import { disqualifyBid } from '../../api/lots'

const route = useRoute()

const statusLabel = {
  SUBMITTED: '已提交',
  PARSING: '解析中',
  PARSED: '已解析',
  PARSE_FAILED: '解析失败',
  FROZEN: '已封存',
  DISQUALIFIED: '已废标',
}
const statusType = {
  SUBMITTED: 'info',
  PARSING: 'warning',
  PARSED: 'success',
  PARSE_FAILED: 'danger',
  FROZEN: 'primary',
  DISQUALIFIED: 'danger',
}

const rows = ref([])
const loading = ref(false)
const detailVisible = ref(false)
const detailLoading = ref(false)
const detail = ref({})

const structuredRows = computed(() => {
  const s = detail.value.structured_data
  if (!s || typeof s !== 'object') return []
  return Object.entries(s).map(([key, value]) => ({ key, value: typeof value === 'object' ? JSON.stringify(value) : String(value ?? '-') }))
})

async function load() {
  loading.value = true
  try {
    const data = await listLotBids(route.params.lotId)
    rows.value = data.items || []
  } finally {
    loading.value = false
  }
}

// P7.4：PM 废标（初筛待办/评审中标记标书 DISQUALIFIED）
async function doDisqualify(row) {
  try {
    await ElMessageBox.confirm(
      `确认将供应商「${row.supplier_name}」的标书标记为废标？废标后该标书不参与评审与定标。`,
      '标记废标', { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await disqualifyBid(route.params.lotId, row.bid_id)
    ElMessage.success('已废标')
    load()
  } catch {
    // 错误 toast 由拦截器处理
  }
}

async function openDetail(row) {
  detailVisible.value = true
  detailLoading.value = true
  detail.value = {}
  try {
    const data = await getBid(row.bid_id)
    detail.value = { ...data, supplier_name: row.supplier_name }
  } finally {
    detailLoading.value = false
  }
}

function fmtWan(v) {
  if (v == null) return '-'
  return (Number(v) / 10000).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

function fmtTime(t) {
  return t ? t.replace('T', ' ').slice(0, 19) : '-'
}

onMounted(() => load())
</script>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
}
.page-title {
  font-weight: 600;
}
.ok-text {
  color: var(--el-color-success);
}
.err-text {
  color: var(--el-color-danger);
}
.empty-guide {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.7;
  margin: 0;
}
.struct-block {
  margin-top: 16px;
}
.struct-title {
  font-weight: 600;
  margin-bottom: 8px;
}
</style>
