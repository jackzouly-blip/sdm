import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath, URL } from "node:url";

// 后端默认监听 8000；开发期通过代理转发，避免跨域并复用同源约定。
const BACKEND = process.env.VITE_BACKEND ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      // /api/* 剥掉前缀后转发给后端（后端路由本身不带 /api）。
      "/api": {
        target: BACKEND,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
      // WebSocket：任务进度推送（路径不与前端路由冲突，保持原样）。
      "/ws": { target: BACKEND, ws: true, changeOrigin: true },
    },
  },
});
