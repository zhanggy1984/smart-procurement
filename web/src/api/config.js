import client from './client'

// 系统配置（P6.2）：GET /config 配置项列表 / PUT /config 批量更新
export const getConfig = () => client.get('/config')
export const updateConfig = (items) => client.put('/config', { items })
