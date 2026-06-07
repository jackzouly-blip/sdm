<script setup lang="ts">
import { ref, onMounted } from "vue";
import { api, errMsg } from "@/api";
import { fmtBytes } from "@/lib/format";
import { X, PackageIcon, Loader2 } from "lucide-vue-next";

const props = defineProps<{ paths: string[]; nameHint?: string }>();

// 估算总大小与分卷数
const estLoading = ref(true);
const totalBytes = ref(0);
const estVolumes = ref(0);
const volumeBytes = ref(0);
onMounted(async () => {
  try {
    const r = await api.archiveEstimate(props.paths);
    totalBytes.value = r.total_bytes;
    estVolumes.value = r.est_volumes;
    volumeBytes.value = r.volume_bytes;
  } catch {
    /* 估算失败忽略，不阻塞打包 */
  } finally {
    estLoading.value = false;
  }
});
const emit = defineEmits<{ close: []; done: [taskId: string] }>();

// 归档名清洗：仅保留字母数字、点、连字符、下划线，避免非法/路径字符。
function sanitize(s: string): string {
  return s.replace(/[^\w.\-]+/g, "_").replace(/^_+|_+$/g, "") || "archive";
}
// 紧凑时间戳后缀 YYYYMMDD_HHMMSS，保证同名多次打包不冲突。
function tsSuffix(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(
    d.getHours()
  )}${p(d.getMinutes())}${p(d.getSeconds())}`;
}
// 默认归档基名：优先用任务名；否则回退到所选条目名 / 公共父目录名。
function defaultBase(): string {
  if (props.nameHint && props.nameHint.trim()) {
    return sanitize(props.nameHint.trim());
  }
  if (props.paths.length === 1) {
    const seg = props.paths[0].replace(/\/+$/, "").split("/").pop() || "archive";
    return sanitize(seg);
  }
  const parent =
    props.paths[0]?.replace(/\/+$/, "").split("/").slice(0, -1).pop() ||
    "archive";
  return sanitize(parent);
}

// 预填默认名（可见可改）；用户清空则后端自动生成。
const archive = ref(`${defaultBase()}_${tsSuffix()}.tar.gz`);
const pwd = ref("");
const period = ref(7);
const submitting = ref(false);
const error = ref("");

async function submit() {
  error.value = "";
  submitting.value = true;
  try {
    const { task_id } = await api.createPackage({
      paths: props.paths,
      archive: archive.value.trim() || null,
      pwd: pwd.value.trim() || null,
      period: period.value,
    });
    emit("done", task_id);
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    submitting.value = false;
  }
}

const periods = [
  { value: 1, label: "1 天" },
  { value: 7, label: "7 天" },
  { value: 30, label: "30 天" },
  { value: 0, label: "永久" },
];
</script>

<template>
  <div
    class="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-6"
    @click.self="emit('close')"
  >
    <div class="bg-white rounded-xl shadow-xl w-full max-w-md flex flex-col">
      <div class="flex items-center px-5 py-3 border-b border-slate-200">
        <PackageIcon :size="18" class="text-blue-600 mr-2" />
        <div class="font-medium text-slate-800">打包并上传网盘</div>
        <button
          class="ml-auto p-1.5 rounded hover:bg-slate-100 text-slate-500"
          @click="emit('close')"
        >
          <X :size="18" />
        </button>
      </div>

      <div class="p-5 flex flex-col gap-4">
        <div>
          <div class="text-sm text-slate-600 mb-1 flex items-center gap-2 flex-wrap">
            <span>已选 {{ paths.length }} 项</span>
            <span v-if="estLoading" class="text-xs text-slate-400 flex items-center gap-1">
              <Loader2 :size="12" class="animate-spin" /> 估算大小中…
            </span>
            <span v-else-if="totalBytes" class="text-xs text-slate-500">
              · 共 <span class="font-medium text-slate-700">{{ fmtBytes(totalBytes) }}</span>
              · 预计 <span class="font-medium text-slate-700">{{ estVolumes }}</span> 个包<span v-if="estVolumes > 1" class="text-slate-400">(每包≤{{ fmtBytes(volumeBytes) }})</span>
            </span>
          </div>
          <div
            class="max-h-28 overflow-auto rounded-md border border-slate-200 bg-slate-50 p-2 text-xs font-mono text-slate-500 space-y-0.5"
          >
            <div v-for="p in paths" :key="p" class="truncate">{{ p }}</div>
          </div>
        </div>

        <label class="block">
          <span class="text-sm text-slate-600">归档文件名（可留空自动生成）</span>
          <input
            v-model="archive"
            type="text"
            placeholder="results.tar.gz"
            class="mt-1 w-full px-3 py-2 rounded-md border border-slate-300 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none text-sm"
          />
        </label>

        <div class="flex gap-3">
          <label class="block flex-1">
            <span class="text-sm text-slate-600">提取码（留空自动生成）</span>
            <input
              v-model="pwd"
              type="text"
              maxlength="8"
              placeholder="自动"
              class="mt-1 w-full px-3 py-2 rounded-md border border-slate-300 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none text-sm"
            />
          </label>
          <label class="block flex-1">
            <span class="text-sm text-slate-600">有效期</span>
            <select
              v-model.number="period"
              class="mt-1 w-full px-3 py-2 rounded-md border border-slate-300 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none text-sm bg-white"
            >
              <option v-for="o in periods" :key="o.value" :value="o.value">
                {{ o.label }}
              </option>
            </select>
          </label>
        </div>

        <p v-if="error" class="text-sm text-rose-600">{{ error }}</p>
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
          :disabled="submitting"
          @click="submit"
        >
          <Loader2 v-if="submitting" :size="15" class="animate-spin" />
          开始打包
        </button>
      </div>
    </div>
  </div>
</template>
