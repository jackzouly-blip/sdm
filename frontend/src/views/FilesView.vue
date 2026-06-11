<script setup lang="ts">
import { ref, onMounted } from "vue";
import { api, errMsg } from "@/api";
import FileBrowser from "@/components/FileBrowser.vue";

// 初始进入第一个白名单根。
const startPath = ref<string>("");
const roots = ref<string[]>([]);
const error = ref("");
const ready = ref(false);

onMounted(async () => {
  try {
    roots.value = await api.fsRoots();
    startPath.value = roots.value[0] ?? "";
    if (!startPath.value) error.value = "未配置可浏览的根目录";
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    ready.value = true;
  }
});

function switchRoot(r: string) {
  startPath.value = r;
}
</script>

<template>
  <div class="w-full">
    <div class="flex items-center gap-3 mb-4">
      <h1 class="text-lg font-semibold text-slate-800">文件浏览</h1>
      <div v-if="roots.length > 1" class="flex gap-1">
        <button
          v-for="r in roots"
          :key="r"
          class="px-3 py-1 text-sm rounded-md border font-mono transition"
          :class="
            startPath === r
              ? 'bg-blue-600 text-white border-blue-600'
              : 'bg-white text-slate-600 border-slate-300 hover:bg-slate-50'
          "
          @click="switchRoot(r)"
        >
          {{ r }}
        </button>
      </div>
    </div>
    <p v-if="error" class="text-sm text-rose-600">{{ error }}</p>
    <FileBrowser
      v-if="ready && !error && startPath"
      :key="startPath"
      :initial-path="startPath"
    />
  </div>
</template>
