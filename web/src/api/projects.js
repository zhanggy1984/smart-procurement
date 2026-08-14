import client from './client'

export function listProjects(params) {
  return client.get('/projects', { params })
}

export function getProject(projectId) {
  return client.get(`/projects/${projectId}`)
}

export function createProject(data) {
  return client.post('/projects', data)
}

export function createLot(projectId, data) {
  return client.post(`/projects/${projectId}/lots`, data)
}

// 标段评分维度（含评分标准子项）
export function listLotDimensions(lotId) {
  return client.get(`/lots/${lotId}/dimensions`)
}

// P7.4：保存评分维度配置（PM 端配置入口）
export function saveLotDimensions(lotId, dimensions) {
  return client.post(`/lots/${lotId}/dimensions`, { dimensions })
}
