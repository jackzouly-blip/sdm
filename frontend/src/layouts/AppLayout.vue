<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { RouterView, RouterLink, useRouter } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import { errMsg } from "@/api";
import {
  ListTodo,
  FolderTree,
  UploadCloud,
  FileCog,
  FileCode,
  TerminalSquare,
  BarChart3,
  LogOut,
  Menu as MenuIcon,
  X as XIcon,
} from "lucide-vue-next";

// 移动端导航抽屉开关
const mobileOpen = ref(false);

const auth = useAuthStore();
const router = useRouter();

onMounted(async () => {
  // 刷新后用已存 token 拉一次当前用户；失败则拦截器会跳登录。
  try {
    await auth.refreshMe();
  } catch (e) {
    console.warn("拉取当前用户失败", errMsg(e));
  }
});

function logout() {
  auth.logout();
  router.push({ name: "login" });
}

// 在线 shell 仅管理员可见（后端同样会以 is_admin 强制校验）。
const nav = computed(() => {
  const items = [
    { name: "jobs", label: "任务", icon: ListTodo },
    { name: "files", label: "文件浏览", icon: FolderTree },
    { name: "tasks", label: "打包记录", icon: UploadCloud },
    { name: "rules", label: "提取规则", icon: FileCog },
  ];
  if (auth.me?.is_admin) {
    items.push({ name: "templates", label: "模板管理", icon: FileCode });
    items.push({ name: "stats", label: "机时统计", icon: BarChart3 });
    items.push({ name: "shell", label: "在线终端", icon: TerminalSquare });
  }
  return items;
});
</script>

<template>
  <div class="min-h-screen flex flex-col">
    <header
      class="h-14 shrink-0 bg-white border-b border-slate-200 flex items-center px-3 sm:px-5 gap-3 sm:gap-6"
    >
      <img src="/cherish-logo.png" alt="CHERISH" class="h-9 w-auto shrink-0" />
      <div class="font-semibold text-slate-800 text-base sm:text-lg whitespace-nowrap">
        驰越诗软件计算平台V3.0
      </div>
      <!-- 桌面导航 -->
      <nav class="hidden md:flex items-center gap-1">
        <RouterLink
          v-for="item in nav"
          :key="item.name"
          :to="{ name: item.name }"
          class="px-3 py-1.5 rounded-md text-sm flex items-center gap-1.5 text-slate-600 hover:bg-slate-100 transition"
          active-class="!bg-blue-50 !text-blue-700"
        >
          <component :is="item.icon" :size="16" />
          {{ item.label }}
        </RouterLink>
      </nav>
      <div class="ml-auto flex items-center gap-2 sm:gap-3 text-sm text-slate-600">
        <span v-if="auth.me" class="hidden sm:inline">
          {{ auth.me.username }}
          <span class="text-slate-400">(uid {{ auth.me.uid }})</span>
        </span>
        <button
          class="hidden md:flex items-center gap-1 px-2.5 py-1.5 rounded-md hover:bg-slate-100 text-slate-600"
          @click="logout"
        >
          <LogOut :size="16" /> 退出
        </button>
        <!-- 移动端汉堡按钮 -->
        <button
          class="md:hidden p-2 rounded-md hover:bg-slate-100 text-slate-600"
          @click="mobileOpen = !mobileOpen"
          aria-label="菜单"
        >
          <XIcon v-if="mobileOpen" :size="22" />
          <MenuIcon v-else :size="22" />
        </button>
      </div>
    </header>

    <!-- 移动端下拉导航 -->
    <nav
      v-if="mobileOpen"
      class="md:hidden bg-white border-b border-slate-200 flex flex-col p-2 gap-1 shadow-sm"
    >
      <RouterLink
        v-for="item in nav"
        :key="item.name"
        :to="{ name: item.name }"
        class="px-3 py-3 rounded-md text-sm flex items-center gap-2 text-slate-700 hover:bg-slate-100"
        active-class="!bg-blue-50 !text-blue-700"
        @click="mobileOpen = false"
      >
        <component :is="item.icon" :size="18" />
        {{ item.label }}
      </RouterLink>
      <div class="border-t border-slate-100 my-1"></div>
      <div v-if="auth.me" class="px-3 py-1 text-xs text-slate-400">
        {{ auth.me.username }} (uid {{ auth.me.uid }})
      </div>
      <button
        class="px-3 py-3 rounded-md text-sm flex items-center gap-2 text-slate-700 hover:bg-slate-100 text-left"
        @click="logout"
      >
        <LogOut :size="18" /> 退出登录
      </button>
    </nav>

    <main class="flex-1 p-3 sm:p-5 overflow-auto">
      <RouterView />
    </main>
  </div>
</template>
