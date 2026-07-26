<script setup lang="ts">
/**
 * 运行详情：逐节点状态与产出。
 *
 * 等待中的节点可在此手动回流——人工确认节点是常规用法；能力/HPC 节点也保留
 * 手动完成的口子，便于外部链路未就绪时把流程推下去。
 */
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRouter } from "vue-router";
import { simApi, errMsg } from "@/api";
import type { NodeRun, PipelineRun } from "@/api/types";
import {
  ArrowLeft,
  Ban,
  Check,
  CircleDashed,
  Clock,
  Loader2,
  X,
} from "lucide-vue-next";

const props = defineProps<{ rid: string }>();
const router = useRouter();

const run = ref<PipelineRun | null>(null);
const loading = ref(true);
const error = ref("");
let timer: number | undefined;

const RUN_STYLE: Record<string, string> = {
  running: "bg-amber-50 text-amber-700",
  waiting: "bg-sky-50 text-sky-700",
  done: "bg-emerald-50 text-emerald-700",
  failed: "bg-rose-50 text-rose-700",
  canceled: "bg-slate-100 text-slate-500",
};
const RUN_TEXT: Record<string, string> = {
  running: "运行中",
  waiting: "等待中",
  done: "完成",
  failed: "失败",
  canceled: "已取消",
};
const NODE_TEXT: Record<string, string> = {
  pending: "待启动",
  running: "执行中",
  waiting: "等待外部",
  done: "完成",
  failed: "失败",
  skipped: "已跳过",
};

const finished = computed(
  () => !!run.value && ["done", "failed", "canceled"].includes(run.value.status)
);

async function load() {
  try {
    run.value = await simApi.getRun(props.rid);
    error.value = "";
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}

async function complete(n: NodeRun, ok: boolean) {
  try {
    run.value = await simApi.completeNode(props.rid, n.node_id,
      ok ? { outputs: { confirmed_by: "手动" } } : { error: "人工标记失败" });
  } catch (e) {
    error.value = errMsg(e);
  }
}

async function cancel() {
  if (!confirm("取消这次运行？未启动的节点会被跳过。")) return;
  try {
    run.value = await simApi.cancelRun(props.rid);
  } catch (e) {
    error.value = errMsg(e);
  }
}

function fmt(ts: number | null) {
  return ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "—";
}

function short(v: unknown) {
  if (v == null) return "";
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s.length > 160 ? s.slice(0, 160) + "…" : s;
}

onMounted(async () => {
  await load();
  // 未完结时轮询；完结即停，不做无谓请求
  timer = window.setInterval(() => {
    if (finished.value) {
      window.clearInterval(timer);
      timer = undefined;
      return;
    }
    void load();
  }, 3000);
});
onUnmounted(() => timer && window.clearInterval(timer));
</script>

<template>
  <div class="w-full">
    <button
      class="flex items-center gap-1 text-sm text-slate-500 hover:text-slate-700 mb-3"
      @click="router.push({ name: 'sim-pipelines' })"
    >
      <ArrowLeft :size="15" /> 返回编排列表
    </button>

    <div v-if="error" class="mb-3 px-3 py-2 rounded-md bg-rose-50 text-rose-700 text-sm">
      {{ error }}
    </div>

    <div v-if="loading" class="flex items-center gap-2 text-slate-500 text-sm py-10 justify-center">
      <Loader2 :size="18" class="animate-spin" /> 加载中…
    </div>

    <template v-else-if="run">
      <div class="flex items-center gap-3 mb-4">
        <h1 class="font-semibold text-slate-800">运行 {{ run.id.slice(0, 8) }}</h1>
        <span class="px-2 py-0.5 rounded text-xs" :class="RUN_STYLE[run.status]">
          {{ RUN_TEXT[run.status] ?? run.status }}
        </span>
        <span class="text-xs text-slate-400">
          定义 v{{ run.def_version }} · {{ fmt(run.created_at) }}
        </span>
        <button
          v-if="!finished"
          class="ml-auto flex items-center gap-1 px-2.5 py-1 rounded-md border border-slate-300 text-sm text-slate-600 hover:bg-slate-50"
          @click="cancel"
        >
          <Ban :size="14" /> 取消运行
        </button>
      </div>

      <div
        v-if="run.error_message"
        class="mb-4 px-3 py-2 rounded-md bg-rose-50 text-rose-700 text-sm"
      >
        {{ run.error_message }}
      </div>

      <div class="space-y-2">
        <div
          v-for="n in run.nodes"
          :key="n.id"
          class="border border-slate-200 rounded-lg bg-white p-3"
        >
          <div class="flex items-center gap-2">
            <Check v-if="n.status === 'done'" :size="16" class="text-emerald-600" />
            <X v-else-if="n.status === 'failed'" :size="16" class="text-rose-600" />
            <Clock v-else-if="n.status === 'waiting'" :size="16" class="text-sky-600" />
            <Loader2
              v-else-if="n.status === 'running'"
              :size="16"
              class="text-amber-600 animate-spin"
            />
            <CircleDashed v-else :size="16" class="text-slate-300" />

            <span class="font-medium text-slate-800 text-sm">{{ n.node_id }}</span>
            <span class="text-xs text-slate-400 font-mono">{{ n.node_type }}</span>
            <span class="text-xs text-slate-500">{{ NODE_TEXT[n.status] ?? n.status }}</span>

            <div v-if="n.status === 'waiting'" class="ml-auto flex gap-1.5">
              <button
                class="px-2 py-1 rounded text-xs bg-emerald-600 text-white hover:bg-emerald-700"
                @click="complete(n, true)"
              >
                标记完成
              </button>
              <button
                class="px-2 py-1 rounded text-xs border border-slate-300 text-slate-600 hover:bg-slate-50"
                @click="complete(n, false)"
              >
                标记失败
              </button>
            </div>
          </div>

          <p v-if="n.wait_hint && n.status === 'waiting'" class="text-xs text-sky-700 mt-1.5 ml-6">
            {{ n.wait_hint }}
            <span v-if="n.external_ref" class="font-mono text-slate-500">
              （句柄 {{ n.external_ref }}）
            </span>
          </p>
          <p v-if="n.error_message" class="text-xs text-rose-600 mt-1.5 ml-6">
            {{ n.error_message }}
          </p>
          <p v-if="n.outputs" class="text-xs text-slate-500 mt-1.5 ml-6 font-mono">
            产出：{{ short(n.outputs) }}
          </p>
        </div>
      </div>
    </template>
  </div>
</template>
