import client from './client'

export function listLotBids(lotId, params) {
  return client.get(`/lots/${lotId}/bids`, { params })
}

export function getBid(bidId) {
  return client.get(`/bids/${bidId}`)
}

export function getBidStatus(bidId) {
  return client.get(`/bids/${bidId}/status`)
}

export function retryParse(bidId) {
  return client.post(`/bids/${bidId}/retry-parse`)
}

// 标书内容（结构化字段 + 正文 chunks，评审工作台左栏用）
export function getBidContent(bidId) {
  return client.get(`/bids/${bidId}/content`)
}
