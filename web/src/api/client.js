import axios from 'axios'
import { ElMessage } from 'element-plus'
import router from '../router'

// 统一 axios 实例：baseURL /api/v1（vite proxy → 后端）
// 请求注入 Bearer token；响应解包 data；401 自动登出；错误统一 toast
const client = axios.create({ baseURL: '/api/v1', timeout: 60000 })

client.interceptors.request.use((config) => {
  const token = localStorage.getItem('sp_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

client.interceptors.response.use(
  (resp) => resp.data,
  (error) => {
    const status = error.response?.status
    if (status === 401) {
      localStorage.removeItem('sp_token')
      localStorage.removeItem('sp_user')
      if (router.currentRoute.value.path !== '/login') {
        router.push('/login')
      }
    }
    const detail = error.response?.data?.detail
    let text = ''
    if (Array.isArray(detail)) {
      text = detail.map((m) => m.msg || JSON.stringify(m)).join('；')
    } else if (typeof detail === 'string') {
      text = detail
    } else {
      text = error.message || '请求失败'
    }
    // SSE 流内错误由调用方处理，不在此全局 toast
    if (!error.config?.sseSilent) {
      ElMessage.error(text)
    }
    return Promise.reject(error)
  },
)

export default client
