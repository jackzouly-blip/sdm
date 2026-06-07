import { defineStore } from "pinia";
import { ref } from "vue";
import { api } from "@/api";
import { getToken, setToken } from "@/api/client";
import type { MeResponse } from "@/api/types";

export const useAuthStore = defineStore("auth", () => {
  const token = ref<string | null>(getToken());
  const me = ref<MeResponse | null>(null);

  function isAuthed(): boolean {
    return !!token.value;
  }

  async function login(username: string, password: string): Promise<void> {
    const resp = await api.login(username, password);
    token.value = resp.token;
    setToken(resp.token);
    me.value = await api.me();
  }

  // 刷新页面后用已存 token 拉取当前用户，验证有效性。
  async function refreshMe(): Promise<void> {
    if (!token.value) return;
    me.value = await api.me();
  }

  function logout(): void {
    token.value = null;
    me.value = null;
    setToken(null);
  }

  return { token, me, isAuthed, login, refreshMe, logout };
});
