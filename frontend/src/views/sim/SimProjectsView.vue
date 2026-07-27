<script setup lang="ts">
/** 仿真项目列表：SDM 的入口页。 */
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { simApi, errMsg } from "@/api";
import type { SimProject } from "@/api/types";
import { FlaskConical, Plus, Trash2, Loader2 } from "lucide-vue-next";

const router = useRouter();
const projects = ref<SimProject[]>([]);
const loading = ref(true);
const error = ref("");

const showCreate = ref(false);
const creating = ref(false);
const form = ref({ name: "", description: "", default_solver: "" });

async function load() {
  loading.value = true;
  error.value = "";
  try {
    projects.value = await simApi.listProjects();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}

async function create() {
  if (!form.value.name.trim()) return;
  creating.value = true;
  try {
    const p = await simApi.createProject({
      name: form.value.name.trim(),
      description: form.value.description.trim() || null,
      default_solver: form.value.default_solver.trim() || null,
      // workdir 不在这里问:由后端按系统配置(HPC_SIM_WORKDIR_ROOT)派生
    });
    showCreate.value = false;
    form.value = { name: "", description: "", default_solver: "" };
    router.push({ name: "sim-project-detail", params: { pid: p.id } });
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    creating.value = false;
  }
}

async function remove(p: SimProject) {
  if (!confirm(`删除仿真项目「${p.name}」？其下的分析对象、工况、作业与结果将一并删除。`))
    return;
  try {
    await simApi.deleteProject(p.id);
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
    <div class="flex items-center gap-3 mb-4">
      <h1 class="text-lg font-semibold text-slate-800">仿真项目</h1>
      <button
        class="ml-auto flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-blue-600 text-white text-sm hover:bg-blue-700 transition"
        @click="showCreate = true"
      >
        <Plus :size="16" /> 新建项目
      </button>
    </div>

    <div v-if="error" class="mb-3 px-3 py-2 rounded-md bg-rose-50 text-rose-700 text-sm">
      {{ error }}
    </div>

    <div v-if="loading" class="flex items-center gap-2 text-slate-500 text-sm py-10 justify-center">
      <Loader2 :size="18" class="animate-spin" /> 加载中…
    </div>

    <div
      v-else-if="!projects.length"
      class="text-center py-16 text-slate-400 border border-dashed border-slate-200 rounded-lg"
    >
      <FlaskConical :size="32" class="mx-auto mb-2 opacity-40" />
      <p class="text-sm">还没有仿真项目</p>
      <p class="text-xs mt-1">新建一个项目，把分析对象、工况与作业组织起来</p>
    </div>

    <div v-else class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      <div
        v-for="p in projects"
        :key="p.id"
        class="group border border-slate-200 rounded-lg p-4 bg-white hover:border-blue-300 hover:shadow-sm transition cursor-pointer"
        @click="router.push({ name: 'sim-project-detail', params: { pid: p.id } })"
      >
        <div class="flex items-start gap-2">
          <FlaskConical :size="18" class="text-blue-600 mt-0.5 shrink-0" />
          <div class="min-w-0 flex-1">
            <div class="font-medium text-slate-800 truncate">{{ p.name }}</div>
            <div class="text-xs text-slate-400 mt-0.5">
              {{ p.owner }} · {{ fmt(p.updated_at) }}
            </div>
          </div>
          <button
            class="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-rose-50 text-slate-400 hover:text-rose-600 transition"
            title="删除项目"
            @click.stop="remove(p)"
          >
            <Trash2 :size="15" />
          </button>
        </div>

        <p v-if="p.description" class="text-sm text-slate-500 mt-2 line-clamp-2">
          {{ p.description }}
        </p>

        <div class="flex items-center gap-3 mt-3 text-xs text-slate-500">
          <span>分析对象 <b class="text-slate-700">{{ p.stats?.targets ?? 0 }}</b></span>
          <span>工况 <b class="text-slate-700">{{ p.stats?.subjects ?? 0 }}</b></span>
          <span>作业 <b class="text-slate-700">{{ p.stats?.jobs ?? 0 }}</b></span>
          <span
            v-if="p.default_solver"
            class="ml-auto px-1.5 py-0.5 rounded bg-slate-100 text-slate-600"
            >{{ p.default_solver }}</span
          >
        </div>
      </div>
    </div>

    <!-- 新建项目 -->
    <div
      v-if="showCreate"
      class="fixed inset-0 z-30 bg-black/30 flex items-center justify-center p-4"
      @click.self="showCreate = false"
    >
      <div class="bg-white rounded-lg shadow-xl w-full max-w-md p-5">
        <h2 class="font-semibold text-slate-800 mb-4">新建仿真项目</h2>
        <label class="block text-sm text-slate-600 mb-1">项目名称</label>
        <input
          v-model="form.name"
          class="w-full px-3 py-2 border border-slate-300 rounded-md text-sm mb-3 focus:outline-none focus:ring-2 focus:ring-blue-200"
          placeholder="如：整椅正碰分析"
          @keyup.enter="create"
        />
        <label class="block text-sm text-slate-600 mb-1">默认求解器</label>
        <input
          v-model="form.default_solver"
          class="w-full px-3 py-2 border border-slate-300 rounded-md text-sm mb-3 focus:outline-none focus:ring-2 focus:ring-blue-200"
          placeholder="如：ls-dyna"
        />
        <label class="block text-sm text-slate-600 mb-1">说明（可选）</label>
        <textarea
          v-model="form.description"
          rows="3"
          class="w-full px-3 py-2 border border-slate-300 rounded-md text-sm mb-4 focus:outline-none focus:ring-2 focus:ring-blue-200"
        ></textarea>
        <div class="flex justify-end gap-2">
          <button
            class="px-3 py-1.5 rounded-md text-sm text-slate-600 hover:bg-slate-100"
            @click="showCreate = false"
          >
            取消
          </button>
          <button
            class="px-3 py-1.5 rounded-md text-sm bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
            :disabled="creating || !form.name.trim()"
            @click="create"
          >
            {{ creating ? "创建中…" : "创建" }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
