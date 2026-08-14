import client from './client'

export function listExperts(params) {
  return client.get('/experts', { params })
}

export function importExperts(file) {
  const fd = new FormData()
  fd.append('file', file)
  return client.post('/experts/import', fd)
}

export function updateExpertStatus(expertId, status) {
  return client.put(`/experts/${expertId}/status`, { status })
}

export function deleteExpert(expertId) {
  return client.delete(`/experts/${expertId}`)
}
