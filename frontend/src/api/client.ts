import axios, { type AxiosInstance } from "axios";

const TOKEN_KEY = "hpc_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

// 所有 API 走 /api 前缀，避免与前端路由（/jobs、/files、/tasks）同名冲突。
// 开发期由 vite 代理剥掉 /api 转发，生产由 nginx 同源反代。
export const http: AxiosInstance = axios.create({ baseURL: "/api" });

http.interceptors.request.use((config) => {
  const token = getToken();
  if (token) {
    config.headers = config.headers ?? {};
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// 401 表示 token 失效：清除并由路由守卫跳回登录页。
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: () => void): void {
  onUnauthorized = fn;
}

http.interceptors.response.use(
  (resp) => resp,
  (error) => {
    if (error.response?.status === 401) {
      setToken(null);
      onUnauthorized?.();
    }
    return Promise.reject(error);
  }
);

// 从 axios 错误中提取后端 detail 文案，便于 UI 直接展示。
export function errMsg(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail;
    if (typeof detail === "string") return detail;
    return error.message;
  }
  return error instanceof Error ? error.message : String(error);
}
