<script setup lang="ts">
import { ref, computed } from "vue";
import { api, errMsg, pollTask } from "@/api";
import DirPicker from "@/components/DirPicker.vue";
import { FolderInput, Loader2, X } from "lucide-vue-next";

const props = defineProps<{ paths: string[] }>();
const emit = defineEmits<{ close: []; moved: [] }>();

const cwd = ref("");
const moving = ref(false);
const error = ref("");
const phase = ref("");

/** 源本身不能作为目标（移进自己里），提前灰掉比让后端报错友好。 */
const srcSet = computed(() => props.paths);

const names = computed(() =>
  props.paths.map((p) => p.split("/").filter(Boolean).pop() ?? p)
);

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

// 目录的加载与导航都交给 DirPicker，这里只关心它最终选中了哪个目录（cwd）。
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

      <!-- 目标目录选择（与「添加共享目录」选落点复用同一个组件） -->
      <div class="px-5 py-3">
        <DirPicker
          v-model="cwd"
          :disabled-paths="srcSet"
          allow-mkdir
          height-class="max-h-56"
        />
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
          :disabled="moving || !cwd"
          @click="submit"
        >
          <Loader2 v-if="moving" :size="15" class="animate-spin" />
          移动到此处
        </button>
      </div>
    </div>
  </div>
</template>
