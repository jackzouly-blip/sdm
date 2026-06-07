<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from "vue";
import { api, errMsg } from "@/api";
import { fmtBytes } from "@/lib/format";
import { X, Loader2, Download, FileQuestion } from "lucide-vue-next";

const props = defineProps<{ path: string }>();
const emit = defineEmits<{ close: [] }>();

type Kind = "text" | "image" | "pdf" | "office" | "binary";
const loading = ref(true);
const error = ref("");
const content = ref("");
const size = ref(0);
const truncated = ref(false);
const kind = ref<Kind>("text");
const blobUrl = ref<string | null>(null);
const downloading = ref(false);
const converting = ref(false);   // Office 转 PDF 中
const officeErr = ref("");

const filename = props.path.split("/").pop() || "";
const ext = (filename.includes(".") ? filename.split(".").pop()! : "").toLowerCase();

const IMG = ["png", "jpg", "jpeg", "gif", "bmp", "webp", "svg", "ico"];
const OFFICE = ["doc", "docx", "xls", "xlsx", "ppt", "pptx"];

function classify(): Kind {
  if (IMG.includes(ext)) return "image";
  if (ext === "pdf") return "pdf";
  if (OFFICE.includes(ext)) return "office";
  return "text"; // 其余先按文本读取，由后端二进制判定兜底
}

async function loadBlob(asKind: Kind) {
  const blob = await api.previewBlob(props.path);
  size.value = blob.size;
  blobUrl.value = URL.createObjectURL(blob);
  kind.value = asKind;
}

onMounted(async () => {
  try {
    const k = classify();
    if (k === "image" || k === "pdf") {
      await loadBlob(k);
    } else if (k === "office") {
      // 服务器 LibreOffice 转 PDF 后内嵌预览；失败则回退下载提示
      kind.value = "office";
      converting.value = true;
      try {
        const blob = await api.officePdfBlob(props.path);
        size.value = blob.size;
        blobUrl.value = URL.createObjectURL(blob);
        kind.value = "pdf";
      } catch {
        officeErr.value = "在线预览失败（服务器可能未安装 LibreOffice），请下载查看";
      } finally {
        converting.value = false;
      }
    } else {
      const r = await api.preview(props.path);
      size.value = r.size;
      if (r.binary) {
        kind.value = "binary";
      } else {
        content.value = r.content;
        truncated.value = r.truncated;
        kind.value = "text";
      }
    }
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
});

onBeforeUnmount(() => {
  if (blobUrl.value) URL.revokeObjectURL(blobUrl.value);
});

async function download() {
  downloading.value = true;
  try {
    await api.download(props.path);
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    downloading.value = false;
  }
}
</script>

<template>
  <div
    class="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-6"
    @click.self="emit('close')"
  >
    <div class="bg-white rounded-xl shadow-xl w-full max-w-5xl max-h-[88vh] flex flex-col">
      <div class="flex items-center px-5 py-3 border-b border-slate-200">
        <div class="font-medium text-slate-800 truncate">{{ filename }}</div>
        <div class="ml-3 text-xs text-slate-400">
          {{ fmtBytes(size) }}
          <span v-if="truncated" class="text-amber-600">（文本已截断）</span>
        </div>
        <button
          class="ml-auto flex items-center gap-1 px-2.5 py-1 text-sm rounded-md border border-slate-300 hover:bg-slate-50 disabled:opacity-60"
          :disabled="downloading"
          @click="download"
        >
          <Loader2 v-if="downloading" :size="14" class="animate-spin" />
          <Download v-else :size="14" /> 下载
        </button>
        <button class="ml-2 p-1.5 rounded hover:bg-slate-100 text-slate-500" @click="emit('close')">
          <X :size="18" />
        </button>
      </div>

      <div class="flex-1 overflow-auto p-4 bg-slate-50">
        <div v-if="loading" class="py-16 flex items-center justify-center text-slate-400 gap-2">
          <Loader2 :size="18" class="animate-spin" /> {{ converting ? "正在转换 Office 文档为 PDF…" : "加载中…" }}
        </div>
        <p v-else-if="error" class="text-sm text-rose-600">{{ error }}</p>

        <!-- 图片 -->
        <div v-else-if="kind === 'image'" class="flex items-center justify-center h-full">
          <img :src="blobUrl!" :alt="filename" class="max-w-full max-h-[72vh] object-contain" />
        </div>

        <!-- PDF -->
        <iframe
          v-else-if="kind === 'pdf'"
          :src="blobUrl!"
          class="w-full h-[72vh] rounded border border-slate-200 bg-white"
        ></iframe>

        <!-- 文本 -->
        <pre
          v-else-if="kind === 'text'"
          class="text-xs font-mono whitespace-pre-wrap break-all text-slate-700"
          >{{ content }}</pre
        >

        <!-- Office / 二进制：不支持内嵌预览，提供下载 -->
        <div v-else class="py-16 flex flex-col items-center justify-center gap-3 text-slate-500">
          <FileQuestion :size="36" class="text-slate-400" />
          <div class="text-sm">
            {{ kind === "office" ? (officeErr || "Office 文件无法预览") : "二进制文件无法预览" }}
          </div>
          <button
            class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-700"
            @click="download"
          >
            <Download :size="15" /> 下载查看
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
