import { defineStore } from 'pinia'
import { login as apiLogin } from '../api/auth'

// 登录态：token + 用户信息持久化到 localStorage（SPA 刷新保持）
const ROLE_HOME = {
  ADMIN: '/admin',
  PROJECT_MANAGER: '/pm',
  REVIEW_EXPERT: '/expert',
  SUPPLIER: '/supplier',
}

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem('sp_token') || '',
    user: JSON.parse(localStorage.getItem('sp_user') || 'null'),
  }),
  getters: {
    isLoggedIn: (s) => !!s.token,
    role: (s) => s.user?.role || '',
    displayName: (s) => s.user?.display_name || '',
  },
  actions: {
    async login(username, password) {
      const data = await apiLogin(username, password)
      this.token = data.access_token
      this.user = data.user
      localStorage.setItem('sp_token', data.access_token)
      localStorage.setItem('sp_user', JSON.stringify(data.user))
      return this.homePath()
    },
    homePath() {
      return ROLE_HOME[this.role] || '/login'
    },
    logout() {
      this.token = ''
      this.user = null
      localStorage.removeItem('sp_token')
      localStorage.removeItem('sp_user')
    },
  },
})
