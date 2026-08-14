import client from './client'

export function listLots(params) {
  return client.get('/lots', { params })
}

export function getLot(lotId) {
  return client.get(`/lots/${lotId}`)
}

export function closeBidding(lotId) {
  return client.post(`/lots/${lotId}/close-bidding`)
}

export function getLotReviews(lotId) {
  return client.get(`/lots/${lotId}/reviews`)
}

export function matchExperts(lotId, tags) {
  return client.post(`/lots/${lotId}/match-experts`, { tags })
}

// P7.4：围串标初筛待办闭环 —— PM 确认放行（深度检测） + 废标
export function confirmPrescreen(lotId) {
  return client.post(`/lots/${lotId}/confirm-prescreen`)
}

export function disqualifyBid(lotId, bidId) {
  return client.post(`/lots/${lotId}/bids/${bidId}/disqualify`)
}
