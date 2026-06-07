<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { api, errMsg } from "@/api";
import { Loader2, BarChart3, Download, List } from "lucide-vue-next";

type Row = { user: string; cpu_hours: number; job_count: number };
type JobRow = {
  short_id: string;
  user: string;
  queue: string;
  name: string;
  cores: number;
  start_ts: number;
  end_ts: number | null;
  hours: number;
  cpu_hours: number;
  state: string;
  exit_status: string | null;
};

function ymd(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

const today = new Date();
const start = ref(ymd(new Date(today.getTime() - 29 * 86400000)));
const end = ref(ymd(today));

const rows = ref<Row[]>([]);
const total = ref(0);
const loading = ref(false);
const error = ref("");
const loaded = ref(false);
const selectedUser = ref(""); // "" = 全部用户

// 用户下拉选项(来自查询结果)
const userOptions = computed(() =>
  [...new Set(rows.value.map((r) => r.user))].sort()
);
// 按所选用户过滤后的展示行(rows 已按机时降序)
const displayRows = computed(() =>
  selectedUser.value
    ? rows.value.filter((r) => r.user === selectedUser.value)
    : rows.value
);
const displayTotal = computed(
  () =>
    Math.round(displayRows.value.reduce((s, r) => s + r.cpu_hours, 0) * 100) / 100
);

// 机时占比柱状图用的最大值(取当前展示集的最大)
function pct(v: number): number {
  const max = displayRows.value.length ? displayRows.value[0].cpu_hours : 0;
  return max > 0 ? Math.round((v / max) * 100) : 0;
}

async function query() {
  if (start.value > end.value) {
    error.value = "开始日期不能晚于结束日期";
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    const r = await api.cpuHoursStats(start.value, end.value);
    rows.value = r.rows;
    total.value = r.total_cpu_hours;
    loaded.value = true;
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}

function downloadCsv(text: string, filename: string) {
  const blob = new Blob(["﻿" + text], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function exportCsv() {
  const header = "排名,用户,CPU机时,作业数\n";
  const body = displayRows.value
    .map((r, i) => `${i + 1},${r.user},${r.cpu_hours},${r.job_count}`)
    .join("\n");
  downloadCsv(header + body, `机时汇总_${start.value}_${end.value}.csv`);
}

function fmtDt(ts: number | null): string {
  if (!ts) return "运行中";
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// 单用户作业清单详情弹窗
const showDetail = ref(false);
const detailUser = ref("");
const detailRows = ref<JobRow[]>([]);
const detailLoading = ref(false);
async function openDetail(u: string) {
  detailUser.value = u;
  detailRows.value = [];
  detailLoading.value = true;
  showDetail.value = true;
  try {
    const r = await api.jobsStatList(start.value, end.value, u);
    detailRows.value = r.rows;
  } catch (e) {
    error.value = errMsg(e);
    showDetail.value = false;
  } finally {
    detailLoading.value = false;
  }
}

const exportingJobs = ref(false);
function csvCell(s: string): string {
  // 含逗号/引号/换行时用引号包裹并转义
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}
async function exportJobList() {
  exportingJobs.value = true;
  error.value = "";
  try {
    const r = await api.jobsStatList(start.value, end.value, selectedUser.value || undefined);
    const header =
      "作业ID,用户,队列,作业名,核数,开始时间,结束时间,时长(小时),机时,状态,退出码\n";
    const body = r.rows
      .map((j) =>
        [
          j.short_id,
          j.user,
          j.queue,
          csvCell(j.name),
          j.cores,
          fmtDt(j.start_ts),
          fmtDt(j.end_ts),
          j.hours,
          j.cpu_hours,
          j.state,
          j.exit_status ?? "",
        ].join(",")
      )
      .join("\n");
    downloadCsv(header + body, `任务清单_${start.value}_${end.value}.csv`);
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    exportingJobs.value = false;
  }
}

onMounted(query);
</script>

<template>
  <div class="max-w-5xl mx-auto">
    <div class="flex items-center gap-2 mb-4 flex-wrap">
      <h1 class="text-lg font-semibold text-slate-800 flex items-center gap-2">
        <BarChart3 :size="20" class="text-blue-600" /> CPU 机时统计
      </h1>
      <span class="text-xs text-slate-400">机时 =（结束−开始）× 核数，按用户汇总</span>
    </div>

    <!-- 查询条件 -->
    <div class="bg-white rounded-xl border border-slate-200 p-4 mb-4 flex items-end gap-3 flex-wrap">
      <label class="text-sm">
        <span class="text-slate-500 block mb-1">开始日期</span>
        <input v-model="start" type="date" class="px-2 py-1.5 border border-slate-300 rounded-md text-sm" />
      </label>
      <label class="text-sm">
        <span class="text-slate-500 block mb-1">结束日期</span>
        <input v-model="end" type="date" class="px-2 py-1.5 border border-slate-300 rounded-md text-sm" />
      </label>
      <label class="text-sm">
        <span class="text-slate-500 block mb-1">用户</span>
        <select
          v-model="selectedUser"
          class="px-2 py-1.5 border border-slate-300 rounded-md text-sm min-w-[120px] bg-white"
        >
          <option value="">全部用户</option>
          <option v-for="u in userOptions" :key="u" :value="u">{{ u }}</option>
        </select>
      </label>
      <button
        class="flex items-center gap-1.5 px-4 py-1.5 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-60"
        :disabled="loading"
        @click="query"
      >
        <Loader2 v-if="loading" :size="15" class="animate-spin" /> 查询
      </button>
      <button
        v-if="displayRows.length"
        class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-slate-300 bg-white hover:bg-slate-50"
        @click="exportCsv"
      >
        <Download :size="15" /> 导出汇总
      </button>
      <button
        class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-slate-300 bg-white hover:bg-slate-50 disabled:opacity-60"
        :disabled="exportingJobs || loading"
        title="导出区间内每个作业的明细(开始/结束时间、核数、机时)"
        @click="exportJobList"
      >
        <Loader2 v-if="exportingJobs" :size="15" class="animate-spin" />
        <Download v-else :size="15" /> 导出任务清单
      </button>
      <span v-if="loaded && !loading" class="ml-auto text-sm text-slate-600">
        合计 <span class="font-semibold text-slate-800">{{ displayTotal }}</span> 机时 ·
        {{ selectedUser ? `用户 ${selectedUser}` : `${displayRows.length} 个用户` }}
      </span>
    </div>

    <p v-if="error" class="text-sm text-rose-600 mb-3">{{ error }}</p>

    <!-- 结果表 -->
    <div class="bg-white rounded-xl border border-slate-200 overflow-x-auto">
      <table class="w-full text-sm min-w-[560px]">
        <thead class="bg-slate-50 text-slate-500 text-left">
          <tr>
            <th class="px-4 py-2.5 font-medium w-12">#</th>
            <th class="px-4 py-2.5 font-medium">用户</th>
            <th class="px-4 py-2.5 font-medium">CPU 机时</th>
            <th class="px-4 py-2.5 font-medium">作业数</th>
            <th class="px-4 py-2.5 font-medium w-1/4">占比</th>
            <th class="px-4 py-2.5 font-medium">详情</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(r, i) in displayRows" :key="r.user" class="border-t border-slate-100">
            <td class="px-4 py-2.5 text-slate-400 tabular-nums">{{ i + 1 }}</td>
            <td class="px-4 py-2.5 text-slate-700 font-medium">{{ r.user }}</td>
            <td class="px-4 py-2.5 text-slate-800 tabular-nums font-semibold">{{ r.cpu_hours }}</td>
            <td class="px-4 py-2.5 text-slate-600 tabular-nums">{{ r.job_count }}</td>
            <td class="px-4 py-2.5">
              <div class="h-2 bg-slate-100 rounded-full overflow-hidden">
                <div class="h-full bg-blue-500 rounded-full" :style="{ width: pct(r.cpu_hours) + '%' }"></div>
              </div>
            </td>
            <td class="px-4 py-2.5">
              <button
                class="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-md border border-slate-300 text-slate-600 hover:bg-slate-50"
                @click="openDetail(r.user)"
              >
                <List :size="13" /> 详情
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="loading" class="py-12 flex items-center justify-center text-slate-400 gap-2">
        <Loader2 :size="18" class="animate-spin" /> 统计中…
      </div>
      <div v-else-if="loaded && !displayRows.length" class="py-12 text-center text-slate-400">
        {{ selectedUser ? `该用户在此区间内无作业` : "该区间内无作业记录" }}
      </div>
    </div>

    <!-- 单用户作业清单详情弹窗 -->
    <div
      v-if="showDetail"
      class="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4"
      @click.self="showDetail = false"
    >
      <div class="bg-white rounded-xl shadow-xl w-full max-w-5xl flex flex-col max-h-[88vh]">
        <div class="flex items-center px-5 py-3 border-b border-slate-200">
          <div class="font-medium text-slate-800">
            {{ detailUser }} 的任务清单
            <span class="text-xs text-slate-400 ml-2">
              {{ start }} ~ {{ end }} · {{ detailRows.length }} 个作业
            </span>
          </div>
          <button
            class="ml-auto px-2 rounded hover:bg-slate-100 text-slate-500 text-lg leading-none"
            aria-label="关闭"
            @click="showDetail = false"
          >
            ✕
          </button>
        </div>
        <div class="overflow-auto">
          <div v-if="detailLoading" class="py-12 flex items-center justify-center text-slate-400 gap-2">
            <Loader2 :size="18" class="animate-spin" /> 加载中…
          </div>
          <table v-else class="w-full text-sm min-w-[860px]">
            <thead class="bg-slate-50 text-slate-500 text-left sticky top-0">
              <tr>
                <th class="px-3 py-2 font-medium">作业ID</th>
                <th class="px-3 py-2 font-medium">队列</th>
                <th class="px-3 py-2 font-medium">作业名</th>
                <th class="px-3 py-2 font-medium">核数</th>
                <th class="px-3 py-2 font-medium">开始时间</th>
                <th class="px-3 py-2 font-medium">结束时间</th>
                <th class="px-3 py-2 font-medium">时长(h)</th>
                <th class="px-3 py-2 font-medium">机时</th>
                <th class="px-3 py-2 font-medium">状态</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="j in detailRows"
                :key="j.short_id + '_' + j.start_ts"
                class="border-t border-slate-100"
              >
                <td class="px-3 py-2 font-mono text-slate-700">{{ j.short_id }}</td>
                <td class="px-3 py-2 text-slate-600">{{ j.queue || "—" }}</td>
                <td class="px-3 py-2 text-slate-600 max-w-[220px] truncate" :title="j.name">
                  {{ j.name || "—" }}
                </td>
                <td class="px-3 py-2 text-slate-600 tabular-nums">{{ j.cores }}</td>
                <td class="px-3 py-2 text-slate-600 tabular-nums">{{ fmtDt(j.start_ts) }}</td>
                <td
                  class="px-3 py-2 tabular-nums"
                  :class="j.end_ts ? 'text-slate-600' : 'text-blue-600'"
                >
                  {{ fmtDt(j.end_ts) }}
                </td>
                <td class="px-3 py-2 text-slate-600 tabular-nums">{{ j.hours }}</td>
                <td class="px-3 py-2 text-slate-800 tabular-nums font-medium">{{ j.cpu_hours }}</td>
                <td class="px-3 py-2 text-slate-600">{{ j.state }}</td>
              </tr>
            </tbody>
          </table>
          <div v-if="!detailLoading && !detailRows.length" class="py-12 text-center text-slate-400">
            无作业
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
