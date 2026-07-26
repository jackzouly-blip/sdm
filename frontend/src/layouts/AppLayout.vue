<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { RouterView, RouterLink, useRouter, useRoute } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import { api, errMsg } from "@/api";
import { APPS, appForPath, visibleNav } from "@/apps/registry";
import {
  LogOut,
  Globe,
  Copy,
  Check,
  ExternalLink,
  Menu as MenuIcon,
  X as XIcon,
} from "lucide-vue-next";

// 移动端导航抽屉开关
const mobileOpen = ref(false);

const auth = useAuthStore();
const router = useRouter();

// 服务器当前 IPv6 入口（ISP 前缀变化时自动反映最新地址，免 DDNS）
const ipv6 = ref<string | null>(null);
const copied = ref(false);
// 是否已通过 IPv6 访问（避免在 v6 页面再显示"切换到 v6"）
const onIpv6 = computed(() => !!ipv6.value && window.location.hostname === ipv6.value);
// 用当前协议/端口/路径拼出 IPv6 入口 URL，便于一键切换
const ipv6Url = computed(() => {
  if (!ipv6.value) return "";
  const l = window.location;
  const port = l.port ? `:${l.port}` : "";
  return `${l.protocol}//[${ipv6.value}]${port}${l.pathname}`;
});
async function copyIpv6() {
  if (!ipv6.value) return;
  try {
    await navigator.clipboard.writeText(ipv6.value);
    copied.value = true;
    setTimeout(() => (copied.value = false), 1500);
  } catch {
    /* 剪贴板不可用时忽略 */
  }
}

onMounted(async () => {
  // 刷新后用已存 token 拉一次当前用户；失败则拦截器会跳登录。
  try {
    await auth.refreshMe();
  } catch (e) {
    console.warn("拉取当前用户失败", errMsg(e));
  }
  // 拉取服务器当前 IPv6（失败不影响主流程）
  try {
    ipv6.value = await api.systemIpv6();
  } catch {
    /* 无 v6 或接口不可用时静默 */
  }
});

function logout() {
  auth.logout();
  router.push({ name: "login" });
}

// 当前所在的一级 APP 由路径决定；APP 内导航与切换器都从注册表派生。
const route = useRoute();
const currentApp = computed(() => appForPath(route.path));
// 管理员专属入口在此过滤（后端同样会以 is_admin 强制校验，这里只是不显示入口）。
const nav = computed(() => visibleNav(currentApp.value, !!auth.me?.is_admin));
const appSwitcherOpen = ref(false);

function switchApp(appId: string) {
  appSwitcherOpen.value = false;
  mobileOpen.value = false;
  const app = APPS.find((a) => a.id === appId);
  if (app && app.id !== currentApp.value.id) router.push({ name: app.home });
}
</script>

<template>
  <div class="min-h-screen flex flex-col">
    <header
      class="h-14 shrink-0 bg-white border-b border-slate-200 flex items-center px-3 sm:px-5 gap-3 sm:gap-6"
    >
      <img src="/cherish-logo.png" alt="CHERISH" class="h-9 w-auto shrink-0" />
      <div
        class="font-semibold text-slate-800 text-base sm:text-lg whitespace-nowrap hidden sm:block"
      >
        驰越诗软件计算平台V3.0
      </div>

      <!-- 一级 APP 切换器 -->
      <div class="relative shrink-0">
        <button
          class="flex items-center gap-2 px-2.5 py-1.5 rounded-md border border-slate-200 bg-slate-50 hover:bg-slate-100 text-slate-700 transition"
          @click="appSwitcherOpen = !appSwitcherOpen"
        >
          <component :is="currentApp.icon" :size="16" class="text-blue-600" />
          <span class="text-sm font-medium">{{ currentApp.label }}</span>
          <svg class="w-3 h-3 text-slate-400" viewBox="0 0 12 12" fill="none">
            <path d="M3 4.5 6 7.5 9 4.5" stroke="currentColor" stroke-width="1.5"
              stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </button>
        <!-- 点击遮罩关闭 -->
        <div
          v-if="appSwitcherOpen"
          class="fixed inset-0 z-10"
          @click="appSwitcherOpen = false"
        ></div>
        <div
          v-if="appSwitcherOpen"
          class="absolute left-0 top-full mt-1 z-20 w-64 bg-white border border-slate-200 rounded-lg shadow-lg p-1"
        >
          <button
            v-for="app in APPS"
            :key="app.id"
            class="w-full flex items-start gap-2.5 px-2.5 py-2 rounded-md text-left hover:bg-slate-50 transition"
            :class="app.id === currentApp.id ? 'bg-blue-50' : ''"
            @click="switchApp(app.id)"
          >
            <component
              :is="app.icon"
              :size="18"
              class="mt-0.5 shrink-0"
              :class="app.id === currentApp.id ? 'text-blue-600' : 'text-slate-400'"
            />
            <span class="min-w-0">
              <span
                class="block text-sm font-medium"
                :class="app.id === currentApp.id ? 'text-blue-700' : 'text-slate-700'"
                >{{ app.label }}</span
              >
              <span class="block text-xs text-slate-400">{{ app.hint }}</span>
            </span>
          </button>
        </div>
      </div>

      <!-- 桌面导航（当前 APP 内） -->
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
        <!-- IPv6 入口：显示服务器当前 IPv6，可复制 / 一键切换（前缀变化自动更新） -->
        <div
          v-if="ipv6"
          class="hidden lg:flex items-center gap-1.5 px-2 py-1 rounded-md bg-slate-100 text-xs"
          :title="'服务器当前 IPv6 地址，ISP 前缀变化会自动更新'"
        >
          <Globe :size="14" class="text-blue-600 shrink-0" />
          <span class="font-mono text-slate-700">[{{ ipv6 }}]</span>
          <button
            class="p-0.5 rounded hover:bg-slate-200 text-slate-500"
            :title="copied ? '已复制' : '复制 IPv6 地址'"
            @click="copyIpv6"
          >
            <Check v-if="copied" :size="13" class="text-green-600" />
            <Copy v-else :size="13" />
          </button>
          <a
            v-if="!onIpv6"
            :href="ipv6Url"
            class="p-0.5 rounded hover:bg-slate-200 text-blue-600"
            title="切换到 IPv6 入口（需在该入口重新登录）"
          >
            <ExternalLink :size="13" />
          </a>
        </div>
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
