<template>
  <el-card shadow="never">
    <div class="toolbar">
      <span class="page-title">回避申报</span>
      <el-select v-model="assignmentId" placeholder="选择待申报任务" style="width: 300px" @change="loadDeclaration">
        <el-option
          v-for="a in pendingAssignments"
          :key="a.assignment_id"
          :label="`${a.lot_name} · ${a.project_name}`"
          :value="a.assignment_id"
        />
      </el-select>
    </div>

    <template v-if="decl">
      <el-alert
        type="info"
        :closable="false"
        title="请对以下投标供应商逐家确认是否存在利害关系（曾任/持股/亲属任职等）。确认存在冲突将申报回避并自动补充匹配专家。"
        style="margin-bottom: 14px"
      />

      <el-table :data="decl.suppliers" border stripe>
        <el-table-column label="供应商 ID" prop="supplier_id" width="160" />
        <el-table-column label="系统检测冲突" min-width="220">
          <template #default="{ row }">
            <el-tag v-if="row.known_conflicts.length" type="danger" size="small">
              {{ row.known_conflicts.join('、') }}
            </el-tag>
            <span v-else class="no-conflict">无</span>
          </template>
        </el-table-column>
        <el-table-column label="本人申报" width="200" align="center">
          <template #default="{ row }">
            <el-switch
              v-model="row.has_conflict"
              active-text="存在冲突"
              inactive-text="无冲突"
              @change="(v) => onConflictChange(row, v)"
            />
          </template>
        </el-table-column>
        <el-table-column v-if="anyConflict" label="冲突关系类型" min-width="180">
          <template #default="{ row }">
            <el-select v-if="row.has_conflict" v-model="row.relation_type" placeholder="选择关系类型">
              <el-option label="曾任公司人员（EMPLOYED_BY）" value="EMPLOYED_BY" />
              <el-option label="持有公司股份（HOLDS_SHARE）" value="HOLDS_SHARE" />
              <el-option label="亲属在公司任职（RELATIVE_EMPLOYED）" value="RELATIVE_EMPLOYED" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column v-if="anyConflict" label="冲突详情" min-width="180">
          <template #default="{ row }">
            <el-input v-if="row.has_conflict" v-model="row.relation_detail" placeholder="如：曾任该公司技术总监" size="small" />
          </template>
        </el-table-column>
      </el-table>

      <div class="actions">
        <el-button type="primary" :loading="submitting" @click="submit">提交回避申报</el-button>
      </div>
    </template>

    <el-empty v-else description="无待申报任务">
      <p class="empty-guide">专家被指派评审标段后需先完成回避申报。<br>新任务会自动出现在顶部下拉框中，也可到「我的任务」查看。</p>
    </el-empty>
  </el-card>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { myAssignments, getDeclaration, declare } from '../../api/declarations'

const assignmentId = ref(null)
const pendingAssignments = ref([])
const decl = ref(null)
const submitting = ref(false)

const anyConflict = computed(() => decl.value?.suppliers.some((s) => s.has_conflict) || false)

async function loadAssignments() {
  const data = await myAssignments()
  pendingAssignments.value = (data.assignments || []).filter(
    (a) => a.status === 'PENDING_DECLARATION',
  )
  const target = routeQueryAssignment() || pendingAssignments.value[0]?.assignment_id
  if (target) {
    assignmentId.value = target
    loadDeclaration()
  }
}

function routeQueryAssignment() {
  return new URLSearchParams(window.location.search).get('assignment_id')
}

async function loadDeclaration() {
  if (!assignmentId.value) return
  const data = await getDeclaration(assignmentId.value)
  // 补充前端状态字段
  data.suppliers = (data.suppliers || []).map((s) => ({
    ...s,
    has_conflict: false,
    relation_type: '',
    relation_detail: '',
  }))
  decl.value = data
}

function onConflictChange(row, val) {
  if (!val) {
    row.relation_type = ''
    row.relation_detail = ''
  }
}

async function submit() {
  submitting.value = true
  try {
    const confirmations = (decl.value.suppliers || []).map((s) => ({
      supplier_id: s.supplier_id,
      has_conflict: s.has_conflict,
      relation_type: s.has_conflict ? s.relation_type : null,
      relation_detail: s.has_conflict ? s.relation_detail : null,
    }))
    const res = await declare(assignmentId.value, confirmations)
    ElMessage.success(res.status === 'IN_PROGRESS' ? '申报完成，无冲突，可进入评审' : '已申报回避')
    decl.value = null
    await loadAssignments()
  } finally {
    submitting.value = false
  }
}

onMounted(loadAssignments)
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
.no-conflict {
  color: var(--el-text-color-placeholder);
}
.actions {
  margin-top: 14px;
}
.empty-guide {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.7;
  margin: 0;
}
</style>
