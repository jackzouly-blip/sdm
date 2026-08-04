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
  SimProjectTemplateRef,
  SimTemplateRelease,
} from "@/api/types";
import { ArrowLeft, Boxes, ChevronRight, Eye, FolderCog, Loader2, Plus, Sparkles, Trash2 } from "lucide-vue-next";
import GeometryPanel from "./GeometryPanel.vue";
import RequirementPanel from "./RequirementPanel.vue";
import GeometryViewer from "./GeometryViewer.vue";
import AiSessionPanel from "./AiSessionPanel.vue";

const props = defineProps<{ pid: string }>();
const router = useRouter();

const showAi = ref(false);

const project = ref<SimProject | null>(null);
const targets = ref<SimTarget[]>([]);
const subjects = ref<SimSubject[]>([]);
const jobs = ref<SimJob[]>([]);
const results = ref<SimResult[]>([]);
// 只取条数用于 tab 角标；明细由 RequirementPanel 自己加载，避免两处各拉一份
const requirementCount = ref(0);
const loading = ref(true);
const error = ref("");

type Tab = "requirements" | "targets" | "subjects" | "templates" | "jobs" | "results";
const tab = ref<Tab>("targets");
const TABS: { key: Tab; label: string }[] = [
  // 需求排在最前:整条链是 需求 → 质量卡 → 几何 → 网格 → 工况 → 作业 → 结果
  { key: "requirements", label: "客户需求" },
  { key: "targets", label: "分析对象" },
  { key: "subjects", label: "工况" },
  { key: "templates", label: "引用模板" },
  { key: "jobs", label: "作业" },
  { key: "results", label: "结果" },
];
const counts = computed<Record<Tab, number>>(() => ({
  requirements: requirementCount.value,
  targets: targets.value.length,
  subjects: subjects.value.length,
  templates: templateRefs.value.length,
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
    const [p, t, s, j, r, reqs] = await Promise.all([
      simApi.getProject(props.pid),
      simApi.listTargets(props.pid),
      simApi.listSubjects(props.pid),
      simApi.listProjectJobs(props.pid),
      simApi.listProjectResults(props.pid),
      simApi.listRequirements(props.pid),
    ]);
    requirementCount.value = reqs.length;
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

const ctrlReleases = ref<SimTemplateRelease[]>([]);
const matReleases = ref<SimTemplateRelease[]>([]);

async function loadReleases() {
  try {
    const [cts, mts] = await Promise.all([
      simApi.listControlTemplates(), simApi.listMaterialTemplates(),
    ]);
    ctrlReleases.value = (await Promise.all(
      cts.map((t) => simApi.listTemplateReleases("control", t.id)))).flat();
    matReleases.value = (await Promise.all(
      mts.map((t) => simApi.listTemplateReleases("material", t.id)))).flat();
  } catch {
    /* 模板库不可用不阻断项目页 */
  }
}

// ── 引用模板(通用列表): 结算实例化按类别取最近一条; 类别可扩展 ──
const templateRefs = ref<SimProjectTemplateRef[]>([]);
const CATEGORIES = [
  { key: "material", label: "材料卡" },
  { key: "control", label: "控制卡" },
];
const addCategory = ref("material");
const addReleaseId = ref("");
const addBusy = ref(false);

const pickerReleases = computed(() =>
  addCategory.value === "control" ? ctrlReleases.value : matReleases.value);

async function loadTemplateRefs() {
  try {
    templateRefs.value = await simApi.listProjectTemplateRefs(props.pid);
  } catch {
    /* 列表加载失败不阻断项目页 */
  }
}

async function addFromLibrary() {
  if (!addReleaseId.value) return;
  addBusy.value = true;
  try {
    await simApi.addProjectTemplateRef(props.pid, addCategory.value, addReleaseId.value);
    addReleaseId.value = "";
    await loadTemplateRefs();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    addBusy.value = false;
  }
}

async function addFromLocal(ev: Event) {
  const input = ev.target as HTMLInputElement;
  const f = input.files?.[0];
  input.value = "";
  if (!f) return;
  addBusy.value = true;
  try {
    await simApi.uploadProjectTemplateRef(props.pid, addCategory.value, f);
    await loadTemplateRefs();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    addBusy.value = false;
  }
}

async function removeRef(rid: string) {
  if (!confirm("移除该引用？（不影响模板库与已生成的结算包）")) return;
  try {
    await simApi.deleteProjectTemplateRef(props.pid, rid);
    await loadTemplateRefs();
  } catch (e) {
    error.value = errMsg(e);
  }
}

// ── 实例化 run 目录: 每次计算独立目录(用户定的流程) ──
const matJob = ref<SimJob | null>(null);
const matDecks = ref<SimGeometry[]>([]);
const matDeckId = ref("");
const matBusy = ref(false);

async function openMaterialize(j: SimJob) {
  matJob.value = j;
  matDeckId.value = "";
  try {
    const all = await Promise.all(targets.value.map((tg) => simApi.listGeometries(tg.id)));
    matDecks.value = all.flat().filter((g) => g.source_type === "deck");
    if (matDecks.value.length === 1) matDeckId.value = matDecks.value[0].id;
  } catch (e) {
    error.value = errMsg(e);
  }
}

async function doMaterialize() {
  if (!matJob.value || !matDeckId.value) return;
  matBusy.value = true;
  try {
    await simApi.materializeJob(matJob.value.id, matDeckId.value);
    matJob.value = null;
    await load();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    matBusy.value = false;
  }
}

// ── 新建计算: 一步建工况(可选)+作业+实例化 run 目录 ──
const newRunOpen = ref(false);
const newRunSubjectId = ref("");
const newRunSubjectName = ref("");
const newRunDeckId = ref("");
const newRunBusy = ref(false);

async function openNewRun() {
  newRunOpen.value = true;
  newRunSubjectId.value = subjects.value[0]?.id ?? "";
  newRunSubjectName.value = "";
  newRunDeckId.value = "";
  try {
    const all = await Promise.all(targets.value.map((tg) => simApi.listGeometries(tg.id)));
    matDecks.value = all.flat().filter((g) => g.source_type === "deck");
    if (matDecks.value.length === 1) newRunDeckId.value = matDecks.value[0].id;
  } catch (e) {
    error.value = errMsg(e);
  }
}

async function doNewRun() {
  if (!newRunDeckId.value) return;
  newRunBusy.value = true;
  try {
    let sid = newRunSubjectId.value;
    if (!sid) {
      const name = newRunSubjectName.value.trim() || "气囊展开";
      const subj = await simApi.createSubject(props.pid, {
        name, subject_type: "airbag_deploy", solver_type: "lsdyna",
      });
      sid = subj.id;
    }
    const job = await simApi.createSimJob(sid);
    await simApi.materializeJob(job.id, newRunDeckId.value);
    newRunOpen.value = false;
    await load();
    tab.value = "jobs";
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    newRunBusy.value = false;
  }
}

function runDir(j: SimJob): string {
  return String((j.submit_payload as Record<string, unknown> | null)?.run_dir ?? "");
}

function catLabel(k: string) {
  return CATEGORIES.find((c) => c.key === k)?.label ?? k;
}

function fmt(ts: number | null) {
  return ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "—";
}

// 展开哪个分析对象的几何面板；同一时刻只展开一个，避免多个 3D 场景并存
const openTarget = ref<string | null>(null);
const previewing = ref<SimGeometry | null>(null);

/** 网格预览：几何仍要传（查看器要它兜底），但实际加载的是网格 GLB */
const previewMeshSrc = ref<{ src: string; title: string } | null>(null);

function onPreview(g: SimGeometry) {
  previewMeshSrc.value = null;
  previewing.value = g;
}

function onPreviewMesh(payload: { src: string; title: string; geometry: SimGeometry }) {
  previewMeshSrc.value = { src: payload.src, title: payload.title };
  previewing.value = payload.geometry;
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

onMounted(() => { load(); loadReleases(); loadTemplateRefs(); });
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
        <div class="flex items-center">
          <h1 class="text-lg font-semibold text-slate-800">{{ project.name }}</h1>
          <button
            class="ml-auto inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md border border-indigo-200 text-indigo-700 text-sm hover:bg-indigo-50"
            title="围绕本项目与 AI 对话：澄清需求、修改条目、调整质量卡（改动经确认后留痕）"
            @click="showAi = !showAi"
          >
            <Sparkles :size="14" /> AI 会话
          </button>
        </div>
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

      <!-- 客户需求与质量卡 -->
      <RequirementPanel v-if="tab === 'requirements'" :pid="pid" />

      <!-- 分析对象 -->
      <div v-else-if="tab === 'targets'">
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
                    @preview-mesh="onPreviewMesh"
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
      <div v-else-if="tab === 'templates'">
        <!-- 引用模板: 每次计算实例化时按类别取**最近添加**的一条。
             library=模板库发布快照(不可变), local=本地上传原文。类别后续可扩展 -->
        <div class="flex flex-wrap items-center gap-2 mb-3 text-xs">
          <select v-model="addCategory" class="rounded border px-2 py-1">
            <option v-for="c in CATEGORIES" :key="c.key" :value="c.key">{{ c.label }}</option>
          </select>
          <select v-model="addReleaseId" class="rounded border px-2 py-1 min-w-56">
            <option value="">从库选择已发布版本…</option>
            <option v-for="r in pickerReleases" :key="r.id" :value="r.id">
              {{ r.name }} v{{ r.version_no }}
            </option>
          </select>
          <button class="rounded bg-slate-800 px-3 py-1 text-white disabled:opacity-50"
                  :disabled="!addReleaseId || addBusy" @click="addFromLibrary">从库添加</button>
          <label class="rounded border px-3 py-1 cursor-pointer hover:bg-slate-50"
                 :class="addBusy ? 'opacity-50 pointer-events-none' : ''">
            本地上传…
            <input type="file" accept=".k,.key,.dyn,.inc" class="hidden" @change="addFromLocal" />
          </label>
        </div>
        <p v-if="!templateRefs.length" class="text-sm text-slate-400 py-6 text-center">
          尚未引用任何模板 —— 结算组装将使用内置默认（工程师 5P-BAG 那套）
        </p>
        <table v-else class="w-full text-sm">
          <thead>
            <tr class="text-left text-xs text-slate-500 border-b">
              <th class="py-1.5 pr-3">类别</th>
              <th class="py-1.5 pr-3">名称</th>
              <th class="py-1.5 pr-3">来源</th>
              <th class="py-1.5 pr-3">添加人 / 时间</th>
              <th class="py-1.5"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in templateRefs" :key="r.id" class="border-b last:border-0">
              <td class="py-1.5 pr-3">{{ catLabel(r.category) }}</td>
              <td class="py-1.5 pr-3">
                <a v-if="r.release_id" :href="simApi.templateReleaseUrl(r.release_id)"
                   class="text-indigo-700 hover:underline">{{ r.name }}</a>
                <span v-else>{{ r.name }}</span>
              </td>
              <td class="py-1.5 pr-3 text-xs">
                <span :class="r.source === 'library'
                  ? 'px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-700'
                  : 'px-1.5 py-0.5 rounded bg-slate-100 text-slate-600'">
                  {{ r.source === "library" ? "模板库" : "本地上传" }}
                </span>
              </td>
              <td class="py-1.5 pr-3 text-xs text-slate-500">
                {{ r.created_by }} · {{ fmt(r.created_at) }}
              </td>
              <td class="py-1.5 text-right">
                <button class="text-xs text-red-600 hover:underline" @click="removeRef(r.id)">移除</button>
              </td>
            </tr>
          </tbody>
        </table>
        <p class="mt-3 text-[11px] text-slate-400">
          同类别有多条时，实例化取最近添加的一条。引用模板库版本是不可变快照——库里继续编辑不影响本项目。
        </p>
      </div>

      <div v-else-if="tab === 'jobs'">
        <div class="mb-3">
          <button class="rounded bg-slate-800 px-3 py-1.5 text-white text-sm"
                  @click="openNewRun()">新建计算（实例化 run 目录）</button>
        </div>
        <p v-if="!jobs.length" class="text-sm text-slate-400 py-8 text-center">
          还没有计算。点上方「新建计算」：选网格 deck 一步生成 run 目录，
          随后在「作业提交」页以该目录 + main.key 入队。
        </p>
        <table v-else class="w-full text-sm">
          <thead class="text-slate-500 border-b border-slate-200">
            <tr>
              <th class="text-left font-medium py-2">工况</th>
              <th class="text-left font-medium py-2">HPC 作业号</th>
              <th class="text-left font-medium py-2">方式</th>
              <th class="text-left font-medium py-2">状态</th>
              <th class="text-left font-medium py-2">投递时间</th>
              <th class="text-left font-medium py-2">运行目录</th>
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
              <td class="py-2 text-xs">
                <span v-if="runDir(j)" class="font-mono text-slate-600 break-all"
                      title="提交时在「作业提交」把初始目录填这里、输入文件填 main.key">
                  {{ runDir(j) }}</span>
                <button v-else-if="!j.hpc_jobid"
                        class="rounded border px-2 py-0.5 hover:bg-slate-50"
                        @click="openMaterialize(j)">实例化…</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="matJob" class="fixed inset-0 z-50 bg-black/30 flex items-center justify-center"
           @click.self="matJob = null">
        <div class="bg-white rounded-lg shadow-xl p-4 w-96 text-sm">
          <div class="font-medium mb-2">实例化运行目录</div>
          <p class="text-xs text-slate-500 mb-2">
            将在项目工作目录下建 runs/&lt;作业号&gt;/，放入所选网格 deck、
            引用模板（控制卡/材料卡/起爆卡）与 main.key。每次计算一个独立目录。
          </p>
          <select v-model="matDeckId" class="w-full rounded border px-2 py-1 mb-3">
            <option value="">选择网格 deck…</option>
            <option v-for="g in matDecks" :key="g.id" :value="g.id">
              {{ g.source_file?.name }} (v{{ g.version_no }})
            </option>
          </select>
          <div class="flex justify-end gap-2">
            <button class="rounded border px-3 py-1" @click="matJob = null">取消</button>
            <button class="rounded bg-slate-800 px-3 py-1 text-white disabled:opacity-50"
                    :disabled="!matDeckId || matBusy" @click="doMaterialize">实例化</button>
          </div>
        </div>
      </div>

      <div v-if="newRunOpen" class="fixed inset-0 z-50 bg-black/30 flex items-center justify-center"
           @click.self="newRunOpen = false">
        <div class="bg-white rounded-lg shadow-xl p-4 w-[26rem] text-sm">
          <div class="font-medium mb-2">新建计算</div>
          <label class="block text-xs text-slate-500 mb-1">工况</label>
          <select v-if="subjects.length" v-model="newRunSubjectId"
                  class="w-full rounded border px-2 py-1 mb-2">
            <option v-for="sj in subjects" :key="sj.id" :value="sj.id">{{ sj.name }}</option>
            <option value="">新建工况…</option>
          </select>
          <input v-if="!subjects.length || !newRunSubjectId" v-model="newRunSubjectName"
                 placeholder="工况名（默认：气囊展开）"
                 class="w-full rounded border px-2 py-1 mb-2" />
          <label class="block text-xs text-slate-500 mb-1">网格 deck</label>
          <select v-model="newRunDeckId" class="w-full rounded border px-2 py-1 mb-3">
            <option value="">选择网格 deck…</option>
            <option v-for="g in matDecks" :key="g.id" :value="g.id">
              {{ g.source_file?.name }} (v{{ g.version_no }})
            </option>
          </select>
          <p v-if="!matDecks.length" class="text-xs text-amber-600 mb-2">
            本项目还没有网格 deck——先在「分析对象」里对平面图点「生成网格」。
          </p>
          <div class="flex justify-end gap-2">
            <button class="rounded border px-3 py-1" @click="newRunOpen = false">取消</button>
            <button class="rounded bg-slate-800 px-3 py-1 text-white disabled:opacity-50"
                    :disabled="!newRunDeckId || newRunBusy" @click="doNewRun">创建并实例化</button>
          </div>
        </div>
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

    <!-- 几何/网格预览：同一个查看器，网格走 src 覆盖(带真实单元边线的 GLB) -->
    <GeometryViewer
      v-if="previewing"
      :geometry="previewing"
      :src="previewMeshSrc?.src"
      :title="previewMeshSrc?.title"
      @close="previewing = null; previewMeshSrc = null"
    />

    <!-- AI 会话抽屉：消息流是主数据，改动全部走提案-确认 -->
    <AiSessionPanel v-if="showAi" :pid="pid" @close="showAi = false" />
  </div>
</template>
