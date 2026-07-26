<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from "vue";
import { api, errMsg } from "@/api";
import type { NodeInfo } from "@/api/types";
import { fmtBytes } from "@/lib/format";
import {
  Server,
  RefreshCw,
  RotateCw,
  Loader2,
  Cpu,
  Activity,
  CircleCheck,
  CircleAlert,
  CircleSlash,
  CircleHelp,
} from "lucide-vue-next";

const nodes = ref<NodeInfo[]>([]);
const summary = ref({ total: 0, up: 0, down: 0, offline: 0, unknown: 0 });
const loading = ref(false);
const error = ref("");
const loaded = ref(false);
// 正在重启的节点名（禁用按钮 + 转圈）
const restarting = ref<string | null>(null);
// 每个节点最近一次重启结果提示
const notice = ref<Record<string, { ok: boolean; msg: string }>>({});

// 自动刷新
const autoRefresh = ref(false);
let timer: ReturnType<typeof setInterval> | undefined;

async function load(silent = false) {
  if (!silent) loading.value = true;
  error.value = "";
  try {
    const r = await api.listNodes();
    nodes.value = r.nodes;
    summary.value = r.summary;
    loaded.value = true;
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}

function toggleAuto() {
  autoRefresh.value = !autoRefresh.value;
  if (timer) {
    clearInterval(timer);
    timer = undefined;
  }
  if (autoRefresh.value) {
    timer = setInterval(() => load(true), 15000);
  }
}

async function restart(node: NodeInfo) {
  if (
    !window.confirm(
      `确认重启节点「${node.name}」的 PBS 服务（pbs_mom / trqauthd）？\n` +
        `将通过 ssh 到该节点执行 systemctl restart。若节点上有运行中的作业，服务重启可能影响作业。`
    )
  )
    return;
  restarting.value = node.name;
  delete notice.value[node.name];
  try {
    const r = await api.restartNodeServices(node.name);
    notice.value[node.name] = {
      ok: true,
      msg: `已重启：${r.services.join(" / ")}`,
    };
    // 重启后稍等片刻再刷新一次，反映最新状态
    setTimeout(() => load(true), 2000);
  } catch (e) {
    notice.value[node.name] = { ok: false, msg: errMsg(e) };
  } finally {
    restarting.value = null;
  }
}

// 内存字符串（如 "131072000kb"）转字节；解析失败返回 null
function memBytes(v: string | null): number | null {
  if (!v) return null;
  const m = /^(\d+)\s*(kb|mb|gb|b)?$/i.exec(v.trim());
  if (!m) return null;
  const n = Number(m[1]);
  const unit = (m[2] || "kb").toLowerCase();
  const mult = unit === "b" ? 1 : unit === "mb" ? 1024 ** 2 : unit === "gb" ? 1024 ** 3 : 1024;
  return n * mult;
}

// 内存分母：优先 totmem（物理+swap），回退 physmem。availmem 是相对 totmem 的
// 可用量，可能大于 physmem，故用 physmem 做分母会算出负值。
function memTotalBytes(n: NodeInfo): number | null {
  return memBytes(n.totmem) ?? memBytes(n.physmem);
}

// 内存使用率（0-100），无数据返回 null
function memPct(n: NodeInfo): number | null {
  const total = memTotalBytes(n);
  const avail = memBytes(n.availmem);
  if (total == null || avail == null || total <= 0) return null;
  return Math.min(100, Math.max(0, Math.round(((total - avail) / total) * 100)));
}

function memUsedText(n: NodeInfo): string {
  const total = memTotalBytes(n);
  const avail = memBytes(n.availmem);
  if (total == null || avail == null) return "—";
  return `${fmtBytes(total - avail)} / ${fmtBytes(total)}`;
}

// 核使用率（已用核槽 / 总核）
function corePct(n: NodeInfo): number {
  const np = n.np || n.ncpus || 0;
  if (np <= 0) return 0;
  return Math.min(100, Math.round((n.used_slots / np) * 100));
}

// 负载相对核数的颜色（>1x 偏高）
function loadClass(n: NodeInfo): string {
  const l = Number(n.loadave);
  const np = n.np || n.ncpus || 0;
  if (!isFinite(l) || np <= 0) return "text-slate-500";
  const r = l / np;
  if (r > 1.1) return "text-rose-600";
  if (r > 0.85) return "text-amber-600";
  return "text-slate-600";
}

const healthMeta = {
  up: { label: "正常", cls: "bg-emerald-100 text-emerald-700", icon: CircleCheck, dot: "bg-emerald-500" },
  down: { label: "宕机", cls: "bg-rose-100 text-rose-700", icon: CircleAlert, dot: "bg-rose-500" },
  offline: { label: "已下线", cls: "bg-slate-200 text-slate-600", icon: CircleSlash, dot: "bg-slate-400" },
  unknown: { label: "状态未知", cls: "bg-amber-100 text-amber-700", icon: CircleHelp, dot: "bg-amber-500" },
} as const;

function meta(n: NodeInfo) {
  return healthMeta[n.health] ?? healthMeta.unknown;
}

// 异常节点优先排前，其余按名称
const sortedNodes = computed(() =>
  [...nodes.value].sort((a, b) => {
    const rank = (h: NodeInfo["health"]) => (h === "up" ? 1 : 0);
    if (rank(a.health) !== rank(b.health)) return rank(a.health) - rank(b.health);
    return a.name.localeCompare(b.name, undefined, { numeric: true });
  })
);

onMounted(() => load());
onUnmounted(() => {
  if (timer) clearInterval(timer);
});
</script>

<template>
  <div class="max-w-[1400px] mx-auto">
    <!-- 顶部工具条 -->
    <div class="flex flex-wrap items-center gap-3 mb-4">
      <h1 class="text-lg font-semibold text-slate-800 flex items-center gap-2">
        <Server :size="20" class="text-blue-600" /> 节点监控
      </h1>

      <!-- 汇总徽标 -->
      <div v-if="loaded" class="flex items-center gap-2 text-xs">
        <span class="px-2 py-1 rounded-md bg-slate-100 text-slate-600">
          共 {{ summary.total }} 节点
        </span>
        <span class="px-2 py-1 rounded-md bg-emerald-100 text-emerald-700">正常 {{ summary.up }}</span>
        <span v-if="summary.down" class="px-2 py-1 rounded-md bg-rose-100 text-rose-700">宕机 {{ summary.down }}</span>
        <span v-if="summary.offline" class="px-2 py-1 rounded-md bg-slate-200 text-slate-600">下线 {{ summary.offline }}</span>
        <span v-if="summary.unknown" class="px-2 py-1 rounded-md bg-amber-100 text-amber-700">未知 {{ summary.unknown }}</span>
      </div>

      <div class="ml-auto flex items-center gap-2">
        <label class="flex items-center gap-1.5 text-sm text-slate-600 cursor-pointer select-none">
          <input type="checkbox" :checked="autoRefresh" @change="toggleAuto" class="accent-blue-600" />
          自动刷新(15s)
        </label>
        <button
          class="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50"
          :disabled="loading"
          @click="load()"
        >
          <RefreshCw :size="16" :class="loading ? 'animate-spin' : ''" /> 刷新
        </button>
      </div>
    </div>

    <div v-if="error" class="mb-4 px-3 py-2 rounded-md bg-rose-50 text-rose-700 text-sm">
      {{ error }}
    </div>

    <!-- 加载中 -->
    <div v-if="loading && !loaded" class="flex items-center justify-center py-20 text-slate-400">
      <Loader2 :size="28" class="animate-spin" />
    </div>

    <!-- 空 -->
    <div v-else-if="loaded && nodes.length === 0" class="py-20 text-center text-slate-400">
      未采集到任何节点
    </div>

    <!-- 节点卡片网格 -->
    <div v-else class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3">
      <div
        v-for="n in sortedNodes"
        :key="n.name"
        class="rounded-lg border bg-white p-4 flex flex-col gap-3"
        :class="n.health === 'up' ? 'border-slate-200' : 'border-rose-200'"
      >
        <!-- 卡片头 -->
        <div class="flex items-center gap-2">
          <span class="w-2 h-2 rounded-full shrink-0" :class="meta(n).dot"></span>
          <span class="font-mono font-medium text-slate-800 truncate" :title="n.name">{{ n.name }}</span>
          <span
            class="ml-auto shrink-0 inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium"
            :class="meta(n).cls"
          >
            <component :is="meta(n).icon" :size="13" />
            {{ meta(n).label }}
          </span>
        </div>

        <!-- 原始状态串（多状态时展示，如 down,offline） -->
        <div v-if="n.state && n.states.length > 1" class="text-xs text-slate-400 font-mono -mt-1">
          {{ n.state }}
        </div>

        <!-- 指标 -->
        <div class="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
          <!-- 核使用 -->
          <div class="col-span-2">
            <div class="flex items-center justify-between text-xs text-slate-500 mb-1">
              <span class="flex items-center gap-1"><Cpu :size="13" /> 核使用</span>
              <span>{{ n.used_slots }} / {{ n.np ?? n.ncpus ?? "—" }} 核 · {{ n.running_jobs }} 作业</span>
            </div>
            <div class="h-1.5 rounded-full bg-slate-100 overflow-hidden">
              <div
                class="h-full rounded-full"
                :class="corePct(n) >= 100 ? 'bg-blue-600' : 'bg-blue-400'"
                :style="{ width: corePct(n) + '%' }"
              ></div>
            </div>
          </div>

          <!-- 内存使用 -->
          <div class="col-span-2">
            <div class="flex items-center justify-between text-xs text-slate-500 mb-1">
              <span>内存</span>
              <span>{{ memPct(n) != null ? memPct(n) + "%" : "—" }} · {{ memUsedText(n) }}</span>
            </div>
            <div class="h-1.5 rounded-full bg-slate-100 overflow-hidden">
              <div
                class="h-full rounded-full"
                :class="(memPct(n) ?? 0) >= 90 ? 'bg-rose-500' : 'bg-emerald-400'"
                :style="{ width: (memPct(n) ?? 0) + '%' }"
              ></div>
            </div>
          </div>

          <!-- 负载 -->
          <div class="flex items-center gap-1.5">
            <Activity :size="14" class="text-slate-400" />
            <span class="text-xs text-slate-500">负载</span>
            <span class="font-medium" :class="loadClass(n)">{{ n.loadave ?? "—" }}</span>
          </div>
          <!-- GPU（有则显示） -->
          <div v-if="n.gpus" class="text-xs text-slate-500 flex items-center gap-1">
            GPU: <span class="font-medium text-slate-700">{{ n.gpus }}</span>
          </div>
        </div>

        <!-- 重启结果提示 -->
        <div
          v-if="notice[n.name]"
          class="text-xs px-2 py-1.5 rounded"
          :class="notice[n.name].ok ? 'bg-emerald-50 text-emerald-700' : 'bg-rose-50 text-rose-700'"
        >
          {{ notice[n.name].msg }}
        </div>

        <!-- 操作 -->
        <div class="mt-auto pt-1 flex justify-end">
          <button
            class="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm border border-slate-300 text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            :disabled="restarting === n.name"
            :title="'ssh 到该节点 systemctl restart pbs_mom trqauthd'"
            @click="restart(n)"
          >
            <RotateCw v-if="restarting !== n.name" :size="15" />
            <Loader2 v-else :size="15" class="animate-spin" />
            重启 PBS 服务
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
