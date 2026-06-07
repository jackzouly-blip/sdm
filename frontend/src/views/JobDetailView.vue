<script setup lang="ts">
import { ref, onMounted } from "vue";
import { useRouter } from "vue-router";
import { api, errMsg } from "@/api";
import type { JobDetail, ExtractRule } from "@/api/types";
import { fmtTime, jobBadge, pbsStateLabel, fmtBytes } from "@/lib/format";
import FileBrowser from "@/components/FileBrowser.vue";
import { ArrowLeft, Loader2, PlayCircle, Box, Trash2, Ban } from "lucide-vue-next";

const props = defineProps<{ jobid: string }>();
const router = useRouter();

const job = ref<JobDetail | null>(null);
const loading = ref(true);
const error = ref("");
const running = ref(false);
const runMsg = ref("");
const d3plotPath = ref<string | null>(null);
const d3plotCount = ref(0); // d3plot 文件总数(主文件 + 状态文件)，反映仿真进度
const rules = ref<ExtractRule[]>([]); // 全部数据提取规则（此处不做按任务匹配，直接列出全部）
const cancelling = ref(false);

// 终止运行/排队中的任务（qdel）
async function cancelJob() {
  if (!job.value) return;
  if (
    !window.confirm(
      `确定终止任务「${job.value.name || job.value.short_id}」吗？此操作不可恢复。`
    )
  )
    return;
  cancelling.value = true;
  error.value = "";
  try {
    await api.cancelJob(job.value.jobid);
    job.value = await api.getJob(props.jobid); // 刷新状态
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    cancelling.value = false;
  }
}
const cleaning = ref(false);
const cleanMsg = ref("");

onMounted(async () => {
  try {
    job.value = await api.getJob(props.jobid);
    // 检测工作目录是否有 d3plot（碰撞仿真结果），有则提供 3D 查看入口
    if (job.value.workdir) {
      try {
        const r = await api.d3plotFind(job.value.workdir);
        if (r.found.length) d3plotPath.value = r.found[0].path;
        d3plotCount.value = r.found.length + r.n_state_files;
      } catch {
        /* 无 d3plot 或无权限，忽略 */
      }
    }
    // 拉取全部数据提取规则用于展示（不按任务匹配，列出所有）
    try {
      rules.value = await api.listRules();
    } catch {
      /* 无权限或暂无规则，忽略 */
    }
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
});

function openD3plot() {
  if (d3plotPath.value) router.push({ name: "d3plot", query: { path: d3plotPath.value } });
}

// 手动触发提取：按当前规则重新匹配并派发，结果在「打包记录」页查看。
async function runExtract() {
  if (!job.value) return;
  running.value = true;
  runMsg.value = "";
  try {
    const { dispatched } = await api.runExtract(job.value.jobid);
    runMsg.value = dispatched
      ? `已派发 ${dispatched} 条提取任务，可在「打包记录」查看进度`
      : "没有匹配的提取规则";
    job.value = await api.getJob(props.jobid);
  } catch (e) {
    runMsg.value = errMsg(e);
  } finally {
    running.value = false;
  }
}

// 清理工作目录下 disk* / mes* / scr* 临时文件（破坏性，需二次确认）。
async function cleanFiles() {
  if (!job.value) return;
  if (
    !window.confirm(
      "将删除该任务工作目录下所有 disk*、mes*、scr* 文件，操作不可恢复。确定继续？"
    )
  )
    return;
  cleaning.value = true;
  cleanMsg.value = "";
  try {
    const r = await api.cleanupJob(job.value.jobid);
    cleanMsg.value = r.count
      ? `已清理 ${r.count} 个文件，释放 ${fmtBytes(r.freed_bytes)}` +
        (r.errors.length ? `（${r.errors.length} 个失败）` : "")
      : "没有匹配的可清理文件";
  } catch (e) {
    cleanMsg.value = errMsg(e);
  } finally {
    cleaning.value = false;
  }
}

// 详情字段表（标签 + 取值）。
function rows(j: JobDetail) {
  return [
    { label: "完整 ID", value: j.jobid },
    { label: "名称", value: j.name || "—" },
    { label: "属主", value: j.owner },
    { label: "队列", value: j.queue || "—" },
    { label: "PBS 状态", value: pbsStateLabel(j.pbs_state) },
    { label: "执行节点", value: j.exec_host || "—" },
    { label: "节点数", value: j.nodes || "—" },
    { label: "提交时间", value: fmtTime(j.submit_ts) },
    { label: "开始时间", value: fmtTime(j.start_ts) },
    { label: "结束时间", value: fmtTime(j.end_ts) },
    { label: "已用时长", value: j.walltime_used || "—" },
    { label: "时长上限", value: j.walltime_limit || "—" },
    {
      label: "退出码",
      value: j.exit_status === null ? "—" : String(j.exit_status),
    },
  ];
}
</script>

<template>
  <div class="max-w-6xl mx-auto">
    <button
      class="flex items-center gap-1 text-sm text-slate-500 hover:text-slate-700 mb-4"
      @click="router.push({ name: 'jobs' })"
    >
      <ArrowLeft :size="16" /> 返回任务列表
    </button>

    <div
      v-if="loading"
      class="py-12 flex items-center justify-center text-slate-400 gap-2"
    >
      <Loader2 :size="18" class="animate-spin" /> 加载中…
    </div>
    <p v-else-if="error" class="text-sm text-rose-600">{{ error }}</p>

    <template v-else-if="job">
      <div class="flex items-center gap-3 mb-4">
        <h1 class="text-lg font-semibold text-slate-800 font-mono">
          {{ job.short_id }}
        </h1>
        <span
          class="px-2 py-0.5 rounded-full text-xs"
          :class="jobBadge(job.pbs_state, job.derived_state).cls"
        >
          {{ jobBadge(job.pbs_state, job.derived_state).text }}
        </span>
        <button
          v-if="job.derived_state === 'active'"
          class="ml-auto inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-rose-300 text-rose-600 bg-white hover:bg-rose-50 disabled:opacity-60"
          :disabled="cancelling"
          @click="cancelJob"
        >
          <Loader2 v-if="cancelling" :size="15" class="animate-spin" />
          <Ban v-else :size="15" />
          终止任务
        </button>
      </div>

      <div
        class="bg-white rounded-xl border border-slate-200 p-5 mb-5 grid grid-cols-2 md:grid-cols-3 gap-x-6 gap-y-3"
      >
        <div v-for="r in rows(job)" :key="r.label" class="text-sm">
          <div class="text-slate-400">{{ r.label }}</div>
          <div class="text-slate-700 break-all font-mono mt-0.5">
            {{ r.value }}
          </div>
        </div>
      </div>

      <!-- d3plot 3D 查看入口（检测到碰撞仿真结果时显示）-->
      <div
        v-if="d3plotPath"
        class="bg-white rounded-xl border border-slate-200 p-4 mb-5 flex items-center gap-3 flex-wrap"
      >
        <Box :size="16" class="text-blue-600" />
        <span class="text-sm font-medium text-slate-700">仿真结果（d3plot）</span>
        <span class="text-xs px-1.5 py-0.5 rounded bg-blue-50 text-blue-600 font-medium">{{ d3plotCount }} 个文件</span>
        <span class="text-xs text-slate-500">检测到碰撞计算结果，可直接在浏览器查看变形动画与云图</span>
        <button
          class="ml-auto flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-700"
          @click="openD3plot"
        >
          <Box :size="15" /> 3D 查看结果
        </button>
      </div>

      <!-- 数据提取：列出全部规则 + 手动触发 -->
      <div class="bg-white rounded-xl border border-slate-200 p-4 mb-5">
        <div class="flex items-center gap-3 flex-wrap mb-2">
          <span class="text-sm font-medium text-slate-700">数据提取</span>
          <span class="text-xs text-slate-400">任务完成后自动在工作目录中运行</span>
          <span v-if="runMsg" class="text-xs text-slate-500">{{ runMsg }}</span>
          <button
            class="ml-auto flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-slate-300 bg-white hover:bg-slate-50 disabled:opacity-60"
            :disabled="running || !job.workdir"
            :title="job.workdir ? '' : '该任务无工作目录'"
            @click="runExtract"
          >
            <Loader2 v-if="running" :size="15" class="animate-spin" />
            <PlayCircle v-else :size="15" />
            立即执行提取
          </button>
        </div>
        <ul v-if="rules.length" class="space-y-1.5">
          <li
            v-for="r in rules"
            :key="r.id"
            class="flex items-start gap-2 text-xs"
          >
            <span
              class="px-1.5 py-0.5 rounded-full shrink-0"
              :class="r.enabled ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-400'"
            >
              {{ r.enabled ? "启用" : "停用" }}
            </span>
            <span class="font-medium text-slate-700 shrink-0">{{ r.name }}</span>
            <code class="text-slate-500 break-all">{{ r.command }}</code>
          </li>
        </ul>
        <div v-else class="text-xs text-slate-400">暂无提取规则</div>
      </div>

      <!-- 文件清理：删除 disk* / mes* / scr* 临时文件 -->
      <div
        class="bg-white rounded-xl border border-slate-200 p-4 mb-5 flex items-center gap-3 flex-wrap"
      >
        <span class="text-sm font-medium text-slate-700">文件清理</span>
        <span class="text-xs text-slate-400">
          删除工作目录下的 disk* / mes* / scr* 临时文件
        </span>
        <span v-if="cleanMsg" class="text-xs text-slate-500">{{ cleanMsg }}</span>
        <button
          class="ml-auto flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-rose-200 text-rose-600 bg-white hover:bg-rose-50 disabled:opacity-60"
          :disabled="cleaning || !job.workdir"
          :title="job.workdir ? '' : '该任务无工作目录'"
          @click="cleanFiles"
        >
          <Loader2 v-if="cleaning" :size="15" class="animate-spin" />
          <Trash2 v-else :size="15" />
          清理文件
        </button>
      </div>

      <div class="mb-2">
        <h2 class="text-base font-medium text-slate-700">
          工作目录
          <span class="font-mono text-sm text-slate-500">{{
            job.workdir || "未知"
          }}</span>
        </h2>
      </div>

      <FileBrowser
        v-if="job.workdir"
        :initial-path="job.workdir"
        :root-lock="true"
        :name-hint="job.name"
      />
      <p v-else class="text-sm text-slate-400">该任务未记录工作目录。</p>
    </template>
  </div>
</template>
