import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 构建产物输出到 kbserver/static（FastAPI StaticFiles 托管 /app）；
// 开发态 /api 代理到本机 kbserver。
export default defineConfig({
  plugins: [vue()],
  // 托管于 /app 子路径（kbserver StaticFiles），资源与路由 base 须一致
  base: '/app/',
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8765',
    },
  },
  build: {
    outDir: '../kbserver/static',
    emptyOutDir: true,
  },
})
