<template>
  <el-card shadow="never">
    <div class="card-header">
      <h3 class="card-title">{{ title }}</h3>
      <el-button
        v-if="templateType"
        link
        type="primary"
        :loading="downloading"
        @click="downloadTemplate"
      >
        <el-icon><Download /></el-icon> 下载上传模板
      </el-button>
    </div>

    <el-upload
      drag
      :http-request="doUpload"
      :show-file-list="false"
      :accept="accept"
      :disabled="uploading"
      class="upload-box"
    >
      <el-icon :size="48" color="#c0c4cc"><UploadFilled /></el-icon>
      <div class="el-upload__text">将文件拖到此处，或 <em>点击上传</em></div>
      <template #tip>
        <div class="el-upload__tip">{{ tips }}</div>
      </template>
    </el-upload>

    <el-alert v-if="result" :type="result.success ? 'success' : 'error'" :closable="false" class="result-box">
      <pre class="result-text">{{ result.message }}</pre>
    </el-alert>

    <el-empty v-else-if="!uploading && !result" description="尚未上传文件" />
  </el-card>
</template>

<script setup>
import { ref } from 'vue'
import { Download } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import client from '../api/client'

const props = defineProps({
  title: { type: String, required: true },
  action: { type: Function, required: true }, // (file: File) => Promise<data>
  accept: { type: String, default: '.xlsx,.xls,.csv' },
  tips: { type: String, default: '支持 Excel（.xlsx/.xls）或 CSV 文件' },
  successMessage: { type: Function, default: (data) => JSON.stringify(data, null, 2) },
  // 传入模板类型（expert/supplier/conflict）时显示「下载上传模板」按钮
  templateType: { type: String, default: '' },
})

const uploading = ref(false)
const downloading = ref(false)
const result = ref(null)

// 下载文件名与后端 _TEMPLATES 一致（后端返回 RFC5987 中文名，前端走 blob 时写死）
const TEMPLATE_FILENAMES = {
  expert: '专家导入模板.xlsx',
  supplier: '供应商导入模板.xlsx',
  conflict: '工商信息冲突导入模板.csv',
}

async function downloadTemplate() {
  downloading.value = true
  try {
    // 走 axios 带 Bearer 鉴权（后端仅 ADMIN），<a href> 直链会 401
    const blob = await client.get(`/import-templates/${props.templateType}`, { responseType: 'blob' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = TEMPLATE_FILENAMES[props.templateType] || `template.${props.templateType}`
    a.click()
    URL.revokeObjectURL(url)
  } catch {
    // 错误 toast 由拦截器处理
  } finally {
    downloading.value = false
  }
}

async function doUpload({ file }) {
  uploading.value = true
  result.value = null
  try {
    const data = await props.action(file)
    result.value = { success: true, message: props.successMessage(data) }
    ElMessage.success('导入成功')
  } catch (e) {
    const detail = e.response?.data?.detail
    let msg = ''
    if (Array.isArray(detail)) msg = detail.map((d) => d.msg || JSON.stringify(d)).join('；')
    else if (typeof detail === 'string') msg = detail
    else msg = e.message || '导入失败'
    result.value = { success: false, message: msg }
    ElMessage.error(msg)
  } finally {
    uploading.value = false
  }
}
</script>

<style scoped>
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}
.card-title {
  margin: 0;
}
.upload-box {
  margin-bottom: 16px;
}
.result-box {
  margin-top: 12px;
}
.result-text {
  margin: 0;
  white-space: pre-wrap;
  font-size: 13px;
  line-height: 1.6;
}
</style>
