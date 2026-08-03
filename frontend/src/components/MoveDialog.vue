<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { api, errMsg, pollTask } from "@/api";
import type { FsEntry } from "@/api/types";
import { FolderInput, Folder, Loader2, X, CornerLeftUp, Home } from "lucide-vue-next";

const props = defineProps<{ paths: string[] }>();
const emit = defineEmits<{ close: []; moved: [] }>();

const roots = ref<string[]>([]);
const cwd = ref("");
const entries = ref<FsEntry[]>([]);
const loading = ref(false);
const moving = ref(false);
const error = ref("");
const phase = ref("");

const dirs = computed(() => entries.value.filter((e) => e.is_dir));
/** 源本身不能作为目标（移进自己里），提前灰掉比让后端报错友好。 */
const srcSet = computed(() => new Set(props.paths));

const names = computed(() =>
  props.paths.map((p) => p.split("/").filter(Boolean).pop() ?? p)
);

async function load(p?: string) {
  loading.value = true;
  error.value = "";
  try {
    const resp = await api.listDir(p ?? cwd.value);
    cwd.value = resp.path;
    entries.value = resp.entries;
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}

function enter(name: string) {
  void load(`${cwd.value.replace(/\/$/, "")}/${name}`);
}

function goUp() {
  const parent = cwd.value.replace(/\/[^/]+$/, "") || "/";
  void load(parent);
}

const atRoot = computed(() => roots.value.includes(cwd.value));

async function submit() {
  moving.value = true;
  error.value = "";
  phase.value = "";
  try {
    const r = await api.movePaths(props.paths, cwd.value);
    if (r.task_id) {
      // 跨文件系统：要复制字节，改由任务异步做，这里跟进度直到终态
      phase.value = "跨文件系统移动，正在复制…";
      await new Promise<void>((resolve) => {
        pollTask(r.task_id as string, (s) => {
          phase.value = s.phase || "复制中…";
          if (["success", "failed", "interrupted"].includes(s.status)) {
            if (s.status !== "success") error.value = s.error || "移动失败";
            resolve();
          }
        });
      });
      if (error.value) return;
    } else if (r.failed.length) {
      error.value = `部分失败(${r.failed.length})：${r.failed
        .slice(0, 3)
        .map((f) => `${f.path.split("/").pop()}: ${f.error}`)
        .join("；")}`;
      return;
    }
    emit("moved");
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    moving.value = false;
  }
}

onMounted(async () => {
  try {
    roots.value = await api.fsRoots();
  } catch (e) {
    error.value = errMsg(e);
    return;
  }
  await load(roots.value[0] ?? "/");
});
</script>

<template>
  <div
    class="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-6"
    @click.self="emit('close')"
  >
    <div class="bg-white rounded-xl shadow-xl w-full max-w-lg flex flex-col max-h-[85vh]">
      <div class="flex items-center px-5 py-3 border-b border-slate-200">
        <FolderInput :size="18" class="text-blue-600 mr-2" />
        <div class="font-medium text-slate-800">移动 {{ paths.length }} 项</div>
        <button
          class="ml-auto p-1.5 rounded hover:bg-slate-100 text-slate-500"
          @click="emit('close')"
        >
          <X :size="18" />
        </button>
      </div>

      <div class="px-5 py-3 border-b border-slate-100">
        <div class="text-xs text-slate-500 mb-1">将移动：</div>
        <div class="text-sm text-slate-700 break-all max-h-16 overflow-auto">
          {{ names.slice(0, 6).join("、")
          }}<span v-if="names.length > 6" class="text-slate-400">
            …等 {{ names.length }} 项</span
          >
        </div>
      </div>

      <!-- 目标目录选择 -->
      <div class="flex items-center gap-2 px-5 py-2 bg-slate-50 border-b border-slate-200">
        <button
          class="p-1 rounded hover:bg-slate-200 text-slate-500 disabled:opacity-30"
          title="上一级"
          :disabled="atRoot"
          @click="goUp"
        >
          <CornerLeftUp :size="15" />
        </button>
        <button
          v-for="r in roots"
          :key="r"
          class="p-1 rounded hover:bg-slate-200 text-slate-500"
          :title="`回到 ${r}`"
          @click="load(r)"
        >
          <Home :size="15" />
        </button>
        <code class="text-xs text-slate-600 truncate">{{ cwd }}</code>
      </div>

      <div class="flex-1 overflow-auto min-h-[10rem]">
        <div
          v-if="loading"
          class="py-10 flex items-center justify-center text-slate-400 gap-2 text-sm"
        >
          <Loader2 :size="16" class="animate-spin" /> 加载中…
        </div>
        <div v-else-if="!dirs.length" class="py-10 text-center text-sm text-slate-400">
          该目录下没有子目录 —— 可直接移动到当前目录
        </div>
        <button
          v-for="d in dirs"
          :key="d.name"
          class="w-full flex items-center gap-2 px-5 py-2 text-sm text-left hover:bg-slate-50 disabled:opacity-40 disabled:hover:bg-white"
          :disabled="srcSet.has(`${cwd.replace(/\/$/, '')}/${d.name}`)"
          :title="
            srcSet.has(`${cwd.replace(/\/$/, '')}/${d.name}`)
              ? '这是待移动项本身'
              : `进入 ${d.name}`
          "
          @click="enter(d.name)"
        >
          <Folder :size="15" class="text-amber-500 shrink-0" />
          <span class="truncate text-slate-700">{{ d.name }}</span>
        </button>
      </div>

      <div class="px-5 py-2 text-xs" :class="error ? 'text-rose-600' : 'text-slate-500'">
        <span v-if="error">{{ error }}</span>
        <span v-else-if="phase">{{ phase }}</span>
        <span v-else>移动到：<code class="text-slate-600">{{ cwd }}</code></span>
      </div>

      <div class="px-5 py-3 border-t border-slate-200 flex justify-end gap-2">
        <button
          class="px-4 py-2 text-sm rounded-md border border-slate-300 hover:bg-slate-50"
          @click="emit('close')"
        >
          取消
        </button>
        <button
          class="px-4 py-2 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-60 flex items-center gap-2"
          :disabled="moving || loading || !cwd"
          @click="submit"
        >
          <Loader2 v-if="moving" :size="15" class="animate-spin" />
          移动到此处
        </button>
      </div>
    </div>
  </div>
</template>
