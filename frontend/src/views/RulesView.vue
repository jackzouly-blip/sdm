<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { api, errMsg } from "@/api";
import type { ExtractRule } from "@/api/types";
import { fmtTime } from "@/lib/format";
import RuleDialog from "@/components/RuleDialog.vue";
import { useAuthStore } from "@/stores/auth";
import { Plus, RefreshCw, Loader2, Inbox, Pencil, Trash2 } from "lucide-vue-next";

const auth = useAuthStore();
const isAdmin = computed(() => !!auth.me?.is_admin);

const rules = ref<ExtractRule[]>([]);
const loading = ref(false);
const error = ref("");
const showDialog = ref(false);
const editing = ref<ExtractRule | null>(null);
const togglingId = ref<number | null>(null);

async function load() {
  loading.value = true;
  error.value = "";
  try {
    rules.value = await api.listRules();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}

function openNew() {
  editing.value = null;
  showDialog.value = true;
}

function openEdit(r: ExtractRule) {
  editing.value = r;
  showDialog.value = true;
}

function onSaved() {
  showDialog.value = false;
  load();
}

async function toggle(r: ExtractRule) {
  togglingId.value = r.id;
  try {
    await api.updateRule(r.id, { enabled: !r.enabled });
    await load();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    togglingId.value = null;
  }
}

async function remove(r: ExtractRule) {
  if (!confirm(`确定删除规则「${r.name}」？`)) return;
  try {
    await api.deleteRule(r.id);
    await load();
  } catch (e) {
    error.value = errMsg(e);
  }
}

onMounted(load);
</script>

<template>
  <div class="max-w-6xl mx-auto">
    <div class="flex items-center gap-3 mb-4">
      <h1 class="text-lg font-semibold text-slate-800">数据后处理工具</h1>
      <span class="text-sm text-slate-400">任务完成后自动在工作目录中运行</span>
      <span v-if="!isAdmin" class="text-xs text-slate-400">（只读，工具由管理员维护）</span>
      <div class="ml-auto flex items-center gap-2">
        <button
          v-if="isAdmin"
          class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-700"
          @click="openNew"
        >
          <Plus :size="15" /> 新建工具
        </button>
        <button
          class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-slate-300 bg-white hover:bg-slate-50"
          :disabled="loading"
          @click="load"
        >
          <RefreshCw :size="15" :class="{ 'animate-spin': loading }" /> 刷新
        </button>
      </div>
    </div>

    <p v-if="error" class="text-sm text-rose-600 mb-3">{{ error }}</p>

    <div class="bg-white rounded-xl border border-slate-200 overflow-x-auto">
      <table class="w-full text-sm min-w-[680px]">
        <thead class="bg-slate-50 text-slate-500 text-left">
          <tr>
            <th class="px-4 py-2.5 font-medium w-16">优先级</th>
            <th class="px-4 py-2.5 font-medium">名称</th>
            <th class="px-4 py-2.5 font-medium">命令</th>
            <th class="px-4 py-2.5 font-medium">生效范围</th>
            <th class="px-4 py-2.5 font-medium w-20">状态</th>
            <th class="px-4 py-2.5 font-medium w-28 text-right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="r in rules"
            :key="r.id"
            class="border-t border-slate-100 hover:bg-slate-50"
          >
            <td class="px-4 py-2.5 text-slate-500">{{ r.priority }}</td>
            <td class="px-4 py-2.5">
              <div class="text-slate-800">{{ r.name }}</div>
              <div class="text-xs text-slate-400">
                更新于 {{ fmtTime(r.updated_at) }}
              </div>
            </td>
            <td class="px-4 py-2.5">
              <code
                class="text-xs text-slate-600 break-all block max-w-md"
                >{{ r.command }}</code
              >
            </td>
            <td class="px-4 py-2.5 text-slate-500 text-xs">所有任务</td>
            <td class="px-4 py-2.5">
              <button
                v-if="isAdmin"
                class="px-2 py-0.5 rounded-full text-xs"
                :class="
                  r.enabled
                    ? 'bg-emerald-100 text-emerald-700'
                    : 'bg-slate-100 text-slate-500'
                "
                :disabled="togglingId === r.id"
                @click="toggle(r)"
              >
                {{ r.enabled ? "启用" : "停用" }}
              </button>
              <span
                v-else
                class="px-2 py-0.5 rounded-full text-xs"
                :class="r.enabled ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-500'"
              >
                {{ r.enabled ? "启用" : "停用" }}
              </span>
            </td>
            <td class="px-4 py-2.5">
              <div v-if="isAdmin" class="flex items-center justify-end gap-1">
                <button
                  class="p-1.5 rounded hover:bg-slate-200 text-slate-500"
                  title="编辑"
                  @click="openEdit(r)"
                >
                  <Pencil :size="15" />
                </button>
                <button
                  class="p-1.5 rounded hover:bg-rose-100 text-rose-500"
                  title="删除"
                  @click="remove(r)"
                >
                  <Trash2 :size="15" />
                </button>
              </div>
              <div v-else class="text-right text-xs text-slate-300">—</div>
            </td>
          </tr>
        </tbody>
      </table>

      <div
        v-if="loading"
        class="py-12 flex items-center justify-center text-slate-400 gap-2"
      >
        <Loader2 :size="18" class="animate-spin" /> 加载中…
      </div>
      <div
        v-else-if="!rules.length"
        class="py-12 flex flex-col items-center justify-center text-slate-400 gap-2"
      >
        <Inbox :size="28" /> {{ isAdmin ? "暂无工具，点击「新建工具」添加" : "暂无后处理工具" }}
      </div>
    </div>

    <RuleDialog
      v-if="showDialog"
      :rule="editing"
      @close="showDialog = false"
      @saved="onSaved"
    />
  </div>
</template>
