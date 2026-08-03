<script setup lang="ts">
import { ref, onMounted, onUnmounted } from "vue";
import { api, errMsg, pollTask } from "@/api";
import type {
  NetdiskShare,
  NetdiskShareFile,
  NetdiskSyncStatus,
  ShareLinkState,
} from "@/api/types";
import { fmtBytes, fmtTime } from "@/lib/format";
import ShareDialog from "@/components/ShareDialog.vue";
import NetdiskCredCard from "@/components/NetdiskCredCard.vue";
import {
  Plus,
  RefreshCw,
  Loader2,
  Inbox,
  Pencil,
  Trash2,
  PlayCircle,
  ChevronDown,
  ChevronRight,
  AlertTriangle,
  CloudDownload,
} from "lucide-vue-next";

const status = ref<NetdiskSyncStatus | null>(null);
const shares = ref<NetdiskShare[]>([]);
const loading = ref(false);
const error = ref("");
const showDialog = ref(false);
const editing = ref<NetdiskShare | null>(null);

// 展开行 → 文件清单
const expanded = ref<number | null>(null);
const files = ref<NetdiskShareFile[]>([]);
const filesLoading = ref(false);

// 正在同步的源 → 进度文案；停止函数用于组件卸载时清理轮询
const progress = ref<Record<number, { phase: string; pct: number }>>({});
const stoppers: Array<() => void> = [];

async function load() {
  loading.value = true;
  error.value = "";
  try {
    shares.value = await api.listShares();
    // 页面刷新后仍在同步的源，重新挂上进度轮询
    for (const s of shares.value) {
      if (s.syncing && s.last_task_id && !progress.value[s.id]) {
        watchTask(s.id, s.last_task_id);
      }
    }
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}

function watchTask(shareId: number, taskId: string) {
  progress.value[shareId] = { phase: "排队中", pct: 0 };
  const stop = pollTask(taskId, (s) => {
    progress.value[shareId] = { phase: s.phase || "同步中", pct: s.progress ?? 0 };
    if (["success", "failed", "interrupted"].includes(s.status)) {
      delete progress.value[shareId];
      void load();
      if (expanded.value === shareId) void openFiles(shareId);
    }
  });
  stoppers.push(stop);
}

async function syncNow(s: NetdiskShare) {
  error.value = "";
  try {
    const { task_id } = await api.syncShare(s.id);
    watchTask(s.id, task_id);
  } catch (e) {
    error.value = errMsg(e);
  }
}

async function openFiles(id: number) {
  filesLoading.value = true;
  try {
    files.value = await api.listShareFiles(id);
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    filesLoading.value = false;
  }
}

async function toggleExpand(s: NetdiskShare) {
  if (expanded.value === s.id) {
    expanded.value = null;
    return;
  }
  expanded.value = s.id;
  await openFiles(s.id);
}

function openNew() {
  editing.value = null;
  showDialog.value = true;
}

function openEdit(s: NetdiskShare) {
  editing.value = s;
  showDialog.value = true;
}

function onSaved() {
  showDialog.value = false;
  void load();
}

async function toggle(s: NetdiskShare) {
  try {
    await api.updateShare(s.id, { enabled: !s.enabled });
    await load();
  } catch (e) {
    error.value = errMsg(e);
  }
}

async function remove(s: NetdiskShare) {
  if (
    !confirm(
      `确定删除共享目录「${s.name}」？\n\n只删除同步配置，已下载到服务器的文件会保留。`
    )
  )
    return;
  try {
    await api.deleteShare(s.id);
    if (expanded.value === s.id) expanded.value = null;
    await load();
  } catch (e) {
    error.value = errMsg(e);
  }
}

function intervalLabel(sec: number): string {
  if (!sec) return "仅手动";
  if (sec % 86400 === 0) return `每 ${sec / 86400} 天`;
  if (sec % 3600 === 0) return `每 ${sec / 3600} 小时`;
  return `每 ${Math.round(sec / 60)} 分钟`;
}

function linkBadge(s: ShareLinkState): { text: string; cls: string } {
  switch (s) {
    case "ok":
      return { text: "链接正常", cls: "bg-emerald-100 text-emerald-700" };
    case "invalid":
      return { text: "链接失效", cls: "bg-rose-100 text-rose-700" };
    case "auth_failed":
      return { text: "平台凭据失效", cls: "bg-orange-100 text-orange-700" };
    default:
      return { text: "未同步过", cls: "bg-slate-100 text-slate-500" };
  }
}

function fileBadge(s: string): { text: string; cls: string } {
  switch (s) {
    case "done":
      return { text: "已同步", cls: "bg-emerald-100 text-emerald-700" };
    case "transferred":
      return { text: "待下载", cls: "bg-blue-100 text-blue-700" };
    case "failed":
      return { text: "失败", cls: "bg-rose-100 text-rose-700" };
    default:
      return { text: "待转存", cls: "bg-slate-100 text-slate-500" };
  }
}

async function refreshStatus() {
  try {
    status.value = await api.netdiskSyncStatus();
  } catch {
    /* 状态拉取失败不阻塞列表 */
  }
}

onMounted(async () => {
  await refreshStatus();
  await load();
});

onUnmounted(() => stoppers.forEach((s) => s()));
</script>

<template>
  <div class="w-full">
    <div class="flex items-center gap-3 mb-4">
      <h1 class="text-lg font-semibold text-slate-800">网盘数据管理</h1>
      <span class="hidden sm:inline text-sm text-slate-400">
        从百度网盘共享目录同步数据到服务器
      </span>
      <div class="ml-auto flex items-center gap-2">
        <button
          class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-60"
          :disabled="!status?.ready"
          @click="openNew"
        >
          <Plus :size="15" /> 添加共享目录
        </button>
        <button
          class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-slate-300 bg-white hover:bg-slate-50"
          :disabled="loading"
          @click="load"
        >
          <RefreshCw :size="15" :class="{ 'animate-spin': loading }" /> 刷新
        </button>
      </div>
    </div>

    <!-- 管理员：凭据配置卡片。普通用户看不到，只会看到下面的提示条 -->
    <NetdiskCredCard
      v-if="status?.is_admin && status.credentials"
      :cred="status.credentials"
      @changed="refreshStatus"
    />

    <div
      v-if="status && !status.ready && !status.is_admin"
      class="flex items-start gap-2 text-sm text-orange-700 bg-orange-50 border border-orange-200 rounded-lg px-4 py-3 mb-4"
    >
      <AlertTriangle :size="16" class="mt-0.5 shrink-0" />
      <span>平台尚未配置网盘凭据，无法使用该功能。请联系运维配置后重试。</span>
    </div>
    <div
      v-else-if="status?.credentials?.state === 'auth_failed' && !status.is_admin"
      class="flex items-start gap-2 text-sm text-rose-700 bg-rose-50 border border-rose-200 rounded-lg px-4 py-3 mb-4"
    >
      <AlertTriangle :size="16" class="mt-0.5 shrink-0" />
      <span>平台网盘凭据已失效，同步暂时不可用。请联系运维更换后重试。</span>
    </div>

    <p v-if="error" class="text-sm text-rose-600 mb-3">{{ error }}</p>

    <div class="bg-white rounded-xl border border-slate-200 overflow-x-auto">
      <table class="w-full text-sm min-w-[860px]">
        <thead class="bg-slate-50 text-slate-500 text-left">
          <tr>
            <th class="px-3 py-2.5 font-medium w-8"></th>
            <th class="px-4 py-2.5 font-medium">名称 / 分享链接</th>
            <th class="px-4 py-2.5 font-medium">服务器落点</th>
            <th class="px-4 py-2.5 font-medium w-28">同步策略</th>
            <th class="px-4 py-2.5 font-medium w-40">状态</th>
            <th class="px-4 py-2.5 font-medium w-36 text-right">操作</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="s in shares" :key="s.id">
            <tr class="border-t border-slate-100 hover:bg-slate-50">
              <td class="px-3 py-2.5">
                <button
                  class="p-1 rounded hover:bg-slate-200 text-slate-400"
                  @click="toggleExpand(s)"
                >
                  <ChevronDown v-if="expanded === s.id" :size="15" />
                  <ChevronRight v-else :size="15" />
                </button>
              </td>
              <td class="px-4 py-2.5">
                <div class="text-slate-800">{{ s.name }}</div>
                <div class="text-xs text-slate-400 truncate max-w-xs">
                  {{ s.share_url }}
                  <span v-if="s.sub_dir" class="font-mono">· {{ s.sub_dir }}</span>
                </div>
              </td>
              <td class="px-4 py-2.5">
                <code class="text-xs text-slate-600 break-all">{{ s.local_dir }}</code>
              </td>
              <td class="px-4 py-2.5">
                <button
                  class="px-2 py-0.5 rounded-full text-xs"
                  :class="
                    s.enabled
                      ? 'bg-blue-50 text-blue-700'
                      : 'bg-slate-100 text-slate-500'
                  "
                  :title="s.enabled ? '点击停用' : '点击启用'"
                  @click="toggle(s)"
                >
                  {{ s.enabled ? intervalLabel(s.poll_interval) : "已停用" }}
                </button>
              </td>
              <td class="px-4 py-2.5">
                <!-- 同步中优先显示进度，比状态徽标更有用 -->
                <div v-if="progress[s.id]" class="flex flex-col gap-1">
                  <div class="flex items-center gap-1.5 text-xs text-blue-700">
                    <Loader2 :size="13" class="animate-spin shrink-0" />
                    <span class="truncate">{{ progress[s.id].phase }}</span>
                  </div>
                  <div class="h-1 rounded-full bg-slate-100 overflow-hidden">
                    <div
                      class="h-full bg-blue-500 transition-all"
                      :style="{ width: `${Math.min(progress[s.id].pct, 100)}%` }"
                    ></div>
                  </div>
                </div>
                <div v-else class="flex flex-col gap-1">
                  <span
                    class="px-2 py-0.5 rounded-full text-xs w-fit"
                    :class="linkBadge(s.link_state).cls"
                  >
                    {{ linkBadge(s.link_state).text }}
                  </span>
                  <span v-if="s.counts" class="text-xs text-slate-400">
                    {{ s.counts.done ?? 0 }} 已同步
                    <span v-if="s.counts.failed" class="text-rose-500">
                      · {{ s.counts.failed }} 失败
                    </span>
                  </span>
                  <span
                    v-if="s.last_error"
                    class="text-xs text-rose-500 truncate max-w-[9rem]"
                    :title="s.last_error"
                  >
                    {{ s.last_error }}
                  </span>
                </div>
              </td>
              <td class="px-4 py-2.5">
                <div class="flex items-center justify-end gap-1">
                  <button
                    class="p-1.5 rounded hover:bg-blue-100 text-blue-600 disabled:opacity-40"
                    title="立即同步"
                    :disabled="!!progress[s.id] || !status?.ready"
                    @click="syncNow(s)"
                  >
                    <PlayCircle :size="16" />
                  </button>
                  <button
                    class="p-1.5 rounded hover:bg-slate-200 text-slate-500"
                    title="编辑"
                    @click="openEdit(s)"
                  >
                    <Pencil :size="15" />
                  </button>
                  <button
                    class="p-1.5 rounded hover:bg-rose-100 text-rose-500"
                    title="删除"
                    @click="remove(s)"
                  >
                    <Trash2 :size="15" />
                  </button>
                </div>
              </td>
            </tr>

            <!-- 展开：文件清单 -->
            <tr v-if="expanded === s.id" class="border-t border-slate-100 bg-slate-50/60">
              <td colspan="6" class="px-4 py-3">
                <div
                  v-if="filesLoading"
                  class="flex items-center gap-2 text-sm text-slate-400 py-4 justify-center"
                >
                  <Loader2 :size="16" class="animate-spin" /> 加载中…
                </div>
                <div v-else-if="!files.length" class="text-sm text-slate-400 py-4 text-center">
                  还没有同步过文件。点击 ▶ 立即同步。
                </div>
                <table v-else class="w-full text-xs">
                  <thead class="text-slate-400 text-left">
                    <tr>
                      <th class="py-1.5 font-medium">文件</th>
                      <th class="py-1.5 font-medium w-24">大小</th>
                      <th class="py-1.5 font-medium w-24">状态</th>
                      <th class="py-1.5 font-medium w-40">更新时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="f in files" :key="f.fs_id" class="border-t border-slate-200/70">
                      <td class="py-1.5 pr-3">
                        <div class="text-slate-700 truncate">{{ f.filename }}</div>
                        <div
                          v-if="f.error"
                          class="text-rose-500 truncate"
                          :title="f.error"
                        >
                          {{ f.error }}
                        </div>
                      </td>
                      <td class="py-1.5 text-slate-500">{{ fmtBytes(f.size) }}</td>
                      <td class="py-1.5">
                        <span
                          class="px-1.5 py-0.5 rounded-full"
                          :class="fileBadge(f.state).cls"
                        >
                          {{ fileBadge(f.state).text }}
                        </span>
                      </td>
                      <td class="py-1.5 text-slate-400">{{ fmtTime(f.updated_at) }}</td>
                    </tr>
                  </tbody>
                </table>
              </td>
            </tr>
          </template>
        </tbody>
      </table>

      <div
        v-if="loading && !shares.length"
        class="py-12 flex items-center justify-center text-slate-400 gap-2"
      >
        <Loader2 :size="18" class="animate-spin" /> 加载中…
      </div>
      <div
        v-else-if="!shares.length"
        class="py-12 flex flex-col items-center justify-center text-slate-400 gap-2"
      >
        <Inbox :size="28" />
        <span>还没有共享目录</span>
        <span class="text-xs">
          在百度网盘里把数据文件夹分享出来，然后点「添加共享目录」
        </span>
      </div>
    </div>

    <div
      v-if="shares.length"
      class="mt-3 flex items-start gap-2 text-xs text-slate-500"
    >
      <CloudDownload :size="14" class="mt-0.5 shrink-0 text-slate-400" />
      <span>
        同步下来的文件先落在上面的服务器落点，请到「文件浏览」把它们移动到自己的工作目录。
      </span>
    </div>

    <ShareDialog
      v-if="showDialog"
      :share="editing"
      :inbox-base="status?.inbox_base"
      @close="showDialog = false"
      @saved="onSaved"
    />
  </div>
</template>
