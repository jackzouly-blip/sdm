<script setup lang="ts">
/**
 * 仿真项目详情：分析对象 / 工况 / 作业 / 结果四个视角。
 *
 * 作业一栏展示 hpc_jobid —— 执行仍在现有 HPC 链路，这里只做编排侧的关联展示，
 * 点进去即跳到算力管理的任务详情页。
 */
import { computed, onMounted, ref } from "vue";
import { useRouter, type RouteLocationRaw } from "vue-router";
import { simApi, errMsg } from "@/api";
import type {
  SimGeometry,
  SimJob,
  SimProject,
  SimResult,
  SimSubject,
  SimTarget,
} from "@/api/types";
import { ArrowLeft, Boxes, ChevronRight, Eye, FolderCog, Loader2, Plus, Trash2 } from "lucide-vue-next";
import GeometryPanel from "./GeometryPanel.vue";
import GeometryViewer from "./GeometryViewer.vue";

const props = defineProps<{ pid: string }>();
const router = useRouter();

const project = ref<SimProject | null>(null);
const targets = ref<SimTarget[]>([]);
const subjects = ref<SimSubject[]>([]);
const jobs = ref<SimJob[]>([]);
const results = ref<SimResult[]>([]);
const loading = ref(true);
const error = ref("");

type Tab = "targets" | "subjects" | "jobs" | "results";
const tab = ref<Tab>("targets");
const TABS: { key: Tab; label: string }[] = [
  { key: "targets", label: "分析对象" },
  { key: "subjects", label: "工况" },
  { key: "jobs", label: "作业" },
  { key: "results", label: "结果" },
];
const counts = computed<Record<Tab, number>>(() => ({
  targets: targets.value.length,
  subjects: subjects.value.length,
  jobs: jobs.value.length,
  results: results.value.length,
}));

const STATUS_STYLE: Record<string, string> = {
  draft: "bg-slate-100 text-slate-500",
  ready: "bg-sky-50 text-sky-700",
  submitted: "bg-sky-50 text-sky-700",
  running: "bg-amber-50 text-amber-700",
  done: "bg-emerald-50 text-emerald-700",
  failed: "bg-rose-50 text-rose-700",
};
const STATUS_TEXT: Record<string, string> = {
  draft: "草稿",
  ready: "就绪",
  submitted: "已投递",
  running: "运行中",
  done: "完成",
  failed: "失败",
};

async function load() {
  loading.value = true;
  error.value = "";
  try {
    const [p, t, s, j, r] = await Promise.all([
      simApi.getProject(props.pid),
      simApi.listTargets(props.pid),
      simApi.listSubjects(props.pid),
      simApi.listProjectJobs(props.pid),
      simApi.listProjectResults(props.pid),
    ]);
    project.value = p;
    targets.value = t;
    subjects.value = s;
    jobs.value = j;
    results.value = r;
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}

// --- 新建分析对象 ---
const newTarget = ref({ name: "", target_type: "part" });
async function addTarget() {
  if (!newTarget.value.name.trim()) return;
  try {
    await simApi.createTarget(props.pid, {
      name: newTarget.value.name.trim(),
      target_type: newTarget.value.target_type,
    });
    newTarget.value.name = "";
    targets.value = await simApi.listTargets(props.pid);
  } catch (e) {
    error.value = errMsg(e);
  }
}
async function delTarget(t: SimTarget) {
  if (!confirm(`删除分析对象「${t.name}」？其几何与网格版本将一并删除。`)) return;
  try {
    await simApi.deleteTarget(t.id);
    targets.value = await simApi.listTargets(props.pid);
  } catch (e) {
    error.value = errMsg(e);
  }
}

// --- 新建工况 ---
const newSubject = ref({ name: "", subject_type: "crash", solver_type: "" });
async function addSubject() {
  if (!newSubject.value.name.trim()) return;
  try {
    await simApi.createSubject(props.pid, {
      name: newSubject.value.name.trim(),
      subject_type: newSubject.value.subject_type,
      solver_type: newSubject.value.solver_type || project.value?.default_solver || "ls-dyna",
    });
    newSubject.value.name = "";
    subjects.value = await simApi.listSubjects(props.pid);
  } catch (e) {
    error.value = errMsg(e);
  }
}
async function delSubject(s: SimSubject) {
  if (!confirm(`删除工况「${s.name}」？其作业与结果将一并删除。`)) return;
  try {
    await simApi.deleteSubject(s.id);
    subjects.value = await simApi.listSubjects(props.pid);
    jobs.value = await simApi.listProjectJobs(props.pid);
  } catch (e) {
    error.value = errMsg(e);
  }
}

// 工作目录：几何/网格/产物的落盘位置,由系统配置(HPC_SIM_WORKDIR_ROOT)按
// <根>/<属主>/<项目 id> 自动派生,不需要人工填。这里给的是**覆盖**入口——
// 个别项目要指向一个既有分析目录时才用得上。
const editingWorkdir = ref(false);
const workdirDraft = ref("");
const savingWorkdir = ref(false);

function startEditWorkdir() {
  workdirDraft.value = project.value?.workdir ?? "";
  editingWorkdir.value = true;
}

async function saveWorkdir() {
  if (!project.value) return;
  savingWorkdir.value = true;
  error.value = "";
  try {
    project.value = await simApi.updateProject(project.value.id, {
      workdir: workdirDraft.value.trim() || null,
    });
    editingWorkdir.value = false;
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    savingWorkdir.value = false;
  }
}

function fmt(ts: number | null) {
  return ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "—";
}

// 展开哪个分析对象的几何面板；同一时刻只展开一个，避免多个 3D 场景并存
const openTarget = ref<string | null>(null);
const previewing = ref<SimGeometry | null>(null);

function onPreview(g: SimGeometry) {
  previewing.value = g;
}

/**
 * 结果 → 查看器路由。按 result_type 分发：目前只有碰撞（d3plot）有实现，
 * 后续 CFD / NVH / 疲劳各自注册后在此扩展，其余类型返回 null（不显示入口）。
 *
 * 只对 d3plot 家族主文件给入口——d3plot01/02 是同一模型的后续状态帧，
 * 查看器从主文件一并加载，逐个给链接只会让人误以为要分别打开。
 */
function viewerFor(r: SimResult): RouteLocationRaw | null {
  if (r.result_type !== "d3plot") return null;
  const name = r.file_path.split(/[/\\]/).pop() ?? "";
  if (name.toLowerCase() !== "d3plot") return null;
  return { name: "d3plot", query: { path: r.file_path } };
}

onMounted(load);
</script>

<template>
  <div class="w-full">
    <button
      class="flex items-center gap-1 text-sm text-slate-500 hover:text-slate-700 mb-3"
      @click="router.push({ name: 'sim-projects' })"
    >
      <ArrowLeft :size="15" /> 返回项目列表
    </button>

    <div v-if="error" class="mb-3 px-3 py-2 rounded-md bg-rose-50 text-rose-700 text-sm">
      {{ error }}
    </div>

    <div v-if="loading" class="flex items-center gap-2 text-slate-500 text-sm py-10 justify-center">
      <Loader2 :size="18" class="animate-spin" /> 加载中…
    </div>

    <template v-else-if="project">
      <div class="mb-4">
        <h1 class="text-lg font-semibold text-slate-800">{{ project.name }}</h1>
        <p class="text-sm text-slate-500 mt-0.5">
          {{ project.description || "无说明" }}
        </p>
        <div class="flex flex-wrap gap-2 mt-2 text-xs text-slate-500">
          <span class="px-1.5 py-0.5 rounded bg-slate-100">属主 {{ project.owner }}</span>
          <span v-if="project.default_solver" class="px-1.5 py-0.5 rounded bg-slate-100">
            求解器 {{ project.default_solver }}
          </span>
          <span class="px-1.5 py-0.5 rounded bg-slate-100">单位制 {{ project.unit_system }}</span>
          <span v-if="project.dbit_project_code" class="px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-700">
            dbit {{ project.dbit_project_code }}
          </span>
        </div>

        <!-- 工作目录:未设置时导入数模必失败,所以缺失要显眼、且能就地补 -->
        <div class="flex items-center gap-2 mt-2 text-xs">
          <span class="text-slate-500 shrink-0">工作目录</span>
          <template v-if="editingWorkdir">
            <input
              v-model="workdirDraft"
              class="flex-1 max-w-xl px-2 py-1 border border-slate-300 rounded font-mono"
              placeholder="如：/caedata/project-ext/hpc-portal/sdm/x90-seat-crash"
              @keyup.enter="saveWorkdir"
            />
            <button
              class="px-2 py-1 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
              :disabled="savingWorkdir"
              @click="saveWorkdir"
            >
              保存
            </button>
            <button class="px-2 py-1 rounded text-slate-500 hover:bg-slate-100" @click="editingWorkdir = false">
              取消
            </button>
          </template>
          <template v-else>
            <code v-if="project.workdir" class="px-1.5 py-0.5 rounded bg-slate-100 text-slate-600">
              {{ project.workdir }}
            </code>
            <span v-else class="px-1.5 py-0.5 rounded bg-slate-100 text-slate-500">
              按系统配置自动创建
            </span>
            <button
              class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-slate-500 hover:bg-slate-100"
              @click="startEditWorkdir"
            >
              <FolderCog :size="12" /> {{ project.workdir ? "覆盖" : "指定其他目录" }}
            </button>
          </template>
        </div>
      </div>

      <div class="flex gap-1 border-b border-slate-200 mb-4">
        <button
          v-for="t in TABS"
          :key="t.key"
          class="px-3 py-2 text-sm border-b-2 -mb-px transition"
          :class="
            tab === t.key
              ? 'border-blue-600 text-blue-700 font-medium'
              : 'border-transparent text-slate-500 hover:text-slate-700'
          "
          @click="tab = t.key"
        >
          {{ t.label }}
          <span class="text-xs text-slate-400">({{ counts[t.key] }})</span>
        </button>
      </div>

      <!-- 分析对象 -->
      <div v-if="tab === 'targets'">
        <div class="flex gap-2 mb-3">
          <input
            v-model="newTarget.name"
            class="flex-1 max-w-xs px-3 py-1.5 border border-slate-300 rounded-md text-sm"
            placeholder="分析对象名称"
            @keyup.enter="addTarget"
          />
          <select
            v-model="newTarget.target_type"
            class="px-2 py-1.5 border border-slate-300 rounded-md text-sm"
          >
            <option value="part">零件</option>
            <option value="assembly">装配</option>
            <option value="system">系统</option>
          </select>
          <button
            class="flex items-center gap-1 px-3 py-1.5 rounded-md bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50"
            :disabled="!newTarget.name.trim()"
            @click="addTarget"
          >
            <Plus :size="15" /> 添加
          </button>
        </div>
        <p v-if="!targets.length" class="text-sm text-slate-400 py-8 text-center">
          还没有分析对象
        </p>
        <table v-else class="w-full text-sm">
          <thead class="text-slate-500 border-b border-slate-200">
            <tr>
              <th class="text-left font-medium py-2">名称</th>
              <th class="text-left font-medium py-2">类型</th>
              <th class="text-left font-medium py-2">创建时间</th>
              <th class="w-10"></th>
            </tr>
          </thead>
          <tbody>
            <template v-for="t in targets" :key="t.id">
              <tr
                class="border-b border-slate-100 hover:bg-slate-50 cursor-pointer"
                @click="openTarget = openTarget === t.id ? null : t.id"
              >
                <td class="py-2 text-slate-800">
                  <ChevronRight
                    :size="14"
                    class="inline -mt-0.5 mr-1 text-slate-400 transition-transform"
                    :class="openTarget === t.id ? 'rotate-90' : ''"
                  />
                  {{ t.name }}
                </td>
                <td class="py-2 text-slate-500">{{ t.target_type }}</td>
                <td class="py-2 text-slate-500">{{ fmt(t.created_at) }}</td>
                <td class="py-2">
                  <button
                    class="p-1 rounded hover:bg-rose-50 text-slate-400 hover:text-rose-600"
                    @click.stop="delTarget(t)"
                  >
                    <Trash2 :size="14" />
                  </button>
                </td>
              </tr>
              <tr v-if="openTarget === t.id">
                <td colspan="4" class="bg-slate-50/60 px-4 py-3">
                  <GeometryPanel
                    :target-id="t.id"
                    :target-name="t.name"
                    @preview="onPreview"
                  />
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>

      <!-- 工况 -->
      <div v-else-if="tab === 'subjects'">
        <div class="flex gap-2 mb-3">
          <input
            v-model="newSubject.name"
            class="flex-1 max-w-xs px-3 py-1.5 border border-slate-300 rounded-md text-sm"
            placeholder="工况名称，如 正碰 50km/h"
            @keyup.enter="addSubject"
          />
          <input
            v-model="newSubject.subject_type"
            class="w-28 px-3 py-1.5 border border-slate-300 rounded-md text-sm"
            placeholder="工况类型"
          />
          <input
            v-model="newSubject.solver_type"
            class="w-32 px-3 py-1.5 border border-slate-300 rounded-md text-sm"
            :placeholder="project.default_solver || 'ls-dyna'"
          />
          <button
            class="flex items-center gap-1 px-3 py-1.5 rounded-md bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50"
            :disabled="!newSubject.name.trim()"
            @click="addSubject"
          >
            <Plus :size="15" /> 添加
          </button>
        </div>
        <p v-if="!subjects.length" class="text-sm text-slate-400 py-8 text-center">
          还没有工况
        </p>
        <table v-else class="w-full text-sm">
          <thead class="text-slate-500 border-b border-slate-200">
            <tr>
              <th class="text-left font-medium py-2">名称</th>
              <th class="text-left font-medium py-2">类型</th>
              <th class="text-left font-medium py-2">求解器</th>
              <th class="text-left font-medium py-2">状态</th>
              <th class="w-10"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="s in subjects" :key="s.id" class="border-b border-slate-100">
              <td class="py-2 text-slate-800">{{ s.name }}</td>
              <td class="py-2 text-slate-500">{{ s.subject_type }}</td>
              <td class="py-2 text-slate-500">{{ s.solver_type }}</td>
              <td class="py-2">
                <span
                  class="px-1.5 py-0.5 rounded text-xs"
                  :class="STATUS_STYLE[s.status]"
                  >{{ STATUS_TEXT[s.status] ?? s.status }}</span
                >
              </td>
              <td class="py-2">
                <button
                  class="p-1 rounded hover:bg-rose-50 text-slate-400 hover:text-rose-600"
                  @click="delSubject(s)"
                >
                  <Trash2 :size="14" />
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 作业 -->
      <div v-else-if="tab === 'jobs'">
        <p v-if="!jobs.length" class="text-sm text-slate-400 py-8 text-center">
          还没有作业。作业由编排产生，执行仍在算力管理的 HPC 链路。
        </p>
        <table v-else class="w-full text-sm">
          <thead class="text-slate-500 border-b border-slate-200">
            <tr>
              <th class="text-left font-medium py-2">工况</th>
              <th class="text-left font-medium py-2">HPC 作业号</th>
              <th class="text-left font-medium py-2">方式</th>
              <th class="text-left font-medium py-2">状态</th>
              <th class="text-left font-medium py-2">投递时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="j in jobs" :key="j.id" class="border-b border-slate-100">
              <td class="py-2 text-slate-800">{{ j.subject_name }}</td>
              <td class="py-2">
                <RouterLink
                  v-if="j.hpc_jobid"
                  :to="{ name: 'job-detail', params: { jobid: j.hpc_jobid } }"
                  class="text-blue-600 hover:underline font-mono text-xs"
                  >{{ j.hpc_jobid }}</RouterLink
                >
                <span v-else class="text-slate-400">未投递</span>
              </td>
              <td class="py-2 text-slate-500">{{ j.submit_mode }}</td>
              <td class="py-2">
                <span
                  class="px-1.5 py-0.5 rounded text-xs"
                  :class="STATUS_STYLE[j.status]"
                  >{{ STATUS_TEXT[j.status] ?? j.status }}</span
                >
              </td>
              <td class="py-2 text-slate-500">{{ fmt(j.submitted_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 结果 -->
      <div v-else>
        <p v-if="!results.length" class="text-sm text-slate-400 py-8 text-center">
          还没有结果
        </p>
        <table v-else class="w-full text-sm">
          <thead class="text-slate-500 border-b border-slate-200">
            <tr>
              <th class="text-left font-medium py-2">工况</th>
              <th class="text-left font-medium py-2">结果类型</th>
              <th class="text-left font-medium py-2">文件</th>
              <th class="text-left font-medium py-2">生成时间</th>
              <th class="w-24"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in results" :key="r.id" class="border-b border-slate-100">
              <td class="py-2 text-slate-800">{{ r.subject_name }}</td>
              <td class="py-2">
                <span class="px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 text-xs">
                  <Boxes :size="11" class="inline -mt-0.5" /> {{ r.result_type }}
                </span>
              </td>
              <td class="py-2 text-slate-500 font-mono text-xs truncate max-w-md">
                {{ r.file_path }}
              </td>
              <td class="py-2 text-slate-500">{{ fmt(r.created_at) }}</td>
              <td class="py-2">
                <!-- 目前只有碰撞(d3plot)有查看器实现；其余类型登记后可下载，
                     待各专业查看器插件注册后这里按 result_type 分发 -->
                <RouterLink
                  v-if="viewerFor(r)"
                  :to="viewerFor(r)!"
                  class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs text-blue-600 hover:bg-blue-50"
                >
                  <Eye :size="12" /> 在线查看
                </RouterLink>
                <span v-else class="text-xs text-slate-300">暂无查看器</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <!-- 几何预览：轻量化产物就绪的版本才会走到这里 -->
    <GeometryViewer
      v-if="previewing"
      :geometry="previewing"
      @close="previewing = null"
    />
  </div>
</template>
