import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发服务器：/api/v1 代理到本地后端（uvicorn 8001，验收端口）
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api/v1': { target: 'http://localhost:8001', changeOrigin: true },
      '/health': { target: 'http://localhost:8001', changeOrigin: true },
    },
  },
})
