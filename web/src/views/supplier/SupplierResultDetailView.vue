<template>
  <div>
    <el-page-header class="page-head" @back="router.push('/supplier/bids')">
      <template #content>
        <span class="page-title">投标结果 - {{ item?.lot_name }}</span>
      </template>
    </el-page-header>

    <template v-if="item">
      <!-- 已中标 -->
      <el-result v-if="item.result_status === 'WINNER'" icon="success" title="恭喜中标">
        <template #sub-title>
          <p class="result-text">贵司在「{{ item.lot_name }}」评标中以综合得分 {{ item.weighted_total }} 分位列第 1 名，成为中标候选人。</p>
        </template>
        <template #extra>
          <div class="next-steps">
            <el-alert type="success" :closable="false" show-icon>
              <template #title>后续步骤</template>
              <ol class="step-list">
                <li>等待采购方发出中标通知书与合同文本。</li>
                <li>按合同约定提交履约保证金及授权委托材料。</li>
                <li>与采购方约定合同签订时间，完成签约备案。</li>
              </ol>
            </el-alert>
          </div>
        </template>
      </el-result>

      <!-- 未中标 -->
      <el-result v-else-if="item.result_status === 'LOSER'" icon="warning" title="未中标">
        <template #sub-title>
          <p class="result-text">
            贵司综合得分 {{ item.weighted_total }} 分，排名第 {{ item.rank }} 名；中标方 {{ item.winner_supplier_name }}（{{ item.winner_weighted_total }} 分）。
            本次报价 {{ fmtWan(item.bid_amount) }}。
          </p>
        </template>
      </el-result>

      <!-- 评审中 / 废标 -->
      <el-alert
        v-else-if="item.result_status === 'UNDER_REVIEW'"
        type="info" :closable="false" show-icon
        title="评审进行中"
        description="投标已提交，评审尚未结束。结果公布后将在此展示排名与各维度得分。"
        class="status-alert"
      />
      <el-alert
        v-else
        type="error" :closable="false" show-icon
        title="标书已废标"
        description="该投标因供应商状态异常已被取消资格。如需了解原因请联系采购方。"
        class="status-alert"
      />

      <!-- 得分明细（未中标：各维度得分 + 落标原因） -->
      <el-card v-if="item.result_status === 'LOSER' && item.dimension_scores.length" shadow="never" class="score-card">
        <template #header>
          <div class="card-header">各维度得分</div>
        </template>
        <el-table :data="item.dimension_scores">
          <el-table-column prop="name" label="维度" min-width="150" />
          <el-table-column label="得分 / 满分" width="130">
            <template #default="{ row }">
              <span :class="{ weak: isWeak(row) }">{{ row.score ?? '-' }} / {{ row.max_score }}</span>
            </template>
          </el-table-column>
          <el-table-column label="权重" width="90">
            <template #default="{ row }">{{ (row.weight * 100).toFixed(1) }}%</template>
          </el-table-column>
          <el-table-column label="得分率" width="100">
            <template #default="{ row }">
              <span v-if="row.score != null">{{ ((row.score / row.max_score) * 100).toFixed(1) }}%</span>
              <span v-else class="muted">未出分</span>
            </template>
          </el-table-column>
        </el-table>
        <el-alert class="gap-alert" type="warning" :closable="false">
          <template #title>落标原因分析</template>
          <p class="gap-text">{{ gapReason }}</p>
        </el-alert>
      </el-card>

      <!-- 质疑入口（预留） -->
      <el-card shadow="never" class="query-card">
        <template #header>
          <div class="card-header">对结果有疑问？</div>
        </template>
        <div class="query-body">
          <p class="query-text">
            如对评审结果存在异议，可提交书面质疑。质疑受理时限与程序以采购文件约定为准。
          </p>
          <el-button type="warning" plain disabled>提交质疑（功能建设中）</el-button>
        </div>
      </el-card>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getMyResults } from '../../api/suppliers'

const route = useRoute()
const router = useRouter()
const item = ref(null)

// 落标原因：与第一名综合得分差距 + 得分率最低的薄弱维度
const gapReason = computed(() => {
  const it = item.value
  if (!it) return ''
  const gap = it.winner_weighted_total != null && it.weighted_total != null
    ? it.winner_weighted_total - it.weighted_total
    : null
  const weak = [...(it.dimension_scores || [])]
    .filter((d) => d.score != null && d.max_score)
    .sort((a, b) => a.score / a.max_score - b.score / b.max_score)[0]
  const parts = []
  if (gap != null && gap > 0) parts.push(`综合得分较第一名低 ${gap.toFixed(2)} 分`)
  if (weak) parts.push(`「${weak.name}」维度得分率最低（${((weak.score / weak.max_score) * 100).toFixed(1)}%）`)
  return parts.length ? parts.join('；') + '，建议在下轮投标中针对性提升。' : '综合表现与中标方接近，建议优化报价与方案细节。'
})

function isWeak(row) {
  return row.score != null && row.max_score && row.score / row.max_score < 0.7
}

function fmtWan(v) {
  if (v == null) return '-'
  return `${(v / 10000).toFixed(1)} 万`
}

async function load() {
  const data = await getMyResults()
  item.value = data.items.find((i) => i.bid_id === route.params.bidId) || null
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
.result-text {
  color: var(--el-text-color-regular);
}
.next-steps {
  text-align: left;
  max-width: 560px;
  margin: 0 auto;
}
.step-list {
  padding-left: 20px;
  line-height: 2;
}
.status-alert {
  margin-bottom: 16px;
}
.score-card {
  margin-top: 16px;
}
.card-header {
  font-weight: 600;
}
.weak {
  color: var(--el-color-danger);
  font-weight: 600;
}
.muted {
  color: var(--el-text-color-placeholder);
}
.gap-alert {
  margin-top: 14px;
}
.gap-text {
  margin: 0;
}
.query-card {
  margin-top: 16px;
}
.query-body {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.query-text {
  margin: 0;
  color: var(--el-text-color-regular);
}
</style>
