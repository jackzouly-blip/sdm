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
import { Box, Download, Eye, Loader2, Upload } from "lucide-vue-next";

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
  if (CAD_EXT.test(name)) return { can: false, text: "待轻量化" };
  return { can: false, text: "格式不支持预览" };
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
      标注「待轻量化」的是 CAD 原生格式，浏览器无法直接渲染，需先经 vektor3d 的
      <code class="px-1 bg-slate-100 rounded">geometry.convert</code> 能力转成
      glTF/GLB。该能力尚未上线，接口契约见
      <code class="px-1 bg-slate-100 rounded">docs/vektor3d-geometry-capability-contract.md</code>。
    </p>
  </div>
</template>
