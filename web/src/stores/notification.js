import { defineStore } from 'pinia'
import client from '../api/client'

// 站内信通知：顶栏铃铛未读数 + 通知抽屉
export const useNotificationStore = defineStore('notification', {
  state: () => ({ unread: 0, list: [], loading: false }),
  actions: {
    async refreshUnread() {
      const data = await client.get('/notifications/unread-count').catch(() => ({ unread_count: 0 }))
      this.unread = data.unread_count ?? 0
    },
    async list() {
      this.loading = true
      try {
        const data = await client.get('/notifications', { params: { page: 1, page_size: 20 } })
        this.list = data.notifications || []
        this.unread = data.unread_count || 0
      } finally {
        this.loading = false
      }
    },
    async markRead(id) {
      await client.put(`/notifications/${id}/read`)
      this.unread = Math.max(0, this.unread - 1)
      const n = this.list.find((x) => x.id === id)
      if (n) n.is_read = true
    },
    async markAllRead() {
      await client.put('/notifications/read-all')
      this.unread = 0
      this.list.forEach((n) => (n.is_read = true))
    },
  },
})
