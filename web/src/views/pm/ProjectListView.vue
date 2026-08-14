<template>
  <el-card shadow="never">
    <div class="toolbar">
      <el-input
        v-model="keyword"
        placeholder="搜索项目名称 / 编码"
        clearable
        :prefix-icon="Search"
        class="search-input"
        @keyup.enter="load(1)"
        @clear="load(1)"
      />
      <el-button type="primary" :icon="Plus" @click="openCreate">新建项目</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" border stripe @row-click="openDetail">
      <el-table-column prop="project_code" label="编码" width="120" />
      <el-table-column prop="name" label="项目名称" min-width="180" show-overflow-tooltip />
      <el-table-column label="类型" width="100">
        <template #default="{ row }">
          <el-tag size="small" effect="plain">{{ typeLabel[row.type] || row.type }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="region" label="地区" width="80">
        <template #default="{ row }">{{ row.region || '-' }}</template>
      </el-table-column>
      <el-table-column label="预算(万元)" width="120" align="right">
        <template #default="{ row }">{{ fmtWan(row.budget) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag size="small" :type="statusType[row.status]">{{ statusLabel[row.status] || row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="创建时间" width="170">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click.stop="openDetail(row)">标段</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="暂无项目">
          <p class="empty-guide">点击右上「新建项目」，创建采购项目并配置标段与评分维度。</p>
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

    <!-- 新建项目 -->
    <el-dialog v-model="createVisible" title="新建项目" width="520px">
      <el-form ref="createForm" :model="form" :rules="formRules" label-width="90px">
        <el-form-item label="项目编码" prop="project_code">
          <el-input v-model="form.project_code" placeholder="如 PRJ-2026-001" />
        </el-form-item>
        <el-form-item label="项目名称" prop="name">
          <el-input v-model="form.name" />
        </el-form-item>
        <el-form-item label="类型" prop="type">
          <el-select v-model="form.type" style="width: 100%">
            <el-option v-for="(label, val) in typeLabel" :key="val" :label="label" :value="val" />
          </el-select>
        </el-form-item>
        <el-form-item label="地区" prop="region">
          <el-select v-model="form.region" clearable style="width: 100%">
            <el-option v-for="r in regions" :key="r" :label="r" :value="r" />
          </el-select>
        </el-form-item>
        <el-form-item label="预算(万元)" prop="budget">
          <el-input-number v-model="form.budget" :precision="2" style="width: 100%" />
        </el-form-item>
        <el-form-item label="负责人ID" prop="managed_by">
          <el-input v-model="form.managed_by" placeholder="项目经理 user_id，可留空" clearable />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>

    <!-- 项目详情 + 标段管理 -->
    <el-dialog v-model="detailVisible" :title="current?.name || '项目详情'" width="720px">
      <el-descriptions :column="3" border size="small">
        <el-descriptions-item label="编码">{{ current?.project_code }}</el-descriptions-item>
        <el-descriptions-item label="类型">{{ typeLabel[current?.type] || current?.type }}</el-descriptions-item>
        <el-descriptions-item label="地区">{{ current?.region || '-' }}</el-descriptions-item>
        <el-descriptions-item label="预算(万元)">{{ fmtWan(current?.budget) }}</el-descriptions-item>
        <el-descriptions-item label="状态">{{ statusLabel[current?.status] || current?.status }}</el-descriptions-item>
        <el-descriptions-item label="负责人ID">{{ current?.managed_by || '-' }}</el-descriptions-item>
      </el-descriptions>

      <div class="lot-header">
        <span class="lot-title">标段</span>
        <el-button type="primary" size="small" :icon="Plus" @click="openCreateLot">新建标段</el-button>
      </div>
      <el-table v-loading="detailLoading" :data="lots" border stripe size="small">
        <el-table-column prop="lot_code" label="标段编码" width="140" />
        <el-table-column prop="name" label="标段名称" min-width="160" show-overflow-tooltip />
        <el-table-column label="预算(万元)" width="120" align="right">
          <template #default="{ row }">{{ fmtWan(row.budget) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="statusType[row.status]">{{ statusLabel[row.status] || row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="openDims(row)">维度</el-button>
            <el-button link type="primary" @click.stop="goBids(row)">标书</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无标段" />
        </template>
      </el-table>

      <!-- P7.4：评分维度配置（PM 端此前无表单，此处补齐） -->
      <el-dialog v-model="dimVisible" :title="`评分维度配置 · ${dimLot?.lot_code || ''}`" width="680px" append-to-body>
        <el-alert
          type="info"
          :closable="false"
          title="配置评分维度（满分/权重），权重和需为 1.0。报价维度名固定，评审走纯公式。"
          style="margin-bottom: 12px"
        />
        <el-table :data="dimsForm" border size="small">
          <el-table-column label="维度名" min-width="140">
            <template #default="{ row }">
              <el-input v-model="row.name" size="small" />
            </template>
          </el-table-column>
          <el-table-column label="满分" width="120">
            <template #default="{ row }">
              <el-input-number v-model="row.max_score" :min="1" :max="100" size="small" />
            </template>
          </el-table-column>
          <el-table-column label="权重" width="140">
            <template #default="{ row }">
              <el-input-number v-model="row.weight" :min="0" :max="1" :step="0.05" :precision="2" size="small" />
            </template>
          </el-table-column>
          <el-table-column label="操作" width="70" align="center">
            <template #default="{ $index }">
              <el-button link type="danger" size="small" @click="dimsForm.splice($index, 1)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div class="dim-actions">
          <el-button size="small" @click="dimsForm.push({ name: '', max_score: 10, weight: 0.1 })">添加维度</el-button>
          <el-tag :type="dimsWeightOk ? 'success' : 'danger'" size="small">
            权重和 {{ dimsWeight.toFixed(2) }}
          </el-tag>
        </div>
        <template #footer>
          <el-button @click="dimVisible = false">取消</el-button>
          <el-button type="primary" :loading="dimSaving" :disabled="!dimsWeightOk" @click="saveDims">保存</el-button>
        </template>
      </el-dialog>

      <!-- 新建标段 -->
      <el-dialog v-model="lotVisible" title="新建标段" width="460px" append-to-body>
        <el-form ref="lotForm" :model="lotForm" :rules="lotRules" label-width="90px">
          <el-form-item label="标段编码" prop="lot_code">
            <el-input v-model="lotForm.lot_code" placeholder="如 LOT-001" />
          </el-form-item>
          <el-form-item label="标段名称" prop="name">
            <el-input v-model="lotForm.name" />
          </el-form-item>
          <el-form-item label="预算(万元)" prop="budget">
            <el-input-number v-model="lotForm.budget" :precision="2" style="width: 100%" />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="lotVisible = false">取消</el-button>
          <el-button type="primary" :loading="lotSaving" @click="submitCreateLot">创建</el-button>
        </template>
      </el-dialog>
    </el-dialog>
  </el-card>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Search, Plus } from '@element-plus/icons-vue'
import { listProjects, getProject, createProject, createLot, saveLotDimensions } from '../../api/projects'

const router = useRouter()

const typeLabel = { GOODS: '货物', SERVICE: '服务', ENGINEERING: '工程' }
const statusLabel = { DRAFT: '草稿', BIDDING: '招标中', UNDER_REVIEW: '评审中', AWARDED: '已定标' }
const statusType = { DRAFT: 'info', BIDDING: 'warning', UNDER_REVIEW: 'primary', AWARDED: 'success' }
const regions = ['华东', '华南', '华北', '华中', '西南', '西北', '东北']

const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const keyword = ref('')
const loading = ref(false)

const createVisible = ref(false)
const saving = ref(false)
const form = ref({ project_code: '', name: '', type: 'SERVICE', region: '', budget: 100, managed_by: '' })
const formRules = {
  project_code: [{ required: true, message: '请输入项目编码', trigger: 'blur' }],
  name: [{ required: true, message: '请输入项目名称', trigger: 'blur' }],
}

const detailVisible = ref(false)
const detailLoading = ref(false)
const current = ref(null)
const lots = ref([])

const lotVisible = ref(false)
const lotSaving = ref(false)
const lotForm = ref({ lot_code: '', name: '', budget: 10 })

// P7.4：评分维度配置（默认 5 维，权重和 1.0，报价维度固定名）
const dimVisible = ref(false)
const dimSaving = ref(false)
const dimLot = ref(null)
const dimsForm = ref([])
const DEFAULT_DIMS = [
  { name: '报价', max_score: 20, weight: 0.2 },
  { name: '技术', max_score: 30, weight: 0.3 },
  { name: '商务', max_score: 25, weight: 0.25 },
  { name: '服务', max_score: 15, weight: 0.15 },
  { name: '资信', max_score: 10, weight: 0.1 },
]
const dimsWeight = computed(() => dimsForm.value.reduce((s, d) => s + (Number(d.weight) || 0), 0))
const dimsWeightOk = computed(() => Math.abs(dimsWeight.value - 1) < 0.001)

function openDims(lot) {
  dimLot.value = lot
  dimsForm.value = DEFAULT_DIMS.map((d) => ({ ...d }))
  dimVisible.value = true
}

async function saveDims() {
  dimSaving.value = true
  try {
    await saveLotDimensions(dimLot.value.lot_id, dimsForm.value)
    ElMessage.success('维度配置已保存')
    dimVisible.value = false
  } catch {
    // 错误 toast 由拦截器处理
  } finally {
    dimSaving.value = false
  }
}
const lotRules = {
  lot_code: [{ required: true, message: '请输入标段编码', trigger: 'blur' }],
  name: [{ required: true, message: '请输入标段名称', trigger: 'blur' }],
}

async function load(p = page.value) {
  loading.value = true
  try {
    const data = await listProjects({ page: p, page_size: pageSize.value, keyword: keyword.value || undefined })
    rows.value = data.items || []
    total.value = data.total || 0
  } finally {
    loading.value = false
  }
}

function openCreate() {
  Object.assign(form.value, { project_code: '', name: '', type: 'SERVICE', region: '', budget: 100, managed_by: '' })
  createVisible.value = true
}

async function submitCreate() {
  saving.value = true
  try {
    // 前端以万元输入，后端契约单位为元，提交时换算
    const payload = {
      ...form.value,
      budget: Math.round(Number(form.value.budget) * 10000),
      managed_by: form.value.managed_by || null,
    }
    await createProject(payload)
    ElMessage.success('项目创建成功')
    createVisible.value = false
    load()
  } catch {
    // 错误 toast 由拦截器处理
  } finally {
    saving.value = false
  }
}

async function openDetail(row) {
  current.value = row
  detailVisible.value = true
  await loadLots(row.project_id)
}

async function loadLots(projectId) {
  detailLoading.value = true
  try {
    const data = await getProject(projectId)
    lots.value = data.lots || []
  } finally {
    detailLoading.value = false
  }
}

function goBids(lot) {
  detailVisible.value = false
  router.push({ path: `/pm/lots/${lot.lot_id}/bids`, query: { name: lot.name } })
}

function openCreateLot() {
  Object.assign(lotForm.value, { lot_code: '', name: '', budget: 10 })
  lotVisible.value = true
}

async function submitCreateLot() {
  lotSaving.value = true
  try {
    const payload = {
      ...lotForm.value,
      budget: Math.round(Number(lotForm.value.budget) * 10000),
    }
    await createLot(current.value.project_id, payload)
    ElMessage.success('标段创建成功')
    lotVisible.value = false
    await loadLots(current.value.project_id)
    load()
  } catch {
    // 错误 toast 由拦截器处理
  } finally {
    lotSaving.value = false
  }
}

// 预算单位为元，展示时换算为万元
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
  justify-content: space-between;
  margin-bottom: 14px;
}
.search-input {
  width: 280px;
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
.lot-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin: 18px 0 10px;
}
.lot-title {
  font-weight: 600;
}
</style>
