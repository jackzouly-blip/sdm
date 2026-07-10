<script setup lang="ts">
import { ref, onMounted, computed } from "vue";
import { api, errMsg } from "@/api";
import type { JobTemplate, TemplateKind } from "@/api/types";
import { Loader2, Plus, Pencil, Trash2, Save, X } from "lucide-vue-next";

const list = ref<JobTemplate[]>([]);
const loading = ref(false);
const error = ref("");
const selectedId = ref<number | null>(null);
const editing = ref(false);
const saving = ref(false);
// 编辑/新建缓冲
const draftName = ref("");
const draftContent = ref("");
const draftKind = ref<TemplateKind>("pbs");

function kindLabel(k: TemplateKind): string {
  return k === "trial" ? "试算" : "作业";
}

const selected = computed(() => list.value.find((t) => t.id === selectedId.value) || null);

async function load() {
  loading.value = true;
  error.value = "";
  try {
    list.value = await api.listTemplates();
    if (selectedId.value === null && list.value.length) selectedId.value = list.value[0].id;
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}
onMounted(load);

function select(t: JobTemplate) {
  if (editing.value) return; // 编辑中先保存/取消
  selectedId.value = t.id;
}

function startNew() {
  editing.value = true;
  selectedId.value = null;
  draftName.value = "";
  draftContent.value = "#!/bin/bash\n";
  draftKind.value = "pbs";
}

function startEdit() {
  if (!selected.value) return;
  editing.value = true;
  draftName.value = selected.value.name;
  draftContent.value = selected.value.content;
  draftKind.value = selected.value.kind || "pbs";
}

function cancel() {
  editing.value = false;
  if (selectedId.value === null && list.value.length) selectedId.value = list.value[0].id;
}

async function save() {
  if (!draftName.value.trim()) {
    error.value = "请填写模板名称";
    return;
  }
  saving.value = true;
  error.value = "";
  try {
    if (selectedId.value === null) {
      const t = await api.createTemplate(
        draftName.value.trim(),
        draftContent.value,
        draftKind.value
      );
      await load();
      selectedId.value = t.id;
    } else {
      await api.updateTemplate(
        selectedId.value,
        draftName.value.trim(),
        draftContent.value,
        draftKind.value
      );
      await load();
    }
    editing.value = false;
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    saving.value = false;
  }
}

async function remove() {
  if (!selected.value) return;
  if (!window.confirm(`确定删除模板「${selected.value.name}」？`)) return;
  try {
    await api.deleteTemplate(selected.value.id);
    selectedId.value = null;
    await load();
  } catch (e) {
    error.value = errMsg(e);
  }
}
</script>

<template>
  <div class="w-full">
    <div class="flex items-center gap-3 mb-4">
      <h1 class="text-lg font-semibold text-slate-800">模板管理</h1>
      <span class="text-xs text-slate-400">作业提交脚本模板（管理员维护）</span>
      <button
        class="ml-auto flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-700"
        @click="startNew"
      >
        <Plus :size="15" /> 新建模板
      </button>
    </div>

    <p v-if="error" class="text-sm text-rose-600 mb-3">{{ error }}</p>

    <div class="flex gap-4" style="min-height: 60vh">
      <!-- 模板列表 -->
      <div class="w-60 shrink-0 bg-white rounded-xl border border-slate-200 overflow-hidden">
        <div class="px-3 py-2 text-xs text-slate-400 border-b border-slate-100">模板列表</div>
        <div v-if="loading" class="p-4 text-slate-400 text-sm flex items-center gap-2">
          <Loader2 :size="16" class="animate-spin" /> 加载中…
        </div>
        <ul v-else class="py-1">
          <li
            v-for="t in list"
            :key="t.id"
            class="px-3 py-2 text-sm cursor-pointer flex items-center gap-2"
            :class="selectedId === t.id && !editing ? 'bg-blue-50 text-blue-700' : 'text-slate-700 hover:bg-slate-50'"
            @click="select(t)"
          >
            <span class="w-1.5 h-1.5 rounded-full" :class="selectedId === t.id ? 'bg-blue-500' : 'bg-slate-300'"></span>
            <span class="truncate">{{ t.name }}</span>
            <span
              v-if="t.kind === 'trial'"
              class="ml-auto shrink-0 px-1.5 py-0.5 rounded text-[10px] bg-indigo-100 text-indigo-700"
              >试算</span
            >
          </li>
          <li v-if="!list.length" class="px-3 py-3 text-xs text-slate-400">暂无模板</li>
        </ul>
      </div>

      <!-- 内容 / 编辑 -->
      <div class="flex-1 bg-white rounded-xl border border-slate-200 flex flex-col overflow-hidden">
        <div class="px-4 py-2 border-b border-slate-100 flex items-center gap-2">
          <template v-if="editing">
            <input
              v-model="draftName"
              placeholder="模板名称"
              class="px-2 py-1 text-sm border border-slate-300 rounded-md w-48"
            />
            <select
              v-model="draftKind"
              class="px-2 py-1 text-sm border border-slate-300 rounded-md"
              title="作业=提交到 PBS 队列；试算=在管理节点直接跑命令行"
            >
              <option value="pbs">作业模板</option>
              <option value="trial">试算模板</option>
            </select>
            <button
              class="ml-auto flex items-center gap-1 px-2.5 py-1 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-60"
              :disabled="saving"
              @click="save"
            >
              <Loader2 v-if="saving" :size="14" class="animate-spin" /><Save v-else :size="14" /> 保存
            </button>
            <button class="flex items-center gap-1 px-2.5 py-1 text-sm rounded-md border border-slate-300 hover:bg-slate-50" @click="cancel">
              <X :size="14" /> 取消
            </button>
          </template>
          <template v-else-if="selected">
            <span class="text-sm font-medium text-slate-700">{{ selected.name }}</span>
            <span
              class="px-1.5 py-0.5 rounded text-[11px]"
              :class="selected.kind === 'trial' ? 'bg-indigo-100 text-indigo-700' : 'bg-slate-100 text-slate-500'"
              >{{ kindLabel(selected.kind) }}模板</span
            >
            <button class="ml-auto flex items-center gap-1 px-2.5 py-1 text-sm rounded-md border border-slate-300 hover:bg-slate-50 text-slate-600" @click="startEdit">
              <Pencil :size="14" /> 编辑
            </button>
            <button class="flex items-center gap-1 px-2.5 py-1 text-sm rounded-md border border-slate-300 hover:bg-rose-50 text-rose-600" @click="remove">
              <Trash2 :size="14" /> 删除
            </button>
          </template>
          <span v-else class="text-sm text-slate-400">选择左侧模板查看，或点击「新建模板」</span>
        </div>
        <div class="flex-1 overflow-auto">
          <textarea
            v-if="editing"
            v-model="draftContent"
            spellcheck="false"
            class="w-full h-full min-h-[55vh] p-4 text-xs font-mono text-slate-700 resize-none outline-none"
          ></textarea>
          <pre
            v-else-if="selected"
            class="p-4 text-xs font-mono whitespace-pre-wrap break-all text-slate-600"
            >{{ selected.content }}</pre
          >
        </div>
      </div>
    </div>
  </div>
</template>
