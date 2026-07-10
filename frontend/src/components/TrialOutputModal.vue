<script setup lang="ts">
import { ref, onMounted, onUnmounted, nextTick, computed } from "vue";
import { api, errMsg } from "@/api";
import type { TrialStatus } from "@/api/types";
import { X, Loader2, Ban, Download } from "lucide-vue-next";

const props = defineProps<{ trialId: number; name?: string }>();
const emit = defineEmits<{ close: []; changed: [] }>();

const text = ref("");
const offset = ref(0);
const status = ref<TrialStatus>("running");
const exitCode = ref<number | null>(null);
const msg = ref<string | null>(null);
const error = ref("");
const cancelling = ref(false);
const autoScroll = ref(true);
const preEl = ref<HTMLElement | null>(null);

let timer: ReturnType<typeof setTimeout> | undefined;
let stopped = false;

const TERMINAL: TrialStatus[] = ["finished", "failed", "killed", "interrupted"];
const isRunning = computed(() => status.value === "running");

const statusText = computed(() => {
  switch (status.value) {
    case "running":
      return "试算中…";
    case "finished":
      return "已完成";
    case "failed":
      return `失败${exitCode.value != null ? `（退出码 ${exitCode.value}）` : ""}`;
    case "killed":
      return "已中断";
    case "interrupted":
      return "中断";
    default:
      return status.value;
  }
});
const statusCls = computed(() => {
  switch (status.value) {
    case "running":
      return "bg-indigo-100 text-indigo-700";
    case "finished":
      return "bg-emerald-100 text-emerald-700";
    case "failed":
      return "bg-rose-100 text-rose-700";
    default:
      return "bg-slate-200 text-slate-600";
  }
});

async function tick() {
  if (stopped) return;
  try {
    const r = await api.trialOutput(props.trialId, offset.value);
    if (stopped) return;
    if (r.data) {
      text.value += r.data;
      offset.value = r.offset;
      if (autoScroll.value) {
        await nextTick();
        if (preEl.value) preEl.value.scrollTop = preEl.value.scrollHeight;
      }
    }
    status.value = r.status;
    exitCode.value = r.exit_code;
    msg.value = r.msg;
    if (TERMINAL.includes(r.status)) {
      stopped = true;
      emit("changed"); // 通知列表刷新状态
      return;
    }
  } catch (e) {
    if (stopped) return;
    error.value = errMsg(e);
  }
  timer = setTimeout(tick, 1200);
}

async function cancel() {
  if (!window.confirm("确定中断该试算吗？将终止管理节点上的计算进程。")) return;
  cancelling.value = true;
  error.value = "";
  try {
    await api.cancelTrial(props.trialId);
    // 让下一轮轮询尽快取到终态输出
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    cancelling.value = false;
  }
}

function download() {
  const blob = new Blob([text.value], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `trial_${props.trialId}_${props.name || "output"}.log`;
  a.click();
  URL.revokeObjectURL(url);
}

onMounted(tick);
onUnmounted(() => {
  stopped = true;
  if (timer) clearTimeout(timer);
});
</script>

<template>
  <div class="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-6" @click.self="emit('close')">
    <div class="bg-white rounded-xl shadow-xl w-full max-w-3xl flex flex-col max-h-[88vh]">
      <div class="flex items-center gap-2 px-5 py-3 border-b border-slate-200">
        <span class="font-medium text-slate-800">试算输出</span>
        <span class="text-slate-400 text-sm truncate">{{ name || `T${trialId}` }}</span>
        <span class="px-2 py-0.5 rounded-full text-xs" :class="statusCls">{{ statusText }}</span>
        <button class="ml-auto p-1.5 rounded hover:bg-slate-100 text-slate-500" @click="emit('close')">
          <X :size="18" />
        </button>
      </div>

      <div class="flex-1 overflow-hidden flex flex-col p-4 gap-2">
        <p v-if="msg && !isRunning" class="text-xs text-slate-500">{{ msg }}</p>
        <p v-if="error" class="text-sm text-rose-600">{{ error }}</p>
        <pre
          ref="preEl"
          class="flex-1 min-h-[40vh] overflow-auto bg-slate-900 text-slate-100 text-xs font-mono rounded-lg p-3 whitespace-pre-wrap break-all"
        >{{ text || (isRunning ? "等待输出…" : "（无输出）") }}</pre>
      </div>

      <div class="px-5 py-3 border-t border-slate-200 flex items-center gap-2">
        <label class="flex items-center gap-1.5 text-xs text-slate-500 select-none">
          <input v-model="autoScroll" type="checkbox" class="accent-blue-600" /> 自动滚动到底部
        </label>
        <button
          class="ml-auto flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-slate-300 hover:bg-slate-50 text-slate-600"
          :disabled="!text"
          @click="download"
        >
          <Download :size="15" /> 下载日志
        </button>
        <button
          v-if="isRunning"
          class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-rose-200 text-rose-600 hover:bg-rose-50 disabled:opacity-60"
          :disabled="cancelling"
          @click="cancel"
        >
          <Loader2 v-if="cancelling" :size="15" class="animate-spin" /><Ban v-else :size="15" /> 中断
        </button>
        <button
          class="flex items-center gap-1.5 px-4 py-1.5 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-700"
          @click="emit('close')"
        >
          关闭
        </button>
      </div>
    </div>
  </div>
</template>
