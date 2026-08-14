<template>
  <div>
    <el-page-header class="page-head" @back="router.back()">
      <template #content>
        <span class="page-title">{{ lot?.name }}</span>
      </template>
    </el-page-header>

    <el-card shadow="never" v-if="lot" class="lot-card">
      <el-descriptions :column="4" border>
        <el-descriptions-item label="标段编号">{{ lot.lot_code }}</el-descriptions-item>
        <el-descriptions-item label="标段预算">{{ fmtWan(lot.budget) }}</el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag :type="lotStatusType(lot.status)" size="small">{{ lotStatusLabel(lot.status) }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="我的投标">
          <el-tag v-if="myBid" type="success" size="small">已投标</el-tag>
          <el-tag v-else type="info" size="small" effect="plain">未投标</el-tag>
        </el-descriptions-item>
      </el-descriptions>

      <div class="bid-action" v-if="lot.status === 'BIDDING'">
        <el-button
          v-if="!myBid"
          type="primary"
          size="large"
          :disabled="loading"
          @click="router.push(`/supplier/lots/${lot.lot_id}/upload`)"
        >
          参与投标
        </el-button>
        <el-button v-else type="success" size="large" @click="router.push(`/supplier/bids/${myBid.bid_id}`)">
          查看我的标书
        </el-button>
      </div>
      <el-alert v-else type="info" :closable="false" show-icon title="该标段已停止投标，可查看我的投标结果" />
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">评分维度（{{ dimensions.length }}）</div>
      </template>
      <el-table :data="dimensions" v-loading="loading">
        <el-table-column prop="name" label="维度" min-width="140" />
        <el-table-column label="满分" width="90">
          <template #default="{ row }">{{ row.max_score }} 分</template>
        </el-table-column>
        <el-table-column label="权重" width="90">
          <template #default="{ row }">{{ (row.weight * 100).toFixed(1) }}%</template>
        </el-table-column>
        <el-table-column label="评分标准" min-width="260">
          <template #default="{ row }">
            <div v-if="row.criteria && row.criteria.length">
              <div v-for="c in row.criteria" :key="c.criterion_id" class="criterion">
                · {{ c.name }}（{{ c.max_score }} 分）：{{ c.description || c.scoring_rubric || '-' }}
              </div>
            </div>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="该标段尚未配置评分维度">
            <p class="empty-guide">PM 为标段配置评分维度后，专家将据此进行评审。</p>
          </el-empty>
        </template>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getLot } from '../../api/lots'
import { listLotDimensions } from '../../api/projects'
import { getMyBids } from '../../api/suppliers'

const route = useRoute()
const router = useRouter()
const loading = ref(false)
const lot = ref(null)
const dimensions = ref([])
const myBid = ref(null)

const LOT_STATUS_LABEL = { BIDDING: '招标中', UNDER_REVIEW: '评审中', EVALUATED: '已评审', AWARDED: '已定标' }
const LOT_STATUS_TYPE = { BIDDING: 'primary', UNDER_REVIEW: 'warning', EVALUATED: 'success', AWARDED: 'info' }
const lotStatusLabel = (s) => LOT_STATUS_LABEL[s] || s
const lotStatusType = (s) => LOT_STATUS_TYPE[s] || 'info'

function fmtWan(v) {
  if (v == null) return '-'
  return `${(v / 10000).toFixed(1)} 万`
}

async function load() {
  loading.value = true
  try {
    const lotId = route.params.lotId
    lot.value = await getLot(lotId)
    dimensions.value = (await listLotDimensions(lotId)).items || []
    const my = await getMyBids({ page: 1, page_size: 200 })
    myBid.value = my.items.find((b) => b.lot_id === lotId) || null
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
.lot-card {
  margin-bottom: 16px;
}
.bid-action {
  margin-top: 16px;
}
.card-header {
  font-weight: 600;
}
.criterion {
  line-height: 1.7;
  color: var(--el-text-color-regular);
}
.empty-guide {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.7;
  margin: 0;
}
</style>
