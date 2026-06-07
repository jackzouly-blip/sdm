<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from "vue";
import { useRoute } from "vue-router";
import { api, errMsg, pollTask } from "@/api";
import type { TaskSnapshot } from "@/api/types";
import { fmtTime, fmtBytes, taskStatusLabel } from "@/lib/format";
import {
  RefreshCw,
  Loader2,
  Inbox,
  Link as LinkIcon,
  Copy,
  Check,
} from "lucide-vue-next";

const route = useRoute();
const tasks = ref<TaskSnapshot[]>([]);
const loading = ref(false);
const error = ref("");
const copied = ref<string | null>(null);

// 活跃任务的进度轮询表，终态后停止。
const pollers = new Map<string, () => void>();
const TERMINAL = ["success", "failed", "interrupted"];

function upsert(snap: TaskSnapshot) {
  const i = tasks.value.findIndex((t) => t.id === snap.id);
  if (i >= 0) tasks.value[i] = snap;
  else tasks.value.unshift(snap);
}

function watchTask(id: string) {
  if (pollers.has(id)) return;
  const stop = pollTask(id, (snap) => {
    upsert(snap);
    if (TERMINAL.includes(snap.status)) closeWs(id);
  });
  pollers.set(id, stop);
}

function closeWs(id: string) {
  pollers.get(id)?.();
  pollers.delete(id);
}

async function load() {
  loading.value = true;
  error.value = "";
  try {
    tasks.value = await api.listTasks();
    // 对仍在进行中的任务建立实时订阅。
    for (const t of tasks.value) {
      if (!TERMINAL.includes(t.status)) watchTask(t.id);
    }
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}

async function copy(text: string, id: string) {
  await navigator.clipboard.writeText(text);
  copied.value = id;
  setTimeout(() => (copied.value === id && (copied.value = null)), 1500);
}

onMounted(async () => {
  await load();
  // 从打包对话框跳转过来时，聚焦订阅该任务。
  const focus = route.query.focus as string | undefined;
  if (focus) watchTask(focus);
});

onBeforeUnmount(() => {
  for (const id of Array.from(pollers.keys())) closeWs(id);
});

interface SharePackage {
  archive?: string;
  size?: number;
}
function shareInfo(t: TaskSnapshot) {
  if (t.type !== "package_upload") return null;
  const r = t.result as Record<string, any> | null;
  if (!r) return null;
  // 新结构：packages[]（一组文件可能拆成多个包，共用一个分享链接）；
  // 兼容旧记录：单包形态 {archive, size}。
  const packages: SharePackage[] = Array.isArray(r.packages)
    ? (r.packages as SharePackage[])
    : r.archive
    ? [{ archive: r.archive as string, size: r.size as number }]
    : [];
  return {
    packages,
    link: r.link as string | undefined,
    pwd: r.pwd as string | undefined,
    period: r.period as number | undefined,
    totalSize: r.total_size as number | undefined,
  };
}

// 任务类型标签。
function typeLabel(type: string): { text: string; cls: string } {
  switch (type) {
    case "package_upload":
      return { text: "打包上传", cls: "bg-indigo-100 text-indigo-700" };
    case "job_extract":
      return { text: "数据提取", cls: "bg-teal-100 text-teal-700" };
    default:
      return { text: type, cls: "bg-slate-100 text-slate-600" };
  }
}

// 提取任务结果（命令 + 退出码 + 输出）。
function extractInfo(t: TaskSnapshot) {
  if (t.type !== "job_extract") return null;
  const r = t.result as Record<string, any> | null;
  if (!r) return null;
  return {
    rule_name: r.rule_name as string | undefined,
    command: r.command as string | undefined,
    stdout: r.stdout as string | undefined,
    stderr: r.stderr as string | undefined,
  };
}
</script>

<template>
  <div class="max-w-5xl mx-auto">
    <div class="flex items-center gap-3 mb-4">
      <h1 class="text-lg font-semibold text-slate-800">任务记录</h1>
      <button
        class="ml-auto flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-slate-300 bg-white hover:bg-slate-50"
        :disabled="loading"
        @click="load"
      >
        <RefreshCw :size="15" :class="{ 'animate-spin': loading }" /> 刷新
      </button>
    </div>

    <p v-if="error" class="text-sm text-rose-600 mb-3">{{ error }}</p>

    <div
      v-if="loading && !tasks.length"
      class="py-12 flex items-center justify-center text-slate-400 gap-2"
    >
      <Loader2 :size="18" class="animate-spin" /> 加载中…
    </div>
    <div
      v-else-if="!tasks.length"
      class="py-16 flex flex-col items-center justify-center text-slate-400 gap-2"
    >
      <Inbox :size="28" /> 暂无任务记录
    </div>

    <div v-else class="space-y-3">
      <div
        v-for="t in tasks"
        :key="t.id"
        class="bg-white rounded-xl border border-slate-200 p-4"
      >
        <div class="flex items-center gap-3">
          <span
            class="px-2 py-0.5 rounded-full text-xs"
            :class="typeLabel(t.type).cls"
          >
            {{ typeLabel(t.type).text }}
          </span>
          <span
            class="px-2 py-0.5 rounded-full text-xs"
            :class="taskStatusLabel(t.status).cls"
          >
            {{ taskStatusLabel(t.status).text }}
          </span>
          <span class="text-sm text-slate-700">{{ t.phase || "—" }}</span>
          <span class="ml-auto text-xs text-slate-400">
            {{ fmtTime(t.updated_at) }}
          </span>
        </div>

        <!-- 进度条 -->
        <div
          v-if="!['success', 'failed', 'interrupted'].includes(t.status)"
          class="mt-3"
        >
          <div class="h-2 bg-slate-100 rounded-full overflow-hidden">
            <div
              class="h-full bg-blue-500 transition-all duration-300"
              :style="{ width: `${Math.round(t.progress)}%` }"
            />
          </div>
          <div class="text-xs text-slate-400 mt-1">
            {{ Math.round(t.progress) }}%
          </div>
        </div>

        <!-- 失败信息 -->
        <p v-if="t.status === 'failed'" class="mt-2 text-sm text-rose-600">
          {{ t.error }}
        </p>

        <!-- 成功分享信息 -->
        <div
          v-else-if="t.status === 'success' && shareInfo(t)"
          class="mt-3 text-sm bg-emerald-50 border border-emerald-100 rounded-lg p-3 space-y-1.5"
        >
          <div class="text-slate-700">
            <span class="text-slate-500">
              归档<span v-if="shareInfo(t)!.packages.length > 1"
                >（{{ shareInfo(t)!.packages.length }} 个包，需全部下载）</span
              >
            </span>
            <div class="mt-1 space-y-0.5">
              <div
                v-for="(pkg, i) in shareInfo(t)!.packages"
                :key="i"
                class="flex items-center gap-2"
              >
                <span class="font-mono">{{ pkg.archive }}</span>
                <span v-if="pkg.size" class="text-slate-400 text-xs">
                  ({{ fmtBytes(pkg.size) }})
                </span>
              </div>
            </div>
          </div>
          <div class="flex items-center gap-2">
            <LinkIcon :size="15" class="text-emerald-600" />
            <a
              :href="shareInfo(t)!.link"
              target="_blank"
              class="text-blue-600 hover:underline break-all"
            >
              {{ shareInfo(t)!.link }}
            </a>
            <button
              class="p-1 rounded hover:bg-emerald-100 text-slate-500"
              title="复制链接"
              @click="copy(shareInfo(t)!.link!, t.id)"
            >
              <Check v-if="copied === t.id" :size="14" class="text-emerald-600" />
              <Copy v-else :size="14" />
            </button>
          </div>
          <div class="text-slate-600">
            提取码
            <span class="font-mono font-medium">{{ shareInfo(t)!.pwd }}</span>
            <span class="text-slate-400 ml-3">
              有效期：{{ shareInfo(t)!.period === 0 ? "永久" : shareInfo(t)!.period + " 天" }}
            </span>
          </div>
        </div>

        <!-- 提取任务结果 -->
        <div
          v-else-if="t.status === 'success' && extractInfo(t)"
          class="mt-3 text-sm bg-teal-50 border border-teal-100 rounded-lg p-3 space-y-1.5"
        >
          <div class="text-slate-700">
            <span class="text-slate-500">规则</span>
            {{ extractInfo(t)!.rule_name }}
          </div>
          <div class="text-slate-600">
            <span class="text-slate-500">命令</span>
            <code class="font-mono text-xs break-all">{{
              extractInfo(t)!.command
            }}</code>
          </div>
          <pre
            v-if="extractInfo(t)!.stdout"
            class="bg-slate-900 text-slate-100 text-xs rounded p-2 overflow-auto max-h-40 whitespace-pre-wrap"
            >{{ extractInfo(t)!.stdout }}</pre>
        </div>
      </div>
    </div>
  </div>
</template>
