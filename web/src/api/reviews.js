import client from './client'

// 专家端：评审历史（P6.4）
export function myReviews(params) {
  return client.get('/reviews', { params })
}
