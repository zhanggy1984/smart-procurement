<template>
  <el-card shadow="never">
    <!-- P6.6 降级 Banner：AI 不可用时顶部红色提示 + 恢复后切回按钮 -->
    <el-alert
      v-if="aiDegraded"
      type="error"
      :closable="false"
      show-icon
      class="degrade-banner"
    >
      <template #title>
        <div class="degrade-title">
          <span>AI 辅助评分暂不可用，已切换为纯人工评审模式</span>
          <el-button
            v-if="aiRecovered"
            type="primary"
            size="small"
            class="switch-back-btn"
            @click="switchBackToAi"
          >切换回 AI 辅助模式</el-button>
        </div>
      </template>
      <div class="degrade-desc">请对照左侧标书原文与评分标准手动打分并填写评语，评分与提交不受影响。AI 恢复后可使用右上按钮切回 AI 辅助模式。</div>
    </el-alert>

    <!-- 顶部上下文 -->
    <div class="wb-head">
      <div class="ctx">
        <el-tag size="small" effect="plain" class="ctx-lot">{{ lotName }}</el-tag>
        <span class="ctx-supplier">{{ supplierName }}</span>
        <span class="ctx-dim">{{ dimensionName }}</span>
        <span class="ctx-max">满分 {{ maxScore }} 分</span>
      </div>
      <el-tag v-if="locked" type="success" effect="dark" size="small">评审已提交锁定（{{ status }}）</el-tag>
    </div>

    <div class="wb-body">
      <!-- ========== 左栏：标书内容 ========== -->
      <div class="wb-left">
        <div class="panel-title">标书内容</div>
        <div v-loading="loadingContent" class="left-scroll">
          <template v-if="content.bid_id">
            <div class="struct-card">
              <div class="struct-row"><span class="k">报价</span><span class="v">{{ fmtWan(content.bid_amount) }} 万元</span></div>
              <div class="struct-row"><span class="k">工期</span><span class="v">{{ content.duration ?? '-' }} 个日历天</span></div>
              <div class="struct-row"><span class="k">团队</span><span class="v">{{ content.team_size ?? '-' }} 人</span></div>
              <div class="struct-row"><span class="k">资质</span><span class="v">{{ content.structured_data?.quality_cert || '-' }}</span></div>
              <div class="struct-row"><span class="k">质保</span><span class="v">{{ content.structured_data?.warranty_months ?? '-' }} 个月</span></div>
            </div>
            <div v-if="content.chunks.length" class="chunk-list">
              <div v-for="c in content.chunks" :key="c.chunk_id" class="chunk-item">
                <div class="chunk-head">
                  {{ c.chapter_title }}
                  <span v-if="fmtPages(c.page_range)" class="cite-pages">{{ fmtPages(c.page_range) }}</span>
                </div>
                <div class="chunk-text">{{ c.content }}</div>
              </div>
            </div>
            <el-alert v-else type="warning" :closable="false" title="标书正文未入库（演示数据降级），仅可查看结构化信息" />
          </template>
          <el-empty v-else description="无标书内容" :image-size="60" />
        </div>
      </div>

      <!-- ========== 中栏：评分标准 + AI 建议 + 追问对话 + 打分 ========== -->
      <div class="wb-mid">
        <!-- 评分标准 -->
        <div class="panel-title">评分标准</div>
        <div v-loading="loadingDims" class="rubric-box">
          <ul v-if="currentDim.criteria?.length" class="rubric-list">
            <li v-for="c in currentDim.criteria" :key="c.criterion_id" class="rubric-item">
              <span class="rubric-name">{{ c.name }}</span>
              <span class="rubric-score">（{{ c.max_score }} 分）</span>
              <div class="rubric-desc">{{ c.scoring_rubric || c.description || '—' }}</div>
            </li>
          </ul>
          <div v-else class="rubric-empty">无评分标准子项（AI 将按维度描述综合评分）</div>
        </div>

        <template v-if="!aiDegraded">
        <!-- AI 建议 -->
        <div class="panel-title">
          AI 评分建议
          <el-button
            type="primary"
            size="small"
            :loading="aiLoading"
            :disabled="locked || (reviewId === '' && creating)"
            style="margin-left: 8px"
            @click="aiScore"
          >{{ aiLoading ? 'AI 评分中…' : 'AI 辅助评分' }}</el-button>
        </div>
        <div class="ai-box">
          <el-alert
            v-if="priceCalc"
            type="success"
            :closable="false"
            :title="`报价公式：${priceCalc.formula}`"
          />
          <el-alert
            v-else-if="noEvidence"
            type="warning"
            :closable="false"
            title="未检索到标书依据，AI 无法评分，请人工评审"
          />
          <pre v-if="aiText" class="ai-pre">{{ aiText }}</pre>
          <div v-if="!aiText && !priceCalc && !noEvidence" class="ai-empty">
            {{ locked ? '评审已提交' : '点击「AI 辅助评分」获取打分建议（报价维度走公式实时出分）' }}
          </div>
        </div>
        </template>

        <template v-if="!aiDegraded">
        <!-- 追问对话 -->
        <div class="panel-title">追问 AI</div>
        <div class="chat-box">
          <div class="chat-list">
            <template v-if="messages.length">
              <div v-for="(m, i) in messages" :key="i" class="chat-msg" :class="m.role">
                <span class="chat-role">{{ m.role === 'user' ? '我' : 'AI' }}</span>
                <pre class="chat-content">{{ m.content }}</pre>
              </div>
            </template>
            <div v-else class="chat-empty">对 AI 的评分理由或标书内容有疑问，可在此追问（如「安全方案为什么给低分？展开说明」）</div>
          </div>
          <div class="chat-input">
            <el-input
              v-model="question"
              placeholder="输入问题追问 AI…"
              :disabled="chatLoading || locked"
              @keyup.enter="sendChat"
            />
            <el-button type="primary" :loading="chatLoading" :disabled="locked || !question.trim()" @click="sendChat">发送</el-button>
          </div>
        </div>
        </template>

        <!-- 人工打分 -->
        <div class="panel-title">
          人工打分
          <el-tag v-if="aiDegraded" type="warning" size="small" style="margin-left: 8px">纯人工模式</el-tag>
        </div>
        <div class="score-box">
          <div class="form-row">
            <span class="field-label">得分</span>
            <el-input-number v-model="score" :max="Number(maxScore)" :precision="1" :step="0.5" :disabled="locked" />
            <span class="max-hint">/ {{ maxScore }}</span>
          </div>
          <div class="form-row">
            <span class="field-label">评语</span>
            <el-input v-model="comment" type="textarea" :rows="3" placeholder="填写评审依据与结论（逐条对照评分标准）" :disabled="locked" />
          </div>
          <div class="actions">
            <el-button :disabled="locked || !reviewId" :loading="saving" @click="saveDraft">保存草稿</el-button>
            <el-button type="primary" :disabled="locked || !reviewId || score == null" :loading="submitting" @click="submit">提交评审</el-button>
            <el-button v-if="locked" @click="$router.push('/expert/tasks')">返回任务列表</el-button>
          </div>
        </div>
      </div>

      <!-- ========== 右栏：证据溯源（AI 降级时隐藏） ========== -->
      <div v-if="!aiDegraded" class="wb-right">
        <div class="panel-title">证据溯源</div>
        <div class="right-scroll">
          <div v-if="citations.length" class="cite-list">
            <div v-for="(cit, i) in citations" :key="i" class="cite-item">
              <div class="cite-head">
                引用「{{ cit.chapter_title }}」
                <span v-if="fmtPages(cit.page_range)" class="cite-pages">{{ fmtPages(cit.page_range) }}</span>
              </div>
              <div class="cite-text">{{ cit.content }}</div>
            </div>
          </div>
          <div v-else class="cite-empty">
            <p>点击「AI 辅助评分」后，AI 引用的标书原文将在此展示，供核对打分依据。</p>
            <p class="cite-hint">评分可溯源：每个引用指向标书对应章节原文。</p>
          </div>
        </div>
      </div>
    </div>
  </el-card>
</template>

<script setup>
import { onMounted, onUnmounted, ref, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { aiStatus, createReview, saveScore, submitReview, chatReview } from '../../api/declarations'
import { getBidContent } from '../../api/bids'
import { listLotDimensions } from '../../api/projects'

const route = useRoute()
const router = useRouter()

const bidId = route.query.bid_id || ''
const dimensionId = route.query.dimension_id || ''
const supplierName = route.query.supplier_name || ''
const dimensionName = route.query.dimension_name || ''
const lotName = route.query.lot_name || ''
const maxScore = Number(route.query.max_score || 0)

const reviewId = ref(route.query.review_id || '')
const creating = ref(false)
const locked = ref(false)
const status = ref('')

// 左栏：标书内容
const content = ref({ chunks: [], structured_data: null, bid_amount: null, duration: null, team_size: null })
const loadingContent = ref(false)
// 中栏：评分标准
const dimensions = ref([])
const loadingDims = ref(false)
const currentDim = computed(() => dimensions.value.find((d) => d.dimension_id === dimensionId) || {})
// 中栏：AI 建议
const aiLoading = ref(false)
const aiText = ref('')
const priceCalc = ref(null)
const noEvidence = ref(false)
// 中栏：追问对话
const messages = ref([])
const question = ref('')
const chatLoading = ref(false)
// 右栏：证据溯源
const citations = ref([])

// P6.6 降级态：AI 不可用 → Banner + 纯人工评分；恢复后轮询亮起切回按钮
const aiDegraded = ref(false)
const aiRecovered = ref(false)
let aiPollTimer = null

const score = ref(null)
const comment = ref('')
const saving = ref(false)
const submitting = ref(false)

function fmtWan(v) {
  if (v == null) return '-'
  return (Number(v) / 10000).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

// 页码格式化（P8.2）：[n,n] 单页「第 n 页」、[a,b] 跨页「第 a-b 页」、[0,0] 无页码返回空串
function fmtPages(r) {
  if (!Array.isArray(r) || r.length < 2 || r[1] <= 0) return ''
  return r[0] === r[1] ? `第 ${r[0]} 页` : `第 ${r[0]}-${r[1]} 页`
}

async function ensureReview() {
  if (reviewId.value) return reviewId.value
  if (creating.value) return null
  creating.value = true
  try {
    const r = await createReview(bidId, dimensionId)
    reviewId.value = r.review_id
    status.value = r.status
    return r.review_id
  } finally {
    creating.value = false
  }
}

function parseSseBlock(block) {
  const frames = []
  const parts = block.split('\n\n')
  for (const part of parts) {
    let event = 'message'
    let id = null
    const dataLines = []
    for (const line of part.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
      else if (line.startsWith('id:')) id = Number(line.slice(3).trim())
    }
    if (dataLines.length) {
      try {
        frames.push({ event, data: JSON.parse(dataLines.join('\n')), id })
      } catch {
        frames.push({ event, data: dataLines.join('\n'), id })
      }
    }
  }
  return frames
}

async function streamSse(resp, onEvent) {
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let lastId = 0
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let idx
    while ((idx = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, idx)
      buffer = buffer.slice(idx + 2)
      for (const f of parseSseBlock(block)) {
        if (f.id != null) lastId = Math.max(lastId, f.id)
        onEvent(f)
      }
    }
  }
  if (buffer.trim()) {
    for (const f of parseSseBlock(buffer)) {
      if (f.id != null) lastId = Math.max(lastId, f.id)
      onEvent(f)
    }
  }
  // 末帧 seq：断流重连的 Last-Event-ID 依据
  return lastId
}

function handleScoreEvent(f) {
  if (f.event === 'thought' && f.data.delta) {
    aiText.value += f.data.delta
  } else if (f.event === 'price_calc') {
    priceCalc.value = f.data
    aiText.value = f.data.formula
    if (f.data.result?.calculatedScore != null) score.value = Number(f.data.result.calculatedScore)
  } else if (f.event === 'score') {
    if (f.data.score != null) score.value = Number(f.data.score)
    if (f.data.comment) comment.value = f.data.comment
    aiText.value += `\n\n【AI 建议得分】${f.data.score ?? '-'}`
  } else if (f.event === 'source') {
    // 右侧证据溯源：收集引用原文
    if (f.data.content) {
      citations.value.push({
        chapter_title: f.data.chapter_title || '',
        content: f.data.content,
        page_range: f.data.page_range || [0, 0],
      })
    }
  } else if (f.event === 'thinking') {
    const stage = f.data.stage
    if (stage === 'NO_EVIDENCE') noEvidence.value = true
    else if (stage !== 'PRICE_CALC') aiText.value += `\n[${stage}] `
  } else if (f.event === 'error') {
    aiText.value += `\n\n【评分中断】${f.data.detail || '请重试'}`
    ElMessage.error(f.data.detail || '评分中断，请重试')
  } else if (f.event === 'reset') {
    aiText.value = '[SSE 缓存过期，请重新评分]'
  }
}

async function aiScore() {
  const rid = await ensureReview()
  if (!rid) return
  aiText.value = ''
  priceCalc.value = null
  noEvidence.value = false
  citations.value = []
  aiLoading.value = true
  try {
    let lastId = 0
    // 断流重连：最多 3 次；lastId>0 且未收尾帧（done/error/reset）→ 按 Last-Event-ID
    // 走 Redis 缓存续推（后端不发新 LLM），否则全量重拉会重复耗 LLM
    for (let attempt = 0; attempt < 3; attempt++) {
      const token = localStorage.getItem('sp_token')
      const headers = {
        Authorization: `Bearer ${token}`,
        // 每次请求独立 key：幂等检查仅防双击，不参与续推去重
        'X-Idempotency-Key': crypto.randomUUID(),
      }
      if (lastId > 0) headers['Last-Event-ID'] = String(lastId)
      const resp = await fetch(`/api/v1/reviews/${rid}/score`, {
        method: 'POST',
        headers,
      })
      if (!resp.ok) {
        if (resp.status === 503) {
          enterDegrade()
          return
        }
        const body = await resp.json().catch(() => ({}))
        ElMessage.error(body.detail || `评分失败（${resp.status}）`)
        return
      }
      let finished = false
      lastId = await streamSse(resp, (f) => {
        if (f.event === 'done' || f.event === 'error' || f.event === 'reset') finished = true
        handleScoreEvent(f)
      })
      if (finished || lastId === 0) break
    }
  } catch (e) {
    ElMessage.error(e.message || 'AI 评分失败')
  } finally {
    aiLoading.value = false
  }
}

async function sendChat() {
  const q = question.value.trim()
  if (!q) return
  const rid = await ensureReview()
  if (!rid) return
  messages.value.push({ role: 'user', content: q })
  question.value = ''
  chatLoading.value = true
  const aiMsg = { role: 'ai', content: '' }
  messages.value.push(aiMsg)
  try {
    const resp = await chatReview(rid, q)
    if (!resp.ok) {
      // 仅 503（断路器 OPEN）降级纯人工；业务 4xx 是正常错误，提示不降级
      if (resp.status === 503) {
        enterDegrade()
        return
      }
      const body = await resp.json().catch(() => ({}))
      ElMessage.error(body.detail || `对话失败（${resp.status}）`)
      return
    }
    await streamSse(resp, (f) => {
      if (f.event === 'thought' && f.data.delta) aiMsg.content += f.data.delta
      if (f.event === 'error') {
        // 业务/流中断错误：仅提示，不降级 AI（降级只认 503 响应）
        ElMessage.error(f.data.detail || '对话失败')
      }
    })
  } catch (e) {
    // 网络层异常：不降级，提示重试
    ElMessage.error(e.message || '对话失败')
  } finally {
    chatLoading.value = false
  }
}

async function saveDraft() {
  saving.value = true
  try {
    await saveScore(reviewId.value, { score: score.value, comment: comment.value })
    ElMessage.success('草稿已保存')
  } finally {
    saving.value = false
  }
}

async function submit() {
  submitting.value = true
  try {
    await saveScore(reviewId.value, { score: score.value, comment: comment.value })
    const r = await submitReview(reviewId.value)
    locked.value = true
    status.value = r.status
    ElMessage.success('评审已提交锁定')
  } finally {
    submitting.value = false
  }
}

async function loadContent() {
  if (!bidId) return
  loadingContent.value = true
  try {
    content.value = await getBidContent(bidId)
  } finally {
    loadingContent.value = false
  }
}

async function loadDimensions() {
  if (!content.value?.lot_id) return
  loadingDims.value = true
  try {
    const data = await listLotDimensions(content.value.lot_id)
    dimensions.value = data.items || []
  } finally {
    loadingDims.value = false
  }
}

async function checkAiStatus() {
  try {
    const r = await aiStatus()
    if (r.status !== 'available') {
      if (!aiDegraded.value) enterDegrade()
      else aiRecovered.value = false
    } else {
      aiRecovered.value = true
    }
  } catch {
    /* 探测失败不硬切，保持现状 */
  }
}

function startAiPolling() {
  stopAiPolling()
  aiPollTimer = setInterval(checkAiStatus, 15000)
}

function stopAiPolling() {
  if (aiPollTimer) {
    clearInterval(aiPollTimer)
    aiPollTimer = null
  }
}

function enterDegrade() {
  aiDegraded.value = true
  aiRecovered.value = false
  startAiPolling()
}

async function switchBackToAi() {
  try {
    const r = await aiStatus()
    if (r.status !== 'available') {
      aiRecovered.value = false
      ElMessage.warning('AI 仍不可用，请稍后再试')
      return
    }
    aiDegraded.value = false
    aiRecovered.value = false
    stopAiPolling()
    ElMessage.success('已切换回 AI 辅助模式')
  } catch {
    ElMessage.error('AI 状态探测失败，请稍后再试')
  }
}

onMounted(async () => {
  await ensureReview()
  await loadContent()
  await loadDimensions()
  checkAiStatus()
})

onUnmounted(stopAiPolling)
</script>

<style scoped>
.degrade-banner {
  margin-bottom: 14px;
}
.degrade-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  font-weight: 600;
}
.degrade-desc {
  font-size: 13px;
  line-height: 1.6;
}
.switch-back-btn {
  margin-left: 16px;
}
.wb-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
}
.ctx {
  display: flex;
  align-items: center;
  gap: 10px;
}
.ctx-lot {
  font-weight: 600;
}
.ctx-supplier {
  font-weight: 600;
}
.ctx-dim {
  color: var(--el-text-color-secondary);
}
.ctx-max {
  color: var(--el-text-color-placeholder);
}
.wb-body {
  display: flex;
  gap: 14px;
  height: calc(100vh - 210px);
}
.wb-left,
.wb-right {
  width: 340px;
  flex: none;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.left-scroll,
.right-scroll {
  flex: 1;
  overflow: auto;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 10px;
}
.wb-mid {
  flex: 1;
  min-width: 0;
  overflow: auto;
  padding-right: 2px;
}
.panel-title {
  font-weight: 600;
  margin-bottom: 8px;
  display: flex;
  align-items: center;
}
.struct-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  margin-bottom: 10px;
  overflow: hidden;
}
.struct-row {
  display: flex;
  font-size: 13px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.struct-row:last-child {
  border-bottom: none;
}
.struct-row .k {
  width: 56px;
  padding: 6px 10px;
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-lighter);
  flex: none;
}
.struct-row .v {
  padding: 6px 10px;
}
.chunk-item {
  margin-bottom: 10px;
}
.chunk-head {
  font-size: 12px;
  color: var(--el-color-primary);
  font-weight: 600;
  margin-bottom: 3px;
}
.chunk-text {
  font-size: 12px;
  color: var(--el-text-color-regular);
  line-height: 1.6;
  white-space: pre-wrap;
}
.rubric-box,
.ai-box,
.chat-box,
.score-box {
  margin-bottom: 16px;
}
.rubric-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.rubric-item {
  padding: 6px 10px;
  border-left: 3px solid var(--el-color-primary-light-5);
  background: var(--el-fill-color-lighter);
  border-radius: 4px;
  margin-bottom: 6px;
}
.rubric-name {
  font-weight: 600;
  font-size: 13px;
}
.rubric-score {
  color: var(--el-text-color-placeholder);
  font-size: 12px;
}
.rubric-desc {
  font-size: 12px;
  color: var(--el-text-color-regular);
  margin-top: 3px;
  line-height: 1.5;
}
.rubric-empty {
  font-size: 13px;
  color: var(--el-text-color-placeholder);
}
.ai-pre {
  background: var(--el-fill-color-lighter);
  border-radius: 6px;
  padding: 10px;
  font-size: 13px;
  line-height: 1.6;
  max-height: 200px;
  overflow: auto;
  white-space: pre-wrap;
  margin: 8px 0 0;
}
.ai-empty {
  font-size: 13px;
  color: var(--el-text-color-placeholder);
  background: var(--el-fill-color-lighter);
  border-radius: 6px;
  padding: 10px;
}
.chat-list {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  min-height: 120px;
  max-height: 220px;
  overflow: auto;
  padding: 8px;
  margin-bottom: 8px;
}
.chat-msg {
  margin-bottom: 8px;
  display: flex;
  gap: 8px;
}
.chat-msg.ai {
  flex-direction: row-reverse;
}
.chat-role {
  flex: none;
  width: 24px;
  height: 24px;
  border-radius: 4px;
  font-size: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
}
.chat-msg.user .chat-role {
  background: var(--el-color-primary);
}
.chat-msg.ai .chat-role {
  background: var(--el-color-success);
}
.chat-content {
  margin: 0;
  background: var(--el-fill-color-lighter);
  border-radius: 6px;
  padding: 8px;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  max-width: 80%;
}
.chat-empty {
  font-size: 13px;
  color: var(--el-text-color-placeholder);
  text-align: center;
  padding: 20px 10px;
}
.chat-input {
  display: flex;
  gap: 8px;
}
.form-row {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 10px;
}
.field-label {
  width: 48px;
  line-height: 32px;
  color: var(--el-text-color-secondary);
  flex: none;
}
.max-hint {
  line-height: 32px;
  color: var(--el-text-color-placeholder);
}
.actions {
  display: flex;
  gap: 10px;
  align-items: center;
}
.cite-item {
  margin-bottom: 10px;
}
.cite-head {
  font-size: 12px;
  font-weight: 600;
  color: var(--el-color-warning-dark-2);
  margin-bottom: 3px;
}
.cite-pages {
  margin-left: 6px;
  font-size: 11px;
  font-weight: 400;
  color: var(--el-text-color-secondary);
}
.cite-text {
  font-size: 12px;
  color: var(--el-text-color-regular);
  line-height: 1.6;
  border-left: 2px solid var(--el-color-warning-light-5);
  padding-left: 8px;
}
.cite-empty {
  font-size: 13px;
  color: var(--el-text-color-placeholder);
  padding: 10px;
}
.cite-hint {
  color: var(--el-text-color-secondary);
}
</style>
