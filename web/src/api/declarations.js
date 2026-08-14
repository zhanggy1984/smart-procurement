import client from './client'

// 专家端：AI 可用状态探测（P6.6 降级 UI）
export function aiStatus() {
  return client.get('/reviews/ai-status')
}

// 专家端：我的任务 / 回避申报 / 评审（P6.4）
export function myAssignments() {
  return client.get('/experts/me/assignments')
}

export function getDeclaration(assignmentId) {
  return client.get(`/experts/assignments/${assignmentId}/declaration`)
}

export function declare(assignmentId, confirmations) {
  return client.post(`/experts/assignments/${assignmentId}/declare`, { confirmations })
}

export function createReview(bidId, dimensionId) {
  return client.post('/reviews', { bid_id: bidId, dimension_id: dimensionId })
}

export function saveScore(reviewId, data) {
  return client.put(`/reviews/${reviewId}/score`, data)
}

export function submitReview(reviewId) {
  return client.post(`/reviews/${reviewId}/submit`)
}

// 追问 AI（SSE 流式，返回原始 Response 由组件逐帧解析）
export function chatReview(reviewId, question) {
  const token = localStorage.getItem('sp_token')
  return fetch(`/api/v1/reviews/${reviewId}/chat`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ question }),
  })
}
