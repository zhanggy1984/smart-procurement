<template>
  <div>
    <!-- 筛选栏：类型 / 地区 / 预算区间 -->
    <el-card shadow="never" class="filter-card">
      <el-form inline :model="query" @submit.prevent>
        <el-form-item label="项目类型">
          <el-select v-model="query.project_type" placeholder="全部类型" clearable style="width: 140px">
            <el-option v-for="t in filters.types" :key="t" :label="typeLabel(t)" :value="t" />
          </el-select>
        </el-form-item>
        <el-form-item label="地区">
          <el-select v-model="query.region" placeholder="全部地区" clearable style="width: 140px">
            <el-option v-for="r in filters.regions" :key="r" :label="r" :value="r" />
          </el-select>
        </el-form-item>
        <el-form-item label="预算（万元）">
          <el-input-number v-model="query.budget_min" :min="0" :controls="false" placeholder="最低" style="width: 110px" />
          <span class="range-sep">-</span>
          <el-input-number v-model="query.budget_max" :min="0" :controls="false" placeholder="最高" style="width: 110px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="onSearch">查询</el-button>
          <el-button @click="onReset">重置</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 标段列表 -->
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>招标市场（共 {{ total }} 个标段）</span>
        </div>
      </template>
      <el-table :data="items" v-loading="loading" @row-click="goLot">
        <el-table-column prop="lot_code" label="标段编号" width="110" />
        <el-table-column label="标段名称" min-width="180">
          <template #default="{ row }">
            <el-link type="primary" @click.stop="goLot(row)">{{ row.name }}</el-link>
          </template>
        </el-table-column>
        <el-table-column label="项目名称" min-width="160">
          <template #default="{ row }">
            <el-link type="primary" @click.stop="goProject(row)">{{ row.project_name }}</el-link>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="90">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ typeLabel(row.type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="region" label="地区" width="80" />
        <el-table-column label="预算" width="120">
          <template #default="{ row }">{{ fmtWan(row.budget) }}</template>
        </el-table-column>
        <el-table-column label="评分维度" width="110">
          <template #default="{ row }">
            <span v-if="row.dimensions.length">{{ row.dimensions.length }} 个</span>
            <el-tag v-else size="small" type="warning">未配置</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="bid_count" label="已投" width="70" />
        <el-table-column label="我的状态" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.has_bid" type="success" size="small">已投标</el-tag>
            <el-tag v-else type="info" size="small" effect="plain">可投标</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="110" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click.stop="goLot(row)">标段详情</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无可投标的标段">
            <p class="empty-guide">处于投标阶段（BIDDING）的标段会出现在市场。<br>可调整筛选条件，或等待新的项目发布标段。</p>
          </el-empty>
        </template>
      </el-table>
      <el-pagination
        class="pager"
        layout="total, prev, pager, next"
        :total="total"
        :page-size="query.page_size"
        :current-page="query.page"
        @current-change="onPage"
      />
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { getMarket } from '../../api/suppliers'

const router = useRouter()
const loading = ref(false)
const items = ref([])
const total = ref(0)
const filters = reactive({ types: [], regions: [] })
const query = reactive({
  project_type: '',
  region: '',
  budget_min: undefined,
  budget_max: undefined,
  page: 1,
  page_size: 20,
})

const TYPE_LABEL = { SERVICE: '服务类', ENGINEERING: '工程类' }
const typeLabel = (t) => TYPE_LABEL[t] || t || '-'

// 预算单位换算：元 → 万元
function fmtWan(v) {
  if (v == null) return '-'
  return `${(v / 10000).toFixed(1)} 万`
}

async function load() {
  loading.value = true
  try {
    const data = await getMarket({
      project_type: query.project_type || undefined,
      region: query.region || undefined,
      budget_min: query.budget_min ?? undefined,
      budget_max: query.budget_max ?? undefined,
      page: query.page,
      page_size: query.page_size,
    })
    items.value = data.items
    total.value = data.total
    filters.types = data.filters.types
    filters.regions = data.filters.regions
  } finally {
    loading.value = false
  }
}

function onSearch() {
  query.page = 1
  load()
}

function onReset() {
  Object.assign(query, { project_type: '', region: '', budget_min: undefined, budget_max: undefined, page: 1 })
  load()
}

function onPage(p) {
  query.page = p
  load()
}

function goLot(row) {
  router.push(`/supplier/lots/${row.lot_id}`)
}

function goProject(row) {
  router.push(`/supplier/projects/${row.project_id}`)
}

onMounted(load)
</script>

<style scoped>
.filter-card {
  margin-bottom: 16px;
}
.range-sep {
  margin: 0 6px;
  color: var(--el-text-color-secondary);
}
.card-header {
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
</style>
