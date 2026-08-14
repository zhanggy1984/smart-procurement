<template>
  <div>
    <el-page-header class="page-head" @back="router.back()">
      <template #content>
        <span class="page-title">标书详情</span>
      </template>
    </el-page-header>

    <el-card shadow="never" v-loading="loading">
      <template #header>
        <div class="card-header">
          <span>{{ detail?.project_name }} · {{ detail?.lot_name }}</span>
          <el-tag :type="parseTagType(detail?.status)" size="small" class="status-tag">
            {{ parseLabel(detail?.status) }}
          </el-tag>
        </div>
      </template>

      <el-descriptions :column="4" border v-if="detail">
        <el-descriptions-item label="标段编号">{{ detail.lot_code }}</el-descriptions-item>
        <el-descriptions-item label="投标方">{{ auth.displayName }}</el-descriptions-item>
        <el-descriptions-item label="报价">{{ fmtWan(detail.bid_amount) }}</el-descriptions-item>
        <el-descriptions-item label="工期">{{ detail.duration ? `${detail.duration} 天` : '-' }}</el-descriptions-item>
        <el-descriptions-item label="团队规模">{{ detail.team_size ? `${detail.team_size} 人` : '-' }}</el-descriptions-item>
        <el-descriptions-item label="解析进度">第 {{ detail.parsing_step || 0 }} / 5 步</el-descriptions-item>
      </el-descriptions>

      <div class="download" v-if="detail?.presigned_url">
        <el-button type="primary" plain :href="detail.presigned_url" tag="a" target="_blank">
          <el-icon><Download /></el-icon>&nbsp;下载投标文件
        </el-button>
      </div>
    </el-card>

    <el-card shadow="never" class="struct-card" v-if="detail?.structured_data">
      <template #header>
        <div class="card-header">结构化解析结果</div>
      </template>
      <el-descriptions :column="2" border>
        <el-descriptions-item v-for="(v, k) in detail.structured_data" :key="k" :label="k">
          <template v-if="isPlain(v)">{{ v }}</template>
          <div v-else class="nested">
            <el-descriptions :column="1" size="small" border>
              <el-descriptions-item v-for="(vv, kk) in v" :key="kk" :label="kk">
                {{ isPlain(vv) ? vv : JSON.stringify(vv) }}
              </el-descriptions-item>
            </el-descriptions>
          </div>
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-empty v-else-if="detail && detail.status !== 'PARSED'" description="解析尚未完成，暂无结构化数据" />
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getMyBidDetail } from '../../api/suppliers'
import { useAuthStore } from '../../stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const loading = ref(false)
const detail = ref(null)

const PARSE_LABEL = {
  SUBMITTED: '已提交', PARSING: '解析中', PARSED: '解析完成',
  PARSE_FAILED: '解析失败', FROZEN: '已封存', DISQUALIFIED: '已废标',
}
const PARSE_TAG = {
  PARSED: 'success', FROZEN: 'success', SUBMITTED: 'info',
  PARSING: 'warning', PARSE_FAILED: 'danger', DISQUALIFIED: 'danger',
}
const parseLabel = (s) => PARSE_LABEL[s] || s || '-'
const parseTagType = (s) => PARSE_TAG[s] || 'info'

function isPlain(v) {
  return typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean' || v === null
}

function fmtWan(v) {
  if (v == null) return '-'
  return `${(v / 10000).toFixed(1)} 万`
}

async function load() {
  loading.value = true
  try {
    detail.value = await getMyBidDetail(route.params.bidId)
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
.card-header {
  display: flex;
  align-items: center;
  gap: 10px;
  font-weight: 600;
}
.status-tag {
  margin-left: auto;
}
.struct-card {
  margin-top: 16px;
}
.download {
  margin-top: 14px;
}
.nested {
  width: 100%;
}
</style>
