<script setup lang="ts">
/**
 * 服务器目录选择器（受控组件）。
 *
 * 从 MoveDialog 抽出来复用：选落点、选移动目标都是同一件事——在 fs_roots
 * 白名单内浏览并选中一个目录。后端仍会独立校验路径，这里只负责好用。
 */
import { ref, computed, onMounted, watch } from "vue";
import { api, errMsg } from "@/api";
import type { FsEntry } from "@/api/types";
import { Folder, Loader2, CornerLeftUp, Home, FolderPlus } from "lucide-vue-next";

const props = defineProps<{
  /** 当前选中的目录（v-model） */
  modelValue: string;
  /** 这些路径不可进入（如"待移动项本身"），可选 */
  disabledPaths?: string[];
  /** 是否显示"新建子目录"，默认不显示 */
  allowMkdir?: boolean;
  /** 列表区高度类，默认 max-h-48 */
  heightClass?: string;
}>();
const emit = defineEmits<{ "update:modelValue": [string] }>();

const roots = ref<string[]>([]);
const cwd = ref("");
const entries = ref<FsEntry[]>([]);
const loading = ref(false);
const error = ref("");
const creating = ref(false);
const newName = ref("");

const dirs = computed(() => entries.value.filter((e) => e.is_dir));
const blocked = computed(() => new Set(props.disabledPaths ?? []));
const atRoot = computed(() => roots.value.includes(cwd.value));

function join(dir: string, name: string): string {
  return `${dir.replace(/\/$/, "")}/${name}`;
}

async function load(p?: string) {
  loading.value = true;
  error.value = "";
  try {
    const resp = await api.listDir(p ?? cwd.value);
    cwd.value = resp.path;
    entries.value = resp.entries;
    emit("update:modelValue", resp.path);
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}

function goUp() {
  void load(cwd.value.replace(/\/[^/]+$/, "") || "/");
}

async function createDir() {
  const name = newName.value.trim();
  if (!name) return;
  error.value = "";
  try {
    await api.makeDir(cwd.value, name);
    newName.value = "";
    creating.value = false;
    await load(join(cwd.value, name)); // 建完直接进去，省一次点击
  } catch (e) {
    error.value = errMsg(e);
  }
}

// 外部改了绑定值（如切换编辑对象）时跟随跳转
watch(
  () => props.modelValue,
  (v) => {
    if (v && v !== cwd.value && !loading.value) void load(v);
  }
);

onMounted(async () => {
  try {
    roots.value = await api.fsRoots();
  } catch (e) {
    error.value = errMsg(e);
    return;
  }
  await load(props.modelValue || roots.value[0] || "/");
});
</script>

<template>
  <div class="border border-slate-200 rounded-lg overflow-hidden">
    <div
      class="flex items-center gap-2 px-3 py-2 bg-slate-50 border-b border-slate-200"
    >
      <button
        type="button"
        class="p-1 rounded hover:bg-slate-200 text-slate-500 disabled:opacity-30"
        title="上一级"
        :disabled="atRoot"
        @click="goUp"
      >
        <CornerLeftUp :size="14" />
      </button>
      <button
        v-for="r in roots"
        :key="r"
        type="button"
        class="p-1 rounded hover:bg-slate-200 text-slate-500"
        :title="`回到 ${r}`"
        @click="load(r)"
      >
        <Home :size="14" />
      </button>
      <code class="text-xs text-slate-600 truncate flex-1">{{ cwd }}</code>
      <button
        v-if="allowMkdir"
        type="button"
        class="p-1 rounded hover:bg-slate-200 text-slate-500 shrink-0"
        title="在当前目录新建子目录"
        @click="creating = !creating"
      >
        <FolderPlus :size="14" />
      </button>
    </div>

    <div v-if="creating" class="flex gap-2 px-3 py-2 border-b border-slate-100">
      <input
        v-model="newName"
        type="text"
        placeholder="新目录名"
        class="flex-1 px-2 py-1 rounded border border-slate-300 text-sm outline-none focus:border-blue-500"
        @keyup.enter="createDir"
      />
      <button
        type="button"
        class="px-2.5 py-1 text-sm rounded bg-blue-600 text-white hover:bg-blue-700"
        @click="createDir"
      >
        创建
      </button>
    </div>

    <div :class="heightClass ?? 'max-h-48'" class="overflow-auto">
      <div
        v-if="loading"
        class="py-8 flex items-center justify-center text-slate-400 gap-2 text-sm"
      >
        <Loader2 :size="15" class="animate-spin" /> 加载中…
      </div>
      <div v-else-if="!dirs.length" class="py-8 text-center text-sm text-slate-400">
        该目录下没有子目录
      </div>
      <button
        v-for="d in dirs"
        :key="d.name"
        type="button"
        class="w-full flex items-center gap-2 px-3 py-1.5 text-sm text-left hover:bg-slate-50 disabled:opacity-40 disabled:hover:bg-white"
        :disabled="blocked.has(join(cwd, d.name))"
        :title="blocked.has(join(cwd, d.name)) ? '不可选择该目录' : `进入 ${d.name}`"
        @click="load(join(cwd, d.name))"
      >
        <Folder :size="15" class="text-amber-500 shrink-0" />
        <span class="truncate text-slate-700">{{ d.name }}</span>
      </button>
    </div>

    <p v-if="error" class="px-3 py-1.5 text-xs text-rose-600 border-t border-slate-100">
      {{ error }}
    </p>
  </div>
</template>
