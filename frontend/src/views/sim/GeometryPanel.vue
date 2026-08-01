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
  Grid3x3,
  Loader2,
  Plug,
  Settings2,
  Trash2,
  Upload,
  Wand2,
} from "lucide-vue-next";
import MeshPanel from "./MeshPanel.vue";

const props = defineProps<{ targetId: string; targetName: string }>();
const emit = defineEmits<{
  (e: "preview", g: SimGeometry): void;
  /** 网格预览：复用同一个查看器，但加载的是网格 GLB（带真实单元边线） */
  (e: "previewMesh", payload: { src: string; title: string; geometry: SimGeometry }): void;
}>();

/** 展开网格面板的几何版本 id */
const openMesh = ref<string | null>(null);

const geometries = ref<SimGeometry[]>([]);
const loading = ref(true);
const error = ref("");
const notice = ref("");
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
//
// 阶段时间线而非"一行最新状态"：这条链跨三方（页面派活、vektor3d 拉源文件、
// vektor3d 回传产物），失败时最要紧的信息是"死在哪一段"。曾经只留最新一行、
// 且在 finally 里清空，于是一句 "socket hang up" 落下来完全无从定位——
// 到底是没拉到源文件，还是转完了没传回来，看不出来。
//
// 时间线里既有 vektor3d 回报的阶段，也有 SDM 自己记的本地阶段（申请票据、
// 已提交作业）：若 vektor3d 一条都没报，时间线停在"已提交作业"这件事本身
// 就是结论——问题在桌面端，而不是在我们与它之间。
interface ConvertStage {
  /** 阶段名。同名阶段就地更新，不追加新行 */
  step: string;
  text: string;
  ts: number;
  /** local=SDM 自己记的；remote=vektor3d 回报的 */
  from: "local" | "remote";
}

const convertStages = ref<ConvertStage[]>([]);
const convertStartedAt = ref(0);
const convertElapsed = ref(0);
const convertFailedAt = ref("");
const convertResult = ref<Record<string, ConvertResult>>({});
let elapsedTimer = 0;

const lastStage = () => convertStages.value[convertStages.value.length - 1];
const convertStep = () => lastStage()?.text ?? "";

function stage(step: string, text: string, from: ConvertStage["from"] = "local") {
  const last = lastStage();
  // 同名阶段就地更新：「网格化」按 5% 一档回报，逐条追加会把时间线冲成二十行，
  // 而它们本是同一阶段在推进。ts 保留首次进入该阶段的时刻，这样时间线上读到的
  // 是"这一阶段从第几秒开始"——相邻两行的差值就是上一阶段的实际耗时。
  if (last && last.from === from && last.step === step) {
    convertStages.value = [...convertStages.value.slice(0, -1), { ...last, text }];
    return;
  }
  convertStages.value = [...convertStages.value, { step, text, ts: Date.now(), from }];
}

/** 阶段相对开始时刻的秒数——「卡住」是靠相邻阶段的时间差看出来的 */
function stageAt(s: ConvertStage) {
  return `${Math.max(0, Math.round((s.ts - convertStartedAt.value) / 1000))}s`;
}

function fmtElapsed(sec: number) {
  return sec < 60 ? `${sec} 秒` : `${Math.floor(sec / 60)} 分 ${sec % 60} 秒`;
}

async function lightweight(g: SimGeometry) {
  converting.value = g.id;
  error.value = "";
  convertStages.value = [];
  convertFailedAt.value = "";
  convertStartedAt.value = Date.now();
  convertElapsed.value = 0;
  window.clearInterval(elapsedTimer);
  // 计时是"卡住"的唯一客观依据：vektor3d 静默时页面至少能说出静默了多久
  elapsedTimer = window.setInterval(() => {
    convertElapsed.value = Math.round((Date.now() - convertStartedAt.value) / 1000);
  }, 1000);

  stage("申请转换票据", "申请转换票据");
  try {
    // 票据只对这一个 gid、这两个端点有效，半小时过期——不把会话 JWT 交出去
    const ticket = await simApi.convertTicket(g.id);
    stage("已交付 vektor3d", `已交付 vektor3d：${ticket.sourceName}（源文件与产物由它直连收发）`);
    // 气囊平面图是纯线框（只有曲线，没有曲面/实体），B-rep 三角化对它必然产出为空。
    // 线框得按线框画：换 mesh.airbag.preview，离散曲线出线段 GLB 并按识别出的
    // 囊袋/腔体/导流袋着色——让人在花几十秒建网格**之前**就看出算法认对没有。
    // 票据与落点完全复用轻量化那套，产物同样落成 lightweight_file。
    const flat = isAirbagFlat(g);
    const result = await vektor3d.runJob<ConvertResult>(
      flat ? "mesh.airbag.preview" : "geometry.convert",
      {
        sourceUrl: ticket.sourceUrl,
        uploadUrl: ticket.uploadUrl,
        authToken: ticket.token,
        sourceName: ticket.sourceName,
        ...(flat ? {} : { options: { unit: "mm" } }),
      },
      {
        // 幂等键带上 gid：页面刷新后重复点不会真的转两遍
        idempotencyKey: `sdm-geometry-${flat ? "flat-" : ""}${g.id}`,
        onProgress: (p, job) => {
          const step = p
            ? p.step
            : job.queuePosition != null
              ? "排队中"
              : "转换中";
          const text = p
            ? `${p.step}${p.detail ? ` · ${p.detail}` : ""}`
            : job.queuePosition != null
              ? `排队中（第 ${job.queuePosition + 1} 位）`
              : "转换中";
          if (lastStage()?.text !== text) stage(step, text, "remote");
        },
      }
    );
    const seg = (result as unknown as { segments?: number }).segments;
    stage("产物已回传", seg != null
      ? `平面图预览已回传：${seg} 条线段`
      : `产物已回传：${(result.bytes / 1048576).toFixed(1)} MB / ${result.triangleCount} 三角面`);
    convertResult.value = { ...convertResult.value, [g.id]: result };
    if (result.warnings?.length) {
      error.value = `转换完成，但有提示：${result.warnings.join("；")}`;
    }
    await load();
    convertStages.value = [];
  } catch (e) {
    // 保留时间线：错误本身往往只有一句网络层措辞，落在哪个阶段才是线索
    convertFailedAt.value = convertStep();
    const raw = e instanceof Vektor3dError ? `vektor3d：${e.message}` : errMsg(e);
    // 纯线框 IGES（气囊平面展开图就是）没有任何曲面/实体，轻量化必然产出为空。
    // 这不是故障而是用错了操作，但原始措辞是 "triangleCount=0" 这类内部读数，
    // 看不出该怎么办 —— 直接把下一步指出来。
    const emptyProduct = /triangleCount=0|faceCount=0|bodies=0|产物不可用/.test(raw);
    error.value = emptyProduct && isAirbagFlat(g)
      ? `${raw}\n—— 这份 IGES 是纯线框（只有曲线，没有曲面/实体），轻量化取不到可渲染的面。`
        + `若它是气囊平面展开图，请改用「生成网格」直接生成 LS-DYNA deck。`
      : raw;
  } finally {
    window.clearInterval(elapsedTimer);
    converting.value = null;
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

// —— 气囊平面图 → deck ——
// profile 是**逐图拟合参数集**：算法通用，但"哪块是拉带、哪条曲线是窄条"依赖
// 具体图纸。generic 只用层级等通用规则，认不出拉带是预期行为而非失败。
const AIRBAG_PROFILES = [
  { id: "generic", label: "通用（只认层级）" },
  { id: "5p-bag", label: "5P-BAG" },
];
const airbagProfile = ref("5p-bag");
const meshing = ref("");
const meshStage = ref("");

function isAirbagFlat(g: SimGeometry) {
  return g.source_type === "airbag_flat"
    || !!g.source_file?.name?.toLowerCase().match(/\.(igs|iges)$/);
}

async function airbagMesh(g: SimGeometry) {
  meshing.value = g.id;
  error.value = "";
  meshStage.value = "申请票据";
  try {
    const t = await simApi.airbagTicket(g.id);
    meshStage.value = "已交付 vektor3d";
    const r = await vektor3d.runJob<{
      ok: boolean;
      mesh?: { nodes: number; elements: number };
      check?: { volume_l: number | null; nonmanifold: number };
      generic_only?: boolean;
    }>(
      "mesh.airbag.generate",
      {
        sourceUrl: t.sourceUrl,
        authToken: t.token,
        sourceName: t.sourceName,
        uploadUrl: t.deckUploadUrl,
        profile: airbagProfile.value,
      },
      {
        // 幂等键带 gid + profile：换 profile 是一次新的作业，不该复用上次结果
        idempotencyKey: `sdm-airbag-${g.id}-${airbagProfile.value}`,
        // 只显示粗粒度阶段：能力侧的 detail 是 Python 日志原文（"边翻转: 0 次"
        // 之类），对使用者是噪声。要排查时看 vektor3d 的作业进度时间线。
        onProgress: (p) => { if (p?.step) meshStage.value = p.step; },
      }
    );
    const nm = r.check?.nonmanifold ?? 0;
    notice.value = nm === 0
      ? `网格已生成并登记为新几何版本：${r.mesh?.nodes ?? 0} 节点 / ${r.mesh?.elements ?? 0} 单元`
        + (r.check?.volume_l ? `，闭合体积 ${r.check.volume_l.toFixed(3)} L` : "")
      : `网格已生成，但有 ${nm} 条非流形边 —— 缝合没接上，提交前需处理`;
    if (r.generic_only) {
      notice.value += "（通用规则：拉带等未识别）";
    }
    await load();
  } catch (e) {
    error.value = e instanceof Vektor3dError ? `vektor3d：${e.message}` : errMsg(e);
  } finally {
    meshing.value = "";
    meshStage.value = "";
  }
}

async function delGeometry(g: SimGeometry) {
  // upload 与 from-path 的后果不同：前者会清掉上传的源文件，后者只解除登记
  const fileHint =
    g.source_type === "upload"
      ? "上传的源文件与网格、轻量化产物将一并清理。"
      : "集群上的源文件不受影响，仅解除登记并清理网格、轻量化产物。";
  if (!confirm(`删除几何版本 v${g.version_no}（${g.source_file?.name ?? "无源文件"}）？${fileHint}`)) return;
  error.value = "";
  try {
    await simApi.deleteGeometry(g.id);
    if (openMesh.value === g.id) openMesh.value = null;
    await load();
  } catch (e) {
    error.value = errMsg(e);
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

    <div v-if="error" class="mb-3 px-3 py-2 rounded-md bg-rose-50 text-rose-700 text-sm whitespace-pre-line">
      {{ error }}
    </div>
    <div v-else-if="notice" class="mb-3 px-3 py-2 rounded-md bg-emerald-50 text-emerald-800 text-sm">
      {{ notice }}
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
        <template v-for="g in geometries" :key="g.id">
        <tr class="border-b border-slate-100">
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
              <!-- 气囊平面图:直接生成 deck。产物登记为**新的几何版本**,
                   故渲染预览复用现成的 deck→GLB 链路,不必另做一套 -->
              <template v-if="isAirbagFlat(g)">
                <select
                  v-model="airbagProfile"
                  class="rounded border px-1 py-0.5 text-xs"
                  :disabled="!!meshing"
                  title="逐图拟合参数集。算法通用,但拉带位置、按实体号点名的窄件依赖具体图纸"
                >
                  <option v-for="p in AIRBAG_PROFILES" :key="p.id" :value="p.id">
                    {{ p.label }}
                  </option>
                </select>
                <button
                  class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs text-emerald-700 hover:bg-emerald-50 disabled:opacity-40 disabled:hover:bg-transparent"
                  :disabled="!vkUsable() || !!meshing"
                  :title="
                    vkUsable()
                      ? '经 vektor3d 把平面展开图直接网格化成 LS-DYNA deck；平面图与产物由它与本服务直连收发'
                      : 'vektor3d 未连接或本页面未被授权，先在上方「连接设置」处理'
                  "
                  @click="airbagMesh(g)"
                >
                  <Loader2 v-if="meshing === g.id" :size="12" class="animate-spin" />
                  <Wand2 v-else :size="12" />
                  {{ meshing === g.id ? (meshStage || "生成中") : "生成网格" }}
                </button>
              </template>
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
                {{ converting === g.id ? "转换中" : (isAirbagFlat(g) ? "预览平面图" : "轻量化") }}
              </button>
              <!-- 网格:属于几何版本,故就近展开而不另设顶级 tab -->
              <button
                class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs hover:bg-slate-50"
                :class="openMesh === g.id ? 'text-sky-700 bg-sky-50' : 'text-slate-500'"
                :disabled="!vkUsable()"
                :title="
                  vkUsable()
                    ? '分析零件形态、生成网格、在 ANSA 中微调'
                    : 'vektor3d 未连接或本页面未被授权，先在上方「连接设置」处理'
                "
                @click="openMesh = openMesh === g.id ? null : g.id"
              >
                <Grid3x3 :size="12" /> 网格
              </button>
              <a
                :href="simApi.sourceDownloadUrl(g.id)"
                class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs text-slate-500 hover:bg-slate-50"
                title="下载源文件"
              >
                <Download :size="12" /> 源文件
              </a>
              <button
                class="p-1 rounded hover:bg-rose-50 text-slate-400 hover:text-rose-600"
                title="删除该几何版本（网格与产物一并清理）"
                @click="delGeometry(g)"
              >
                <Trash2 :size="13" />
              </button>
            </div>
          </td>
        </tr>
        <tr v-if="openMesh === g.id">
          <td colspan="6" class="bg-slate-50/60 px-3 py-3">
            <MeshPanel
              :geometry="g"
              @preview="(p) => emit('previewMesh', { ...p, geometry: g })"
              @refresh="load"
            />
          </td>
        </tr>
        </template>
      </tbody>
    </table>

    <!-- 转换阶段时间线：vektor3d 回报的阶段（下载 → 解析 → 生成 glTF → 回传）
         与 SDM 自己记的本地阶段并列，各带耗时。失败时保留，用于定位死在哪一段。 -->
    <div
      v-if="convertStages.length"
      class="mt-3 px-3 py-2 rounded-md text-xs"
      :class="convertFailedAt ? 'bg-rose-50 text-rose-900' : 'bg-indigo-50 text-indigo-900'"
    >
      <div class="flex items-center gap-2">
        <Loader2 v-if="converting" :size="13" class="animate-spin shrink-0" />
        <AlertTriangle v-else :size="13" class="shrink-0" />
        <span class="font-medium">
          {{ convertFailedAt ? `转换中断于「${convertFailedAt}」` : "轻量化进行中" }}
        </span>
        <span class="ml-auto shrink-0 tabular-nums opacity-70">
          已用 {{ fmtElapsed(convertElapsed) }}
        </span>
      </div>
      <ol class="mt-1.5 space-y-0.5">
        <li
          v-for="(s, i) in convertStages"
          :key="i"
          class="flex items-baseline gap-2"
          :class="convertFailedAt && i === convertStages.length - 1 ? 'font-medium' : 'opacity-80'"
        >
          <span class="w-10 shrink-0 text-right tabular-nums opacity-60">{{ stageAt(s) }}</span>
          <span
            class="shrink-0 px-1 rounded text-[10px]"
            :class="s.from === 'remote' ? 'bg-white/70' : 'bg-black/5'"
            :title="s.from === 'remote' ? 'vektor3d 回报' : 'SDM 本地阶段'"
          >{{ s.from === "remote" ? "vk" : "sdm" }}</span>
          <span class="break-all">{{ s.text }}</span>
        </li>
      </ol>
      <p v-if="convertFailedAt" class="mt-1.5 opacity-80">
        时间线停在此处即为断点：若最后一条是 sdm 阶段，说明 vektor3d 未回报任何进度，
        请查看桌面端 vektor3d 的日志。
      </p>
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
