<script setup lang="ts">
import { ref } from "vue";
import { useRouter, useRoute } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import { errMsg } from "@/api";
import { LogIn, Loader2 } from "lucide-vue-next";

const auth = useAuthStore();
const router = useRouter();
const route = useRoute();

const username = ref("");
const password = ref("");
const loading = ref(false);
const error = ref("");

async function submit() {
  error.value = "";
  if (!username.value || !password.value) {
    error.value = "请输入用户名和密码";
    return;
  }
  loading.value = true;
  try {
    await auth.login(username.value, password.value);
    const redirect = (route.query.redirect as string) || "/jobs";
    router.push(redirect);
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="min-h-screen flex items-center justify-center bg-slate-50">
    <form
      class="w-80 bg-white rounded-xl shadow-sm border border-slate-200 p-7 flex flex-col gap-4"
      @submit.prevent="submit"
    >
      <div class="text-center">
        <img src="/cherish-logo.png" alt="CHERISH" class="h-20 w-auto mx-auto mb-3" />
        <div class="text-xl font-semibold text-slate-800">驰越诗软件计算平台V3.0</div>
        <div class="text-sm text-slate-500 mt-1">使用系统账号登录</div>
      </div>

      <label class="block">
        <span class="text-sm text-slate-600">用户名</span>
        <input
          v-model="username"
          type="text"
          autocomplete="username"
          class="mt-1 w-full px-3 py-2 rounded-md border border-slate-300 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none"
        />
      </label>

      <label class="block">
        <span class="text-sm text-slate-600">密码</span>
        <input
          v-model="password"
          type="password"
          autocomplete="current-password"
          class="mt-1 w-full px-3 py-2 rounded-md border border-slate-300 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none"
        />
      </label>

      <p v-if="error" class="text-sm text-rose-600">{{ error }}</p>

      <button
        type="submit"
        :disabled="loading"
        class="mt-1 w-full py-2 rounded-md bg-blue-600 text-white font-medium hover:bg-blue-700 disabled:opacity-60 flex items-center justify-center gap-2"
      >
        <Loader2 v-if="loading" :size="16" class="animate-spin" />
        <LogIn v-else :size="16" />
        登录
      </button>
    </form>
  </div>
</template>
