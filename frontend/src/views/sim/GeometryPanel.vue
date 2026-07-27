<script setup lang="ts">
/**
 * 分析对象的几何版本面板：上传数模、轻量化、查看版本、下载、预览。
 *
 * CAD 原生格式（CATIA/STEP/JT）浏览器渲染不了，必须先经 vektor3d 的
 * geometry.convert 能力转成 glTF/GLB。这条链路的方向是反直觉的：
 * **调用由本页面发起，但文件传输是 vektor3d 与本服务直连完成的，不经浏览器**
 * ——SDM 在集群、vektor3d 在用户桌面只监听 localhost，集群调不通它；
 * 而几百 MB 的装配经浏览器中转必然打爆页面内存。
 * 契约见 docs/vektor3d-geometry-capability-contract.md。
 */
import { onMounted, ref } from "vue";
import { simApi, errMsg } from "@/api";
import type { SimGeometry } from "@/api/types";
import { vektor3d, Vektor3dError, type ConvertResult, type HealthInfo } from "@/api/vektor3d";
import {
  AlertTriangle,
  Box,
  Download,
  Eye,
  FolderInput,
  Loader2,
  Plug,
  Settings2,
  Upload,
  Wand2,
} from "lucide-vue-next";

const props = defineProps<{ targetId: string; targetName: string }>();
const emit = defineEmits<{ (e: "preview", g: SimGeometry): void }>();

const geometries = ref<SimGeometry[]>([]);
const loading = ref(true);
const error = ref("");
const uploading = ref(false);
const progress = ref(0);
const fileInput = ref<HTMLInputElement | null>(null);

/** 需要轻量化才能渲染的 CAD 原生格式 */
const CAD_EXT = /\.(catpart|catproduct|stp|step|jt|igs|iges|3dxml|prt|sldprt|sldasm|x_t|x_b|zip)$/i;
/** 求解器输入卡：SDM 自己解析成 GLB，不依赖 vektor3d */
const DECK_EXT = /\.(k|key|kinc|dyn)$/i;

const clusterPath = ref("");
const importing = ref(false);
const converting = ref<string | null>(null);

// ── vektor3d 连接 ────────────────────────────────────────────────────────────
const vkHealth = ref<HealthInfo | null>(null);
const vkError = ref("");
const vkChecking = ref(false);
const showVkSettings = ref(false);
const pageOrigin = window.location.origin;
// Chrome 的私有网络访问(PNA)：非安全上下文的公网页面发往 127.0.0.1 的请求会被拦，
// 报错与"没装 vektor3d"完全一样。这是联调最容易卡住的地方,提前点明。
const insecurePrivateNetwork =
  window.location.protocol === "http:" && !/^(localhost|127\.|\[::1\])/.test(window.location.hostname);
const vkBase = ref(vektor3d.getBase());
const vkToken = ref(vektor3d.getPairingToken());

async function checkVektor3d() {
  vkChecking.value = true;
  vkError.value = "";
  try {
    vkHealth.value = await vektor3d.health();
  } catch (e) {
    vkHealth.value = null;
    vkError.value = e instanceof Error ? e.message : String(e);
  } finally {
    vkChecking.value = false;
  }
}

function saveVkSettings() {
  vektor3d.setBase(vkBase.value);
  vektor3d.setPairingToken(vkToken.value);
  showVkSettings.value = false;
  checkVektor3d();
}

/** 能不能点「轻量化」：连得上、且这个源被授权 */
function vkUsable(): boolean {
  return !!vkHealth.value?.authorized;
}

// ── 轻量化：把转换派给桌面端 vektor3d ─────────────────────────────────────────
const convertStep = ref("");
const convertResult = ref<Record<string, ConvertResult>>({});

async function lightweight(g: SimGeometry) {
  converting.value = g.id;
  convertStep.value = "申请转换票据…";
  error.value = "";
  try {
    // 票据只对这一个 gid、这两个端点有效，半小时过期——不把会话 JWT 交出去
    const ticket = await simApi.convertTicket(g.id);
    const result = await vektor3d.runJob<ConvertResult>(
      "geometry.convert",
      {
        sourceUrl: ticket.sourceUrl,
        uploadUrl: ticket.uploadUrl,
        authToken: ticket.token,
        sourceName: ticket.sourceName,
        options: { unit: "mm" },
      },
      {
        // 幂等键带上 gid：页面刷新后重复点不会真的转两遍
        idempotencyKey: `sdm-geometry-${g.id}`,
        onProgress: (p, job) => {
          convertStep.value = p
            ? `${p.step}${p.detail ? ` · ${p.detail}` : ""}`
            : job.queuePosition != null
              ? `排队中（第 ${job.queuePosition + 1} 位）`
              : "转换中…";
        },
      }
    );
    convertResult.value = { ...convertResult.value, [g.id]: result };
    if (result.warnings?.length) {
      error.value = `转换完成，但有提示：${result.warnings.join("；")}`;
    }
    await load();
  } catch (e) {
    error.value =
      e instanceof Vektor3dError
        ? `vektor3d：${e.message}`
        : errMsg(e);
  } finally {
    converting.value = null;
    convertStep.value = "";
  }
}

/**
 * 从集群路径导入。deck 走这条路是必须的——主控 .key 会牵出几百 MB 的 include
 * 树、散在集群目录里，浏览器传不上来，而它本就在集群上。
 */
async function importFromPath() {
  const p = clusterPath.value.trim();
  if (!p) return;
  importing.value = true;
  error.value = "";
  try {
    const g = await simApi.addGeometryFromPath(props.targetId, p);
    clusterPath.value = "";
    await load();
    if (g.convert_task_id) {
      converting.value = g.id;
      pollConversion(g.id);
    }
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    importing.value = false;
  }
}

/** deck 转换是分钟级的异步任务，轮询到产物出现为止 */
function pollConversion(gid: string) {
  const timer = window.setInterval(async () => {
    try {
      const rows = await simApi.listGeometries(props.targetId);
      geometries.value = rows;
      const g = rows.find((r) => r.id === gid);
      if (g?.lightweight_file) {
        window.clearInterval(timer);
        converting.value = null;
      }
    } catch {
      window.clearInterval(timer);
      converting.value = null;
    }
  }, 5000);
  // 兜底：整车 deck 实测约 40 秒，10 分钟仍无产物即认为失败，停止轮询
  window.setTimeout(() => {
    window.clearInterval(timer);
    if (converting.value === gid) converting.value = null;
  }, 600000);
}

async function load() {
  loading.value = true;
  error.value = "";
  try {
    geometries.value = await simApi.listGeometries(props.targetId);
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}

async function onPick(ev: Event) {
  const input = ev.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  uploading.value = true;
  progress.value = 0;
  error.value = "";
  try {
    await simApi.uploadGeometry(props.targetId, file, (p) => (progress.value = p));
    await load();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    uploading.value = false;
    input.value = ""; // 允许同名文件再次选择
  }
}

function fmtSize(n?: number) {
  if (!n) return "—";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}

function fmt(ts: number) {
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
}

/** 该版本当前能否在网页里渲染 */
function renderState(g: SimGeometry): { can: boolean; text: string } {
  if (g.lightweight_file) return { can: true, text: "可预览" };
  const name = g.source_file?.name ?? "";
  if (converting.value === g.id) return { can: false, text: "转换中…" };
  // deck 由 SDM 自己解析，不依赖 vektor3d；没产物说明转换失败或还没跑
  if (DECK_EXT.test(name)) return { can: false, text: "待转换" };
  if (CAD_EXT.test(name)) return { can: false, text: "待轻量化" };
  return { can: false, text: "格式不支持预览" };
}

/** 是否是「需要 vektor3d 轻量化」的 CAD 原生格式（deck 走 SDM 自己的解析） */
function needsLightweight(g: SimGeometry): boolean {
  if (g.lightweight_file) return false;
  const name = g.source_file?.name ?? "";
  return CAD_EXT.test(name) && !DECK_EXT.test(name);
}

/** 摘要里的关键读数：include 缺失意味着模型不完整，必须显眼 */
function deckWarning(g: SimGeometry): string | null {
  const s = g.topo_summary as Record<string, unknown> | null;
  if (!s) return null;
  const missing = Number(s.missingIncludeCount ?? 0);
  if (missing > 0) return `${missing} 个 include 缺失，模型可能不完整`;
  if (s.truncated) return "三角面超预算，已按零件截断";
  return null;
}

onMounted(() => {
  load();
  checkVektor3d();
});
defineExpose({ reload: load });
</script>

<template>
  <div>
    <div class="flex items-center gap-2 mb-3">
      <span class="text-sm text-slate-600">{{ targetName }} 的几何版本</span>
      <input
        ref="fileInput"
        type="file"
        class="hidden"
        accept=".CATPart,.CATProduct,.stp,.step,.jt,.igs,.iges,.3dxml,.prt,.sldprt,.sldasm,.x_t,.x_b,.stl,.obj,.glb,.gltf,.zip"
        @change="onPick"
      />
      <button
        class="ml-auto flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50"
        :disabled="uploading"
        @click="fileInput?.click()"
      >
        <Loader2 v-if="uploading" :size="15" class="animate-spin" />
        <Upload v-else :size="15" />
        {{ uploading ? `上传中 ${progress}%` : "导入数模" }}
      </button>
    </div>

    <!-- 从集群路径导入：deck 只能走这条（include 树在集群上，传不上来） -->
    <div class="flex gap-2 mb-3">
      <input
        v-model="clusterPath"
        class="flex-1 px-3 py-1.5 border border-slate-300 rounded-md text-sm font-mono"
        placeholder="集群路径，如 /data/project-ext/user07/.../000_Master.key"
        @keyup.enter="importFromPath"
      />
      <button
        class="flex items-center gap-1.5 px-3 py-1.5 rounded-md border border-slate-300 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50 shrink-0"
        :disabled="importing || !clusterPath.trim()"
        @click="importFromPath"
      >
        <Loader2 v-if="importing" :size="15" class="animate-spin" />
        <FolderInput v-else :size="15" />
        从集群导入
      </button>
    </div>

    <!-- vektor3d 连接状态：CAD 数模的轻量化要靠它，连不上就先说清楚为什么 -->
    <div
      class="flex items-center gap-2 mb-3 px-3 py-1.5 rounded-md text-xs"
      :class="
        vkUsable()
          ? 'bg-emerald-50/70 text-emerald-800'
          : 'bg-slate-50 text-slate-600 border border-slate-200'
      "
    >
      <Plug :size="13" :class="vkUsable() ? '' : 'text-slate-400'" />
      <template v-if="vkChecking">正在探测 vektor3d…</template>
      <template v-else-if="vkUsable()">
        vektor3d 已连接（v{{ vkHealth?.appVersion || "?" }}）
        <span v-if="vkHealth?.notReady?.length" class="text-amber-700">
          · {{ vkHealth.notReady.map((n) => n.reason).filter(Boolean).join("；") }}
        </span>
      </template>
      <template v-else-if="vkHealth && !vkHealth.authorized">
        vektor3d 已启动，但本页面未被授权：{{ vkHealth.authMessage }}
      </template>
      <template v-else>{{ vkError || "未检测到 vektor3d" }}</template>
      <button
        class="ml-auto inline-flex items-center gap-1 px-1.5 py-0.5 rounded hover:bg-white/70"
        @click="showVkSettings = !showVkSettings"
      >
        <Settings2 :size="12" /> 连接设置
      </button>
      <button class="px-1.5 py-0.5 rounded hover:bg-white/70" @click="checkVektor3d">
        重新检测
      </button>
    </div>

    <div
      v-if="showVkSettings"
      class="mb-3 p-3 rounded-md border border-slate-200 bg-white text-xs space-y-2"
    >
      <div class="flex items-center gap-2">
        <label class="w-24 text-slate-500 shrink-0">服务地址</label>
        <input
          v-model="vkBase"
          class="flex-1 px-2 py-1 border border-slate-300 rounded font-mono"
          placeholder="http://127.0.0.1:23710"
        />
      </div>
      <div class="flex items-center gap-2">
        <label class="w-24 text-slate-500 shrink-0">配对令牌</label>
        <input
          v-model="vkToken"
          class="flex-1 px-2 py-1 border border-slate-300 rounded font-mono"
          placeholder="vektor3d「设置 → 能力服务」里的令牌，未启用则留空"
        />
      </div>
      <p class="text-slate-400 leading-relaxed">
        vektor3d 默认拒绝一切网页调用。请在其「系统 → 能力服务」里把本页面地址
        <code class="px-1 bg-slate-100 rounded">{{ pageOrigin }}</code>
        加入来源白名单；若启用了配对令牌，这里填同一个值。
      </p>
      <p v-if="insecurePrivateNetwork" class="text-amber-600 leading-relaxed">
        当前页面走的是 http，浏览器的「私有网络访问」策略会拦截由它发往
        127.0.0.1 的请求（与 vektor3d 是否运行无关，表现就是"连不上"）。
        把门户挂到 https 即可解除——Chrome 视 http://127.0.0.1 为可信来源，
        https 页面调它不算混合内容。
      </p>
      <button
        class="px-2.5 py-1 rounded bg-blue-600 text-white hover:bg-blue-700"
        @click="saveVkSettings"
      >
        保存并重新检测
      </button>
    </div>

    <div v-if="error" class="mb-3 px-3 py-2 rounded-md bg-rose-50 text-rose-700 text-sm">
      {{ error }}
    </div>

    <div v-if="loading" class="flex items-center gap-2 text-slate-500 text-sm py-6 justify-center">
      <Loader2 :size="16" class="animate-spin" /> 加载中…
    </div>

    <div
      v-else-if="!geometries.length"
      class="text-center py-10 text-slate-400 border border-dashed border-slate-200 rounded-lg"
    >
      <Box :size="26" class="mx-auto mb-2 opacity-40" />
      <p class="text-sm">还没有几何版本</p>
      <p class="text-xs mt-1">导入 CATIA / STEP / JT 等数模文件；装配请打成 zip</p>
    </div>

    <table v-else class="w-full text-sm">
      <thead class="text-slate-500 border-b border-slate-200">
        <tr>
          <th class="text-left font-medium py-2 w-14">版本</th>
          <th class="text-left font-medium py-2">文件</th>
          <th class="text-left font-medium py-2 w-20">大小</th>
          <th class="text-left font-medium py-2 w-28">渲染状态</th>
          <th class="text-left font-medium py-2">导入时间</th>
          <th class="w-28"></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="g in geometries" :key="g.id" class="border-b border-slate-100">
          <td class="py-2 text-slate-500">v{{ g.version_no }}</td>
          <td class="py-2 text-slate-800 truncate max-w-xs">
            {{ g.source_file?.name ?? "—" }}
          </td>
          <td class="py-2 text-slate-500">{{ fmtSize(g.source_file?.size) }}</td>
          <td class="py-2">
            <span
              class="px-1.5 py-0.5 rounded text-xs"
              :class="
                renderState(g).can
                  ? 'bg-emerald-50 text-emerald-700'
                  : 'bg-amber-50 text-amber-700'
              "
              :title="
                renderState(g).can
                  ? '已有轻量化产物，可在网页中渲染'
                  : 'CAD 原生格式需先经 vektor3d 转成 glTF/GLB 才能在浏览器渲染'
              "
            >
              {{ renderState(g).text }}
            </span>
            <span
              v-if="deckWarning(g)"
              class="ml-1 inline-flex items-center gap-0.5 text-xs text-amber-600"
              :title="deckWarning(g)!"
            >
              <AlertTriangle :size="11" />
            </span>
          </td>
          <td class="py-2 text-slate-500">{{ fmt(g.created_at) }}</td>
          <td class="py-2">
            <div class="flex items-center gap-1">
              <button
                v-if="renderState(g).can"
                class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs text-blue-600 hover:bg-blue-50"
                @click="emit('preview', g)"
              >
                <Eye :size="12" /> 预览
              </button>
              <!-- CAD 原生格式：派给桌面端 vektor3d 转 glTF/GLB -->
              <button
                v-else-if="needsLightweight(g)"
                class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs text-indigo-600 hover:bg-indigo-50 disabled:opacity-40 disabled:hover:bg-transparent"
                :disabled="!vkUsable() || !!converting"
                :title="
                  vkUsable()
                    ? '经 vektor3d 转成 glTF/GLB；文件由 vektor3d 与本服务直连收发，不经浏览器'
                    : 'vektor3d 未连接或本页面未被授权，先在上方「连接设置」处理'
                "
                @click="lightweight(g)"
              >
                <Loader2 v-if="converting === g.id" :size="12" class="animate-spin" />
                <Wand2 v-else :size="12" />
                {{ converting === g.id ? "转换中" : "轻量化" }}
              </button>
              <a
                :href="simApi.sourceDownloadUrl(g.id)"
                class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs text-slate-500 hover:bg-slate-50"
                title="下载源文件"
              >
                <Download :size="12" /> 源文件
              </a>
            </div>
          </td>
        </tr>
      </tbody>
    </table>

    <!-- 转换进度：vektor3d 逐步回报（下载 → 转换 → 生成 glTF → 回传） -->
    <div
      v-if="converting && convertStep"
      class="mt-3 flex items-center gap-2 px-3 py-2 rounded-md bg-indigo-50 text-indigo-800 text-xs"
    >
      <Loader2 :size="13" class="animate-spin shrink-0" />
      <span class="truncate">{{ convertStep }}</span>
      <span class="ml-auto text-indigo-500 shrink-0">源文件与产物由 vektor3d 直连收发</span>
    </div>

    <p
      v-if="geometries.some((g) => !renderState(g).can)"
      class="text-xs text-slate-400 mt-3 leading-relaxed"
    >
      求解器输入卡（.k/.key）由 SDM 自行解析成 glTF，不依赖 vektor3d；标注「待轻量化」的是
      CAD 原生格式，浏览器无法直接渲染，点「轻量化」交由 vektor3d 的
      <code class="px-1 bg-slate-100 rounded">geometry.convert</code> 转成 glTF/GLB。
      装配请打成 zip 上传——<code class="px-1 bg-slate-100 rounded">.CATProduct</code>
      只是引用壳，单传它拿不到子零件。接口契约见
      <code class="px-1 bg-slate-100 rounded">docs/vektor3d-geometry-capability-contract.md</code>。
    </p>
  </div>
</template>
