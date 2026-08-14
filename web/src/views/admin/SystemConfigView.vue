<template>
  <el-card shadow="never">
    <PageHeader title="系统配置" desc="配置保存后即时生效，无需重启服务；未接入业务逻辑的配置项仅作预留存储。">
      <el-button type="primary" :loading="saving" @click="save">保存</el-button>
    </PageHeader>

    <el-empty v-if="!loading && !error && !items.length" description="暂无配置项" />
    <el-alert v-else-if="error" type="error" :closable="false" :title="error" />

    <div v-loading="loading" class="group-list">
      <div v-for="(group, gkey) in groups" :key="gkey" class="group">
        <h4 class="group-title">{{ GROUP_LABELS[gkey] }}</h4>
        <div v-for="item in group" :key="item.key" class="row">
          <div class="row-main">
            <div class="row-label">
              {{ item.label }}
              <el-tag :type="item.wired ? 'success' : 'info'" size="small" class="wired-tag">
                {{ item.wired ? '已接入' : '未接入业务' }}
              </el-tag>
            </div>
            <div class="row-desc">{{ item.description }}（默认 {{ item.default_value }}）</div>
          </div>
          <el-input-number
            :model-value="values[item.key]"
            @update:model-value="(v) => setValue(item.key, v)"
            :min="item.min"
            :max="item.max"
            :step="isIntItem(item) ? 1 : 0.05"
            :precision="isIntItem(item) ? 0 : 2"
            :controls="false"
            class="value-input"
          />
        </div>
      </div>
    </div>
  </el-card>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import PageHeader from '../../components/PageHeader.vue'
import { getConfig, updateConfig } from '../../api/config'

// 按配置键前缀分组（键名见后端 config_service._DEFAULTS）
const GROUP_LABELS = { fraud: '围串标检测', llm: 'LLM 参数', conflict: '回避规则', review: '评审' }
const GROUP_OF = (key) => {
  const prefix = key.split('.')[0]
  return GROUP_LABELS[prefix] ? prefix : 'fraud'
}

const items = ref([])
const values = reactive({})
const loading = ref(false)
const saving = ref(false)
const error = ref('')

const groups = computed(() => {
  const acc = {}
  for (const it of items.value) {
    const g = GROUP_OF(it.key)
    ;(acc[g] ||= []).push(it)
  }
  return acc
})

function isIntItem(item) {
  return Number(item.default_value) % 1 === 0
}

function setValue(key, v) {
  values[key] = v
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await getConfig()
    items.value = data.items || []
    for (const it of items.value) {
      values[it.key] = Number(it.value)
    }
  } catch (e) {
    error.value = e.response?.data?.detail || e.message || '加载失败'
  } finally {
    loading.value = false
  }
}

async function save() {
  // 只提交有变更的项
  const changed = items.value
    .filter((it) => values[it.key] !== Number(it.value))
    .map((it) => ({ key: it.key, value: values[it.key] }))
  if (!changed.length) {
    ElMessage.info('没有需要保存的变更')
    return
  }
  saving.value = true
  try {
    await updateConfig(changed)
    ElMessage.success('保存成功，配置已生效')
    await load()
  } catch {
    // 错误 toast 由拦截器处理
  } finally {
    saving.value = false
  }
}

onMounted(() => load())
</script>

<style scoped>
.group {
  margin-bottom: 20px;
}
.group-title {
  margin: 0 0 8px;
  font-size: 14px;
  color: #409eff;
}
.row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 4px;
  border-bottom: 1px solid #f0f0f0;
}
.row-main {
  min-width: 0;
  margin-right: 24px;
}
.row-label {
  font-weight: 500;
}
.wired-tag {
  margin-left: 8px;
}
.row-desc {
  font-size: 12px;
  color: #909399;
  margin-top: 2px;
}
.value-input {
  width: 160px;
  flex-shrink: 0;
}
</style>
