import path from "node:path"
import { fileURLToPath } from "node:url"

import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

const rootDir = path.dirname(fileURLToPath(import.meta.url))

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(rootDir, "./src"),
    },
  },
  // 开发服务器只监听本机；/api 转发到本机后端（scripts/preview.ps1 用 YUNSHU_API_URL 指定端口）
  server: {
    host: "127.0.0.1",
    proxy: {
      "/api": process.env.YUNSHU_API_URL ?? "http://127.0.0.1:8000",
      // 4.2 P16：Prometheus 指标导出
      "/metrics": process.env.YUNSHU_API_URL ?? "http://127.0.0.1:8000",
      "/mcp": process.env.YUNSHU_API_URL ?? "http://127.0.0.1:8000",
    },
  },
})
