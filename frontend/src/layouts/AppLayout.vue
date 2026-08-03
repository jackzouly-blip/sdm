<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { RouterView, RouterLink, useRouter, useRoute } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import { api, errMsg } from "@/api";
import {
  APPS,
  appForPath,
  flatNav,
  groupContains,
  visibleGroups,
  visibleNav,
} from "@/apps/registry";
import {
  LogOut,
  Globe,
  Copy,
  Check,
  ChevronDown,
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
// 低频/运维页面收进下拉，避免顶栏项数无限增长后挤到逐字换行。
const navGroups = computed(() => visibleGroups(currentApp.value, !!auth.me?.is_admin));
// 移动端抽屉直接铺开：小屏没有"放不下"的问题，分组反而多一层点击。
const mobileNav = computed(() => flatNav(currentApp.value, !!auth.me?.is_admin));
const openGroup = ref<string | null>(null);
const appSwitcherOpen = ref(false);

/**
 * 分组下拉的屏幕坐标。
 *
 * 面板必须 Teleport 到 body 并用 fixed 定位：<nav> 上有 overflow-x-auto，
 * 而浏览器在 overflow-x 非 visible 时会把 overflow-y 也算成 auto，
 * 于是挂在 nav 内部的绝对定位面板会被**整个裁掉**——菜单确实打开了，但看不见。
 */
const menuPos = ref({ left: 0, top: 0 });

function toggleGroup(id: string, ev: MouseEvent) {
  appSwitcherOpen.value = false;
  if (openGroup.value === id) {
    openGroup.value = null;
    return;
  }
  const r = (ev.currentTarget as HTMLElement).getBoundingClientRect();
  menuPos.value = { left: r.left, top: r.bottom + 4 };
  openGroup.value = id;
}

// 面板是 fixed 定位的，页面滚动或窗口变化后位置就不再对齐按钮，直接关掉最省事
function closeGroup() {
  openGroup.value = null;
}
onMounted(() => {
  window.addEventListener("resize", closeGroup);
  window.addEventListener("scroll", closeGroup, true);
});
onUnmounted(() => {
  window.removeEventListener("resize", closeGroup);
  window.removeEventListener("scroll", closeGroup, true);
});

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

      <!-- 桌面导航（当前 APP 内）：胶囊标签，纯文字。
           whitespace-nowrap + shrink-0 是必需的——缺了它宽度不足时文字会逐字换行；
           overflow-x-auto 作兜底，页面再多也只是横向滚动，不会把右侧用户区挤出去。 -->
      <nav class="hidden md:flex items-center gap-1.5 min-w-0 overflow-x-auto nav-scroll">
        <RouterLink
          v-for="item in nav"
          :key="item.name"
          :to="{ name: item.name }"
          class="shrink-0 whitespace-nowrap px-3.5 py-1.5 rounded-full border border-slate-200 bg-white text-sm text-slate-600 hover:bg-slate-50 hover:border-slate-300 transition"
          active-class="!bg-blue-50 !border-blue-300 !text-blue-700"
        >
          {{ item.label }}
        </RouterLink>

        <!-- 分组下拉（如「运维」）：组内有当前页时按钮同样高亮，
             否则用户会以为自己不在任何导航项上 -->
        <div v-for="g in navGroups" :key="g.id" class="shrink-0">
          <button
            class="flex items-center gap-1 whitespace-nowrap px-3.5 py-1.5 rounded-full border text-sm transition"
            :class="
              groupContains(g, route.name as string)
                ? 'bg-blue-50 border-blue-300 text-blue-700'
                : 'bg-slate-50 border-slate-200 text-slate-600 hover:bg-slate-100 hover:border-slate-300'
            "
            @click="toggleGroup(g.id, $event)"
          >
            {{ g.label }}
            <ChevronDown :size="13" class="opacity-60" />
          </button>
          <!-- Teleport 到 body：留在 nav 内会被 overflow-x-auto 连带的
               overflow-y 裁掉，表现为"点了没反应" -->
          <Teleport to="body">
            <template v-if="openGroup === g.id">
              <div class="fixed inset-0 z-40" @click="closeGroup"></div>
              <div
                class="fixed z-50 w-40 bg-white border border-slate-200 rounded-lg shadow-lg p-1"
                :style="{ left: menuPos.left + 'px', top: menuPos.top + 'px' }"
              >
                <RouterLink
                  v-for="item in g.items"
                  :key="item.name"
                  :to="{ name: item.name }"
                  class="block px-3 py-2 rounded-md text-sm text-slate-700 hover:bg-slate-50 transition"
                  active-class="!bg-blue-50 !text-blue-700"
                  @click="closeGroup"
                >
                  {{ item.label }}
                </RouterLink>
              </div>
            </template>
          </Teleport>
        </div>
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
      <!-- 移动端保留图标：竖排列表里图标帮助快速定位，且触摸目标更好点 -->
      <RouterLink
        v-for="item in mobileNav"
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

<style scoped>
/* 顶栏导航的横向滚动只是极窄屏下的兜底，平时不该出现滚动条占位 */
.nav-scroll {
  scrollbar-width: none;
}
.nav-scroll::-webkit-scrollbar {
  display: none;
}
</style>
