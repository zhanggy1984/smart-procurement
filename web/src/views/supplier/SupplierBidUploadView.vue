<template>
  <div>
    <el-page-header class="page-head" @back="router.back()">
      <template #content>
        <span class="page-title">参与投标 - {{ lot?.name }}</span>
      </template>
    </el-page-header>

    <el-card shadow="never" v-if="lot" class="info-card">
      <el-descriptions :column="3" border>
        <el-descriptions-item label="标段编号">{{ lot.lot_code }}</el-descriptions-item>
        <el-descriptions-item label="预算">{{ fmtWan(lot.budget) }}</el-descriptions-item>
        <el-descriptions-item label="投标方">{{ auth.displayName }}</el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">上传投标文件（PDF / DOCX，≤50MB）</div>
      </template>

      <el-upload
        drag
        :auto-upload="false"
        :limit="1"
        :on-change="onFileChange"
        :on-remove="onFileRemove"
        accept=".pdf,.doc,.docx"
        class="uploader"
      >
        <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
        <div class="el-upload__text">将文件拖到此处，或 <em>点击选择</em></div>
        <template #tip>
          <div class="el-upload__tip">支持 PDF/DOC/DOCX 格式，单个文件不超过 50MB。</div>
        </template>
      </el-upload>

      <div v-if="selectedFile" class="file-info">
        <div class="info-row">
          <span class="info-label">文件名</span>
          <span>{{ selectedFile.name }}（{{ fmtSize(selectedFile.size) }}）</span>
        </div>
        <div class="info-row">
          <span class="info-label">SHA-256</span>
          <code class="hash">{{ sha256 || '计算中…' }}</code>
        </div>
        <div v-if="parseStep > 0" class="info-row">
          <span class="info-label">解析进度</span>
          <el-progress class="parse-progress" :percentage="parsePercent" :status="parseStatus" :stroke-width="14" />
        </div>
      </div>

      <div class="actions" v-if="selectedFile">
        <el-button type="primary" :loading="uploading || parsing" :disabled="!sha256" @click="submit">
          {{ uploading ? '上传中…' : '提交投标' }}
        </el-button>
        <el-button v-if="uploadDone" type="success" @click="goDetail">查看标书详情</el-button>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { UploadFilled } from '@element-plus/icons-vue'
import { getLot } from '../../api/lots'
import { getMyBidDetail, uploadBid } from '../../api/suppliers'
import { useAuthStore } from '../../stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const lotId = route.params.lotId

const lot = ref(null)
const selectedFile = ref(null)
const sha256 = ref('')
const uploading = ref(false)
const parsing = ref(false)
const uploadDone = ref(false)
const parseStep = ref(0)
const bidId = ref('')
const timer = null

const parsePercent = computed(() => Math.min(parseStep.value * 20, 100))
const parseStatus = computed(() => {
  if (parseStep.value >= 5) return 'success'
  return undefined
})

async function load() {
  lot.value = await getLot(lotId)
}

function fmtSize(bytes) {
  if (bytes == null) return '-'
  return bytes > 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${(bytes / 1024).toFixed(1)} KB`
}

function fmtWan(v) {
  if (v == null) return '-'
  return `${(v / 10000).toFixed(1)} 万`
}

async function computeSha256(file) {
  const buf = await file.arrayBuffer()
  const digest = await crypto.subtle.digest('SHA-256', buf)
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('')
}

function onFileChange(file) {
  selectedFile.value = file.raw
  sha256.value = ''
  parseStep.value = 0
  uploadDone.value = false
  computeSha256(file.raw).then((h) => {
    sha256.value = h
  })
}

function onFileRemove() {
  selectedFile.value = null
  sha256.value = ''
}

async function submit() {
  uploading.value = true
  try {
    const result = await uploadBid(lotId, selectedFile.value, () => {})
    bidId.value = result.bid_id
    ElMessage.success('投标已提交，开始解析文件')
    // 轮询解析状态：SUBMITTED → PARSING → PARSED / PARSE_FAILED
    parsing.value = true
    pollStatus()
  } catch (e) {
    /* 全局拦截器已 toast */
  } finally {
    uploading.value = false
  }
}

async function pollStatus() {
  try {
    const detail = await getMyBidDetail(bidId.value)
    parseStep.value = detail.parsing_step || 0
    const st = detail.status
    if (st === 'PARSED') {
      parsing.value = false
      uploadDone.value = true
      ElMessage.success('解析完成')
      return
    }
    if (st === 'PARSE_FAILED') {
      parsing.value = false
      uploadDone.value = true
      ElMessage.warning('解析失败，标书仍可查看')
      return
    }
    setTimeout(pollStatus, 2000)
  } catch (e) {
    setTimeout(pollStatus, 3000)
  }
}

function goDetail() {
  router.push(`/supplier/bids/${bidId.value}`)
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
.info-card {
  margin-bottom: 16px;
}
.card-header {
  font-weight: 600;
}
.uploader :deep(.el-upload-dragger) {
  padding: 32px 0;
}
.file-info {
  margin-top: 16px;
  padding: 12px 16px;
  background: var(--el-fill-color-light);
  border-radius: 6px;
}
.info-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 6px 0;
  font-size: 13px;
}
.info-label {
  color: var(--el-text-color-secondary);
  flex-shrink: 0;
}
.hash {
  word-break: break-all;
  font-size: 12px;
}
.parse-progress {
  flex: 1;
  max-width: 320px;
}
.actions {
  margin-top: 16px;
}
</style>
