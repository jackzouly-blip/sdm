<script setup lang="ts">
import { ref, reactive, onMounted } from "vue";
import { useRouter } from "vue-router";
import { api, errMsg } from "@/api";
import type { JobSummary } from "@/api/types";
import { fmtTime, jobBadge } from "@/lib/format";
import { RefreshCw, Loader2, Inbox, Ban, Terminal, Trash2 } from "lucide-vue-next";
import TrialOutputModal from "@/components/TrialOutputModal.vue";

const router = useRouter();
const jobs = ref<JobSummary[]>([]);
const loading = ref(false);
const error = ref("");
const cancelling = ref<string | null>(null);

// 试算输出弹窗（jobid 形如 "trial:<id>"）
const trialModal = ref<{ id: number; name: string } | null>(null);
function isTrial(job: JobSummary): boolean {
  return job.jobid.startsWith("trial:");
}
function openTrial(job: JobSummary) {
  const id = Number(job.jobid.slice("trial:".length));
  trialModal.value = { id, name: job.name || job.short_id };
}

// 终止运行/排队中的任务（qdel）
async function cancel(job: JobSummary) {
  if (
    !window.confirm(
      `确定终止任务「${job.name || job.short_id}」（${job.short_id}）吗？此操作不可恢复。`
    )
  )
    return;
  cancelling.value = job.jobid;
  error.value = "";
  try {
    await api.cancelJob(job.jobid);
    await load();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    cancelling.value = null;
  }
}

// 撤回一个尚未进入 PBS 的本地排队项（未占用 PBS 资源，无需确认弹窗那么谨慎）
async function cancelQueued(job: JobSummary) {
  if (job.queue_id == null) return;
  cancelling.value = job.jobid;
  error.value = "";
  try {
    await api.cancelQueuedSubmission(job.queue_id);
    await load();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    cancelling.value = null;
  }
}
// 删除一条“提交失败”的本地排队记录（仅清门户记录，未占任何 PBS 资源）
async function deleteFailed(job: JobSummary) {
  if (job.queue_id == null) return;
  if (!window.confirm(`删除失败记录「${job.name || job.short_id}」？该记录未占用计算资源，仅从列表清除。`))
    return;
  cancelling.value = job.jobid;
  error.value = "";
  try {
    await api.deleteFailedSubmission(job.queue_id);
    await load();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    cancelling.value = null;
  }
}
const filter = ref<"" | "active" | "done">("active");

// d3plot 文件数：默认关(开启才逐个扫目录，避免列表加载变慢)
const showD3plot = ref(false);
const d3plotCounts = reactive<Record<string, number>>({});

async function fetchD3plotCounts() {
  const todo = jobs.value.filter((j) => j.workdir);
  for (const j of todo) delete d3plotCounts[j.jobid]; // 置为加载中
  let i = 0;
  const worker = async () => {
    while (i < todo.length) {
      const j = todo[i++];
      try {
        const r = await api.d3plotFind(j.workdir!);
        d3plotCounts[j.jobid] = r.found.length + r.n_state_files;
      } catch {
        d3plotCounts[j.jobid] = 0;
      }
    }
  };
  await Promise.all(Array.from({ length: 6 }, worker)); // 并发上限 6
}

function toggleD3plot() {
  showD3plot.value = !showD3plot.value;
  if (showD3plot.value) fetchD3plotCounts();
}

async function load() {
  loading.value = true;
  error.value = "";
  try {
    jobs.value = await api.listJobs(filter.value || undefined);
    if (showD3plot.value) fetchD3plotCounts();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}

function open(job: JobSummary) {
  // 试算行：打开输出面板（非 PBS 任务，无 qstat 详情）
  if (isTrial(job)) {
    openTrial(job);
    return;
  }
  // 本地排队中/提交失败的记录不是真实 PBS 任务，没有详情页可看
  if (job.queue_id != null) return;
  router.push({ name: "job-detail", params: { jobid: job.jobid } });
}

onMounted(load);

const filters: { value: "" | "active" | "done"; label: string }[] = [
  { value: "", label: "全部" },
  { value: "active", label: "运行中" },
  { value: "done", label: "已完成" },
];

// 从 exec_host("node01/0+node01/1+node02/0") 提取去重的节点名
function nodeNames(execHost: string | null): string {
  if (!execHost) return "—";
  const set = new Set(
    execHost
      .split("+")
      .map((s) => s.split("/")[0].split(".")[0]) // 取节点名，去掉 /核 与域名后缀
      .filter(Boolean)
  );
  return set.size ? [...set].join(", ") : "—";
}
</script>

<template>
  <div class="w-full">
    <div class="flex items-center gap-3 mb-4">
      <h1 class="text-lg font-semibold text-slate-800">我的任务</h1>
      <div class="flex gap-1 ml-2">
        <button
          v-for="f in filters"
          :key="f.value"
          class="px-3 py-1 text-sm rounded-md border transition"
          :class="
            filter === f.value
              ? 'bg-blue-600 text-white border-blue-600'
              : 'bg-white text-slate-600 border-slate-300 hover:bg-slate-50'
          "
          @click="
            filter = f.value;
            load();
          "
        >
          {{ f.label }}
        </button>
      </div>
      <button
        class="ml-auto px-3 py-1.5 text-sm rounded-md border transition"
        :class="showD3plot ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-slate-600 border-slate-300 hover:bg-slate-50'"
        title="显示每个作业已生成的 d3plot 文件数(需逐个扫描目录)"
        @click="toggleD3plot"
      >
        d3plot 数
      </button>
      <button
        class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-slate-300 bg-white hover:bg-slate-50"
        :disabled="loading"
        @click="load"
      >
        <RefreshCw :size="15" :class="{ 'animate-spin': loading }" /> 刷新
      </button>
    </div>

    <p v-if="error" class="text-sm text-rose-600 mb-3">{{ error }}</p>

    <div class="bg-white rounded-xl border border-slate-200 overflow-x-auto">
      <table class="w-full text-sm min-w-[760px]">
        <thead class="bg-slate-50 text-slate-500 text-left">
          <tr>
            <th class="px-4 py-2.5 font-medium">任务 ID</th>
            <th class="px-4 py-2.5 font-medium">名称</th>
            <th class="px-4 py-2.5 font-medium">提交人</th>
            <th class="px-4 py-2.5 font-medium">状态</th>
            <th class="px-4 py-2.5 font-medium">队列</th>
            <th class="px-4 py-2.5 font-medium">计算节点</th>
            <th class="px-4 py-2.5 font-medium">提交时间</th>
            <th class="px-4 py-2.5 font-medium">结束时间</th>
            <th class="px-4 py-2.5 font-medium">用时</th>
            <th v-if="showD3plot" class="px-4 py-2.5 font-medium">d3plot</th>
            <th class="px-4 py-2.5 font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="job in jobs"
            :key="job.jobid"
            class="border-t border-slate-100 hover:bg-blue-50/40"
            :class="job.queue_id != null ? '' : 'cursor-pointer'"
            @click="open(job)"
          >
            <td class="px-4 py-2.5 font-mono text-slate-700">
              {{ job.short_id }}
            </td>
            <td class="px-4 py-2.5 text-slate-700">{{ job.name || "—" }}</td>
            <td class="px-4 py-2.5 text-slate-600">{{ job.owner || "—" }}</td>
            <td class="px-4 py-2.5">
              <span
                class="px-2 py-0.5 rounded-full text-xs"
                :class="jobBadge(job.pbs_state, job.derived_state).cls"
                :title="job.msg || ''"
              >
                {{ jobBadge(job.pbs_state, job.derived_state).text }}
              </span>
            </td>
            <td class="px-4 py-2.5 text-slate-600">{{ job.queue || "—" }}</td>
            <td class="px-4 py-2.5 text-slate-600 font-mono text-xs">{{ nodeNames(job.exec_host) }}</td>
            <td class="px-4 py-2.5 text-slate-600">
              {{ fmtTime(job.submit_ts) }}
            </td>
            <td class="px-4 py-2.5 text-slate-600">
              {{ job.end_ts ? fmtTime(job.end_ts) : "—" }}
            </td>
            <td class="px-4 py-2.5 text-slate-600">
              {{ job.walltime_used || "—" }}
            </td>
            <td v-if="showD3plot" class="px-4 py-2.5 text-slate-600 tabular-nums">
              <span v-if="!job.workdir" class="text-slate-300">—</span>
              <span v-else-if="d3plotCounts[job.jobid] === undefined" class="text-slate-400">…</span>
              <span v-else-if="d3plotCounts[job.jobid] > 0" class="text-blue-600 font-medium">{{ d3plotCounts[job.jobid] }}</span>
              <span v-else class="text-slate-300">无</span>
            </td>
            <td class="px-4 py-2.5">
              <button
                v-if="isTrial(job)"
                class="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-md border border-indigo-200 text-indigo-700 hover:bg-indigo-50"
                @click.stop="openTrial(job)"
              >
                <Terminal :size="13" />
                {{ job.derived_state === 'trial_running' ? '查看/中断' : '查看输出' }}
              </button>
              <button
                v-else-if="job.derived_state === 'active'"
                class="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-md border border-rose-200 text-rose-600 hover:bg-rose-50 disabled:opacity-60"
                :disabled="cancelling === job.jobid"
                @click.stop="cancel(job)"
              >
                <Loader2 v-if="cancelling === job.jobid" :size="13" class="animate-spin" />
                <Ban v-else :size="13" />
                终止
              </button>
              <button
                v-else-if="job.derived_state === 'queued_local'"
                class="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-md border border-amber-200 text-amber-700 hover:bg-amber-50 disabled:opacity-60"
                :disabled="cancelling === job.jobid"
                title="尚未进入 PBS，撤回不占用任何计算资源"
                @click.stop="cancelQueued(job)"
              >
                <Loader2 v-if="cancelling === job.jobid" :size="13" class="animate-spin" />
                <Ban v-else :size="13" />
                取消排队
              </button>
              <button
                v-else-if="job.derived_state === 'queue_failed'"
                class="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-md border border-slate-200 text-slate-500 hover:bg-slate-50 disabled:opacity-60"
                :disabled="cancelling === job.jobid"
                title="仅清除门户里的这条失败记录，不占用任何计算资源"
                @click.stop="deleteFailed(job)"
              >
                <Loader2 v-if="cancelling === job.jobid" :size="13" class="animate-spin" />
                <Trash2 v-else :size="13" />
                删除记录
              </button>
              <span v-else class="text-slate-300">—</span>
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
        v-else-if="!jobs.length"
        class="py-12 flex flex-col items-center justify-center text-slate-400 gap-2"
      >
        <Inbox :size="28" /> 暂无任务
      </div>
    </div>

    <TrialOutputModal
      v-if="trialModal"
      :trial-id="trialModal.id"
      :name="trialModal.name"
      @close="trialModal = null"
      @changed="load"
    />
  </div>
</template>
