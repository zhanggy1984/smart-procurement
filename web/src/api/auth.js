import axios from 'axios'
import { ElMessage } from 'element-plus'
import router from '../router'

// T15：登录/刷新统一走 /api/auth/*（4 家 agent 契约一致），不经过业务 baseURL(/api/v1)
const authClient = axios.create({ baseURL: '/api', timeout: 60000 })

// 与 client.js 行为一致：响应解包 data；401 自动登出；错误统一 toast
authClient.interceptors.response.use(
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
    ElMessage.error(text)
    return Promise.reject(error)
  },
)

export function login(username, password) {
  return authClient.post('/auth/login', { username, password })
}

export function refresh(refreshToken) {
  return authClient.post('/auth/refresh', { refresh_token: refreshToken })
}
