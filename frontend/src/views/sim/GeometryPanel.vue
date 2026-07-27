<script setup lang="ts">
/**
 * 分析对象的几何版本面板：上传数模、查看版本、下载、预览。
 *
 * CAD 原生格式（CATIA/STEP/JT）浏览器渲染不了，必须先经 vektor3d 的
 * geometry.convert 能力转成 glTF/GLB。该能力尚未上线，故此处对未轻量化的版本
 * 明确标注状态，而不是给一个点了没反应的预览按钮。
 * 契约见 docs/vektor3d-geometry-capability-contract.md。
 */
import { onMounted, ref } from "vue";
import { simApi, errMsg } from "@/api";
import type { SimGeometry } from "@/api/types";
import { AlertTriangle, Box, Download, Eye, FolderInput, Loader2, Upload } from "lucide-vue-next";

const props = defineProps<{ targetId: string; targetName: string }>();
const emit = defineEmits<{ (e: "preview", g: SimGeometry): void }>();

const geometries = ref<SimGeometry[]>([]);
const loading = ref(true);
const error = ref("");
const uploading = ref(false);
const progress = ref(0);
const fileInput = ref<HTMLInputElement | null>(null);

/** 需要轻量化才能渲染的 CAD 原生格式 */
const CAD_EXT = /\.(catpart|catproduct|stp|step|jt|igs|iges|3dxml|prt|sldprt|sldasm|x_t|x_b)$/i;
/** 求解器输入卡：SDM 自己解析成 GLB，不依赖 vektor3d */
const DECK_EXT = /\.(k|key|kinc|dyn)$/i;

const clusterPath = ref("");
const importing = ref(false);
const converting = ref<string | null>(null);

/**
 * 从集群路径导入。deck 走这条路是必须的——主控 .key 会牵出几百 MB 的 include
 * 树、散在集群目录里，浏览器传不上来，而它本就在集群上。
 */
async function importFromPath() {
  const p = clusterPath.value.trim();
  if (!p) return;
  importing.value = true;
  error.value = "";
  try {
    const g = await simApi.addGeometryFromPath(props.targetId, p);
    clusterPath.value = "";
    await load();
    if (g.convert_task_id) {
      converting.value = g.id;
      pollConversion(g.id);
    }
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    importing.value = false;
  }
}

/** deck 转换是分钟级的异步任务，轮询到产物出现为止 */
function pollConversion(gid: string) {
  const timer = window.setInterval(async () => {
    try {
      const rows = await simApi.listGeometries(props.targetId);
      geometries.value = rows;
      const g = rows.find((r) => r.id === gid);
      if (g?.lightweight_file) {
        window.clearInterval(timer);
        converting.value = null;
      }
    } catch {
      window.clearInterval(timer);
      converting.value = null;
    }
  }, 5000);
  // 兜底：整车 deck 实测约 40 秒，10 分钟仍无产物即认为失败，停止轮询
  window.setTimeout(() => {
    window.clearInterval(timer);
    if (converting.value === gid) converting.value = null;
  }, 600000);
}

async function load() {
  loading.value = true;
  error.value = "";
  try {
    geometries.value = await simApi.listGeometries(props.targetId);
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}

async function onPick(ev: Event) {
  const input = ev.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  uploading.value = true;
  progress.value = 0;
  error.value = "";
  try {
    await simApi.uploadGeometry(props.targetId, file, (p) => (progress.value = p));
    await load();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    uploading.value = false;
    input.value = ""; // 允许同名文件再次选择
  }
}

function fmtSize(n?: number) {
  if (!n) return "—";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}

function fmt(ts: number) {
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
}

/** 该版本当前能否在网页里渲染 */
function renderState(g: SimGeometry): { can: boolean; text: string } {
  if (g.lightweight_file) return { can: true, text: "可预览" };
  const name = g.source_file?.name ?? "";
  if (converting.value === g.id) return { can: false, text: "转换中…" };
  // deck 由 SDM 自己解析，不依赖 vektor3d；没产物说明转换失败或还没跑
  if (DECK_EXT.test(name)) return { can: false, text: "待转换" };
  if (CAD_EXT.test(name)) return { can: false, text: "待轻量化" };
  return { can: false, text: "格式不支持预览" };
}

/** 摘要里的关键读数：include 缺失意味着模型不完整，必须显眼 */
function deckWarning(g: SimGeometry): string | null {
  const s = g.topo_summary as Record<string, unknown> | null;
  if (!s) return null;
  const missing = Number(s.missingIncludeCount ?? 0);
  if (missing > 0) return `${missing} 个 include 缺失，模型可能不完整`;
  if (s.truncated) return "三角面超预算，已按零件截断";
  return null;
}

onMounted(load);
defineExpose({ reload: load });
</script>

<template>
  <div>
    <div class="flex items-center gap-2 mb-3">
      <span class="text-sm text-slate-600">{{ targetName }} 的几何版本</span>
      <input
        ref="fileInput"
        type="file"
        class="hidden"
        accept=".CATPart,.CATProduct,.stp,.step,.jt,.igs,.iges,.3dxml,.prt,.sldprt,.sldasm,.x_t,.x_b,.stl,.obj,.glb,.gltf"
        @change="onPick"
      />
      <button
        class="ml-auto flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50"
        :disabled="uploading"
        @click="fileInput?.click()"
      >
        <Loader2 v-if="uploading" :size="15" class="animate-spin" />
        <Upload v-else :size="15" />
        {{ uploading ? `上传中 ${progress}%` : "导入数模" }}
      </button>
    </div>

    <!-- 从集群路径导入：deck 只能走这条（include 树在集群上，传不上来） -->
    <div class="flex gap-2 mb-3">
      <input
        v-model="clusterPath"
        class="flex-1 px-3 py-1.5 border border-slate-300 rounded-md text-sm font-mono"
        placeholder="集群路径，如 /data/project-ext/user07/.../000_Master.key"
        @keyup.enter="importFromPath"
      />
      <button
        class="flex items-center gap-1.5 px-3 py-1.5 rounded-md border border-slate-300 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50 shrink-0"
        :disabled="importing || !clusterPath.trim()"
        @click="importFromPath"
      >
        <Loader2 v-if="importing" :size="15" class="animate-spin" />
        <FolderInput v-else :size="15" />
        从集群导入
      </button>
    </div>

    <div v-if="error" class="mb-3 px-3 py-2 rounded-md bg-rose-50 text-rose-700 text-sm">
      {{ error }}
    </div>

    <div v-if="loading" class="flex items-center gap-2 text-slate-500 text-sm py-6 justify-center">
      <Loader2 :size="16" class="animate-spin" /> 加载中…
    </div>

    <div
      v-else-if="!geometries.length"
      class="text-center py-10 text-slate-400 border border-dashed border-slate-200 rounded-lg"
    >
      <Box :size="26" class="mx-auto mb-2 opacity-40" />
      <p class="text-sm">还没有几何版本</p>
      <p class="text-xs mt-1">导入 CATIA / STEP / JT 等数模文件</p>
    </div>

    <table v-else class="w-full text-sm">
      <thead class="text-slate-500 border-b border-slate-200">
        <tr>
          <th class="text-left font-medium py-2 w-14">版本</th>
          <th class="text-left font-medium py-2">文件</th>
          <th class="text-left font-medium py-2 w-20">大小</th>
          <th class="text-left font-medium py-2 w-28">渲染状态</th>
          <th class="text-left font-medium py-2">导入时间</th>
          <th class="w-28"></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="g in geometries" :key="g.id" class="border-b border-slate-100">
          <td class="py-2 text-slate-500">v{{ g.version_no }}</td>
          <td class="py-2 text-slate-800 truncate max-w-xs">
            {{ g.source_file?.name ?? "—" }}
          </td>
          <td class="py-2 text-slate-500">{{ fmtSize(g.source_file?.size) }}</td>
          <td class="py-2">
            <span
              class="px-1.5 py-0.5 rounded text-xs"
              :class="
                renderState(g).can
                  ? 'bg-emerald-50 text-emerald-700'
                  : 'bg-amber-50 text-amber-700'
              "
              :title="
                renderState(g).can
                  ? '已有轻量化产物，可在网页中渲染'
                  : 'CAD 原生格式需先经 vektor3d 转成 glTF/GLB 才能在浏览器渲染'
              "
            >
              {{ renderState(g).text }}
            </span>
            <span
              v-if="deckWarning(g)"
              class="ml-1 inline-flex items-center gap-0.5 text-xs text-amber-600"
              :title="deckWarning(g)!"
            >
              <AlertTriangle :size="11" />
            </span>
          </td>
          <td class="py-2 text-slate-500">{{ fmt(g.created_at) }}</td>
          <td class="py-2">
            <div class="flex items-center gap-1">
              <button
                v-if="renderState(g).can"
                class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs text-blue-600 hover:bg-blue-50"
                @click="emit('preview', g)"
              >
                <Eye :size="12" /> 预览
              </button>
              <a
                :href="simApi.sourceDownloadUrl(g.id)"
                class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs text-slate-500 hover:bg-slate-50"
                title="下载源文件"
              >
                <Download :size="12" /> 源文件
              </a>
            </div>
          </td>
        </tr>
      </tbody>
    </table>

    <p
      v-if="geometries.some((g) => !renderState(g).can)"
      class="text-xs text-slate-400 mt-3 leading-relaxed"
    >
      求解器输入卡（.k/.key）由 SDM 自行解析成 glTF，不依赖 vektor3d；标注「待轻量化」的是 CAD 原生格式，浏览器无法直接渲染，需先经 vektor3d 的
      <code class="px-1 bg-slate-100 rounded">geometry.convert</code> 能力转成
      glTF/GLB。该能力尚未上线，接口契约见
      <code class="px-1 bg-slate-100 rounded">docs/vektor3d-geometry-capability-contract.md</code>。
    </p>
  </div>
</template>
