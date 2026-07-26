<script setup lang="ts">
/**
 * 工况模板列表。
 *
 * 模板的三个 json 是 AI 的约束边界：schema 定义可填什么、validation_rules
 * 定义什么算对、export_mapping 定义如何导出求解器输入卡。这里先做清单与查看，
 * 结构化编辑随编排能力一起做——在节点类型 schema 稳定前不急于固化编辑器。
 */
import { onMounted, ref } from "vue";
import { simApi, errMsg } from "@/api";
import type { SimTemplate } from "@/api/types";
import { FileCode, Loader2, Lock, Trash2 } from "lucide-vue-next";

const templates = ref<SimTemplate[]>([]);
const loading = ref(true);
const error = ref("");
const expanded = ref<string | null>(null);

async function load() {
  loading.value = true;
  error.value = "";
  try {
    templates.value = await simApi.listTemplates();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}

async function remove(t: SimTemplate) {
  if (!confirm(`删除工况模板「${t.name}」？`)) return;
  try {
    await simApi.deleteTemplate(t.id);
    await load();
  } catch (e) {
    error.value = errMsg(e);
  }
}

function pretty(v: unknown) {
  return v ? JSON.stringify(v, null, 2) : "（未配置）";
}

/** 模板的三段约束，按固定顺序并排展示。 */
function sections(t: SimTemplate): { label: string; value: unknown }[] {
  return [
    { label: "可填字段 schema", value: t.schema },
    { label: "校验规则", value: t.validation_rules },
    { label: "输入卡导出映射", value: t.export_mapping },
  ];
}

onMounted(load);
</script>

<template>
  <div class="w-full">
    <h1 class="text-lg font-semibold text-slate-800 mb-1">工况模板</h1>
    <p class="text-sm text-slate-500 mb-4">
      模板定义一类工况的可填字段、校验规则与求解器输入卡导出映射，是 AI 生成配置时的约束边界。
    </p>

    <div v-if="error" class="mb-3 px-3 py-2 rounded-md bg-rose-50 text-rose-700 text-sm">
      {{ error }}
    </div>

    <div v-if="loading" class="flex items-center gap-2 text-slate-500 text-sm py-10 justify-center">
      <Loader2 :size="18" class="animate-spin" /> 加载中…
    </div>

    <div
      v-else-if="!templates.length"
      class="text-center py-16 text-slate-400 border border-dashed border-slate-200 rounded-lg"
    >
      <FileCode :size="32" class="mx-auto mb-2 opacity-40" />
      <p class="text-sm">还没有工况模板</p>
    </div>

    <div v-else class="space-y-2">
      <div
        v-for="t in templates"
        :key="t.id"
        class="border border-slate-200 rounded-lg bg-white overflow-hidden"
      >
        <div
          class="flex items-center gap-2 px-4 py-3 cursor-pointer hover:bg-slate-50"
          @click="expanded = expanded === t.id ? null : t.id"
        >
          <FileCode :size="16" class="text-blue-600 shrink-0" />
          <span class="font-medium text-slate-800">{{ t.name }}</span>
          <span class="px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 text-xs">
            {{ t.subject_type }} / {{ t.solver_type }}
          </span>
          <Lock v-if="t.is_builtin" :size="13" class="text-slate-400" title="内置模板" />
          <button
            v-if="!t.is_builtin"
            class="ml-auto p-1 rounded hover:bg-rose-50 text-slate-400 hover:text-rose-600"
            @click.stop="remove(t)"
          >
            <Trash2 :size="14" />
          </button>
        </div>

        <div v-if="expanded === t.id" class="border-t border-slate-100 p-4 bg-slate-50/50">
          <div class="grid gap-3 lg:grid-cols-3">
            <div v-for="sec in sections(t)" :key="sec.label">
              <div class="text-xs font-medium text-slate-600 mb-1">{{ sec.label }}</div>
              <pre
                class="text-xs bg-white border border-slate-200 rounded p-2 overflow-auto max-h-56 text-slate-600"
              >{{ pretty(sec.value) }}</pre>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
