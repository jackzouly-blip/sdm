<script setup lang="ts">
/** 编排定义列表。 */
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { simApi, errMsg } from "@/api";
import type { PipelineDef, PipelineRun } from "@/api/types";
import { GitBranch, Loader2, Plus, Trash2 } from "lucide-vue-next";

const router = useRouter();
const defs = ref<PipelineDef[]>([]);
const runs = ref<PipelineRun[]>([]);
const loading = ref(true);
const error = ref("");
const creating = ref(false);
const newName = ref("");

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

async function load() {
  loading.value = true;
  error.value = "";
  try {
    [defs.value, runs.value] = await Promise.all([
      simApi.listPipelines(),
      simApi.listRuns(),
    ]);
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}

async function create() {
  const name = newName.value.trim();
  if (!name) return;
  creating.value = true;
  try {
    // 新建时给一个最小可用的起点节点，空 DAG 后端会拒绝
    const d = await simApi.createPipeline({
      name,
      doc: {
        nodes: [
          {
            id: "validate",
            type: "internal.validate_config",
            label: "配置校验",
            params: {},
            position: { x: 120, y: 100 },
          },
        ],
        edges: [],
      },
    });
    newName.value = "";
    router.push({ name: "sim-pipeline-editor", params: { pid: d.id } });
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    creating.value = false;
  }
}

async function remove(d: PipelineDef) {
  if (!confirm(`删除编排「${d.name}」？已产生的运行记录不受影响。`)) return;
  try {
    await simApi.deletePipeline(d.id);
    await load();
  } catch (e) {
    error.value = errMsg(e);
  }
}

function fmt(ts: number) {
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
}

onMounted(load);
</script>

<template>
  <div class="w-full">
    <h1 class="text-lg font-semibold text-slate-800 mb-1">编排</h1>
    <p class="text-sm text-slate-500 mb-4">
      用有向无环图描述仿真流程：配置校验、输入卡生成、能力调用、集群求解按依赖自动推进。
    </p>

    <div v-if="error" class="mb-3 px-3 py-2 rounded-md bg-rose-50 text-rose-700 text-sm">
      {{ error }}
    </div>

    <div class="flex gap-2 mb-4">
      <input
        v-model="newName"
        class="flex-1 max-w-xs px-3 py-1.5 border border-slate-300 rounded-md text-sm"
        placeholder="新建编排名称"
        @keyup.enter="create"
      />
      <button
        class="flex items-center gap-1 px-3 py-1.5 rounded-md bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50"
        :disabled="creating || !newName.trim()"
        @click="create"
      >
        <Plus :size="15" /> 新建
      </button>
    </div>

    <div v-if="loading" class="flex items-center gap-2 text-slate-500 text-sm py-10 justify-center">
      <Loader2 :size="18" class="animate-spin" /> 加载中…
    </div>

    <template v-else>
      <div
        v-if="!defs.length"
        class="text-center py-12 text-slate-400 border border-dashed border-slate-200 rounded-lg mb-6"
      >
        <GitBranch :size="30" class="mx-auto mb-2 opacity-40" />
        <p class="text-sm">还没有编排定义</p>
      </div>
      <div v-else class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3 mb-8">
        <div
          v-for="d in defs"
          :key="d.id"
          class="group border border-slate-200 rounded-lg p-4 bg-white hover:border-blue-300 hover:shadow-sm transition cursor-pointer"
          @click="router.push({ name: 'sim-pipeline-editor', params: { pid: d.id } })"
        >
          <div class="flex items-start gap-2">
            <GitBranch :size="17" class="text-blue-600 mt-0.5 shrink-0" />
            <div class="min-w-0 flex-1">
              <div class="font-medium text-slate-800 truncate">{{ d.name }}</div>
              <div class="text-xs text-slate-400 mt-0.5">
                v{{ d.version }} · {{ d.doc.nodes?.length ?? 0 }} 个节点 ·
                {{ fmt(d.updated_at) }}
              </div>
            </div>
            <button
              class="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-rose-50 text-slate-400 hover:text-rose-600 transition"
              @click.stop="remove(d)"
            >
              <Trash2 :size="15" />
            </button>
          </div>
        </div>
      </div>

      <h2 class="text-sm font-semibold text-slate-700 mb-2">最近运行</h2>
      <p v-if="!runs.length" class="text-sm text-slate-400 py-6 text-center">
        还没有运行记录
      </p>
      <table v-else class="w-full text-sm">
        <thead class="text-slate-500 border-b border-slate-200">
          <tr>
            <th class="text-left font-medium py-2">运行</th>
            <th class="text-left font-medium py-2">状态</th>
            <th class="text-left font-medium py-2">开始时间</th>
            <th class="text-left font-medium py-2">说明</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="r in runs"
            :key="r.id"
            class="border-b border-slate-100 hover:bg-slate-50 cursor-pointer"
            @click="router.push({ name: 'sim-run', params: { rid: r.id } })"
          >
            <td class="py-2 font-mono text-xs text-blue-600">{{ r.id.slice(0, 8) }}</td>
            <td class="py-2">
              <span class="px-1.5 py-0.5 rounded text-xs" :class="RUN_STYLE[r.status]">
                {{ RUN_TEXT[r.status] ?? r.status }}
              </span>
            </td>
            <td class="py-2 text-slate-500">{{ fmt(r.created_at) }}</td>
            <td class="py-2 text-slate-500 truncate max-w-md">
              {{ r.error_message ?? "—" }}
            </td>
          </tr>
        </tbody>
      </table>
    </template>
  </div>
</template>
