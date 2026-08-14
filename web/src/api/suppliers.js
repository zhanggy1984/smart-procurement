import client from './client'

export function importSuppliers(file) {
  const fd = new FormData()
  fd.append('file', file)
  return client.post('/suppliers/import', fd)
}

// P7.4：管理端供应商列表（拉黑管理）
export function listSuppliers(params) {
  return client.get('/suppliers', { params })
}

export function updateSupplierStatus(supplierId, body) {
  return client.put(`/suppliers/${supplierId}/status`, body)
}

// ---------- 供应商端自助（P6.5） ----------

// 招标市场（可投标标段，支持类型/地区/预算筛选）
export function getMarket(params) {
  return client.get('/suppliers/me/market', { params })
}

// 我的投标列表（含解析状态）
export function getMyBids(params) {
  return client.get('/suppliers/me/bids', { params })
}

// 我的标书详情（结构化信息 + 解析状态）
export function getMyBidDetail(bidId) {
  return client.get(`/suppliers/me/bids/${bidId}`)
}

// 投标结果（三态：已中标/未中标/评审中）
export function getMyResults() {
  return client.get('/suppliers/me/results')
}

// 上传标书（multipart，带进度回调；progress 为 0~1 小数）
export function uploadBid(lotId, file, onProgress) {
  const fd = new FormData()
  fd.append('file', file)
  return client.post(`/lots/${lotId}/bids`, fd, {
    onUploadProgress: (e) => {
      if (e.total) onProgress(e.loaded / e.total)
    },
  })
}
