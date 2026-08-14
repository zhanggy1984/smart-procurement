import client from './client'

// 评标汇总 / 收尾相关（P6.3 评标汇总页）
export function getLotSummary(lotId) {
  return client.get(`/lots/${lotId}/summary`)
}

export function completeReview(lotId) {
  return client.post(`/lots/${lotId}/complete-review`)
}

// PDF 下载：blob 响应（拦截器解包 data 后仍是 Blob）
export function downloadReport(lotId) {
  return client.get(`/lots/${lotId}/summary/report`, { responseType: 'blob' })
}

// 推送定标（项目下全部标段终态 → AWARDED + 归档）
export function submitForAward(projectId) {
  return client.post(`/projects/${projectId}/submit-for-award`)
}
