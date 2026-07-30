<script setup lang="ts">
/**
 * 几何版本的网格面板：分析零件形态 → 逐零件生成网格 → 合并回装 → 检查/导出 →
 * 交给工程师在 ANSA 里手工微调再提交回来。
 *
 * 与几何面板同一条通路：**调用由本页面发起，文件传输是 vektor3d 与本服务直连的**
 * ——SDM 在集群、vektor3d 在用户桌面只监听 localhost。网格文件比几何更大
 * （.ansa 正本几十 MB、求解器文件上百 MB），更不能经浏览器中转。
 *
 * 两条要点决定了这个面板的形态：
 * ① **.ansa 是正本**，求解器文件只是派生物 —— 所以"导出"随时可再来一次，
 *    不必重新划网格；
 * ② **不整装配一把梭**：先按 ANSA 产品树拆零件、按形态选策略，逐零件生成后再合并。
 *    一个厚件卡死不该拖垮整个装配。
 * 契约见 docs/vektor3d-geometry-capability-contract.md 2.3~2.11。
 */
import { computed, onMounted, ref } from "vue";
import { simApi, errMsg } from "@/api";
import type {
  SimGeometry,
  SimMesh,
  SimMeshStrategyPart,
  SimPartInventoryItem,
} from "@/api/types";
import { vektor3d, Vektor3dError } from "@/api/vektor3d";
import {
  AlertTriangle,
  CheckCircle2,
  Download,
  Eye,
  Grid3x3,
  Layers,
  Loader2,
  PenLine,
  ScanSearch,
  Upload,
} from "lucide-vue-next";

const props = defineProps<{ geometry: SimGeometry }>();
const emit = defineEmits<{
  (e: "preview", payload: { src: string; title: string }): void;
  (e: "refresh"): void;
}>();

const meshes = ref<SimMesh[]>([]);
const loading = ref(true);
const error = ref("");
const notice = ref("");
/** 当前正在跑的动作与进度文案；null 表示空闲 */
const busy = ref<string | null>(null);
const busyStep = ref("");

const inventory = computed<SimPartInventoryItem[]>(() => props.geometry.part_inventory ?? []);
const strategy = computed(() => props.geometry.mesh_strategy);
const analyzed = computed(() => inventory.value.length > 0 || !!strategy.value);

/** 按零件名取策略建议：inventory 是零件粒度，classify 是体粒度，按名字前缀对上 */
function strategyOf(partName: string): SimMeshStrategyPart | undefined {
  const parts = strategy.value?.parts ?? [];
  return (
    parts.find((p) => p.partId === partName) ||
    parts.find((p) => p.partId.startsWith(partName))
  );
}

const MESH_TYPE_TEXT: Record<string, string> = {
  surface: "面网格",
  volume: "体网格",
  midsurface: "中面壳",
};
const STATUS_STYLE: Record<string, string> = {
  generating: "bg-amber-50 text-amber-700",
  ready: "bg-emerald-50 text-emerald-700",
  failed: "bg-rose-50 text-rose-700",
  "checked-out": "bg-sky-50 text-sky-700",
};
const STATUS_TEXT: Record<string, string> = {
  generating: "生成中",
  ready: "就绪",
  failed: "失败",
  "checked-out": "已检出",
};

async function load() {
  loading.value = true;
  try {
    meshes.value = await simApi.listMeshes(props.geometry.id);
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}
onMounted(load);

function onVkError(e: unknown): string {
  return e instanceof Vektor3dError ? `vektor3d：${e.message}` : errMsg(e);
}

function trackProgress(prefix: string) {
  return (p: { step: string; detail: string } | null, job: { queuePosition: number | null }) => {
    busyStep.value = p
      ? `${prefix}：${p.step}${p.detail ? ` · ${p.detail}` : ""}`
      : job.queuePosition != null
        ? `${prefix}：排队中（第 ${job.queuePosition + 1} 位）`
        : `${prefix}…`;
  };
}

/**
 * 分析零件形态：mesh.inventory（ANSA 产品树列零件）+ mesh.classify（逐体选策略）。
 * 两者结论都落在几何版本上——网格还没生成时就该可见，正是它们决定后面怎么划。
 */
async function analyze() {
  busy.value = "analyze";
  error.value = "";
  notice.value = "";
  try {
    const ticket = await simApi.meshTicket(props.geometry.id);
    busyStep.value = "清点零件…";
    const inv = await vektor3d.runJob<{ parts: SimPartInventoryItem[] }>(
      "mesh.inventory",
      { sourceUrl: ticket.sourceUrl, authToken: ticket.token, sourceName: ticket.sourceName },
      { idempotencyKey: `sdm-inventory-${props.geometry.id}`, onProgress: trackProgress("清点零件") }
    );
    const cls = await vektor3d.runJob<Record<string, unknown>>(
      "mesh.classify",
      { sourceUrl: ticket.sourceUrl, authToken: ticket.token, sourceName: ticket.sourceName },
      { idempotencyKey: `sdm-classify-${props.geometry.id}`, onProgress: trackProgress("形态分类") }
    );
    await simApi.setGeometryAnalysis(props.geometry.id, {
      part_inventory: inv.parts ?? [],
      mesh_strategy: cls,
    });
    emit("refresh");
    const review = (cls as { summary?: { needsReviewCount?: number } }).summary?.needsReviewCount ?? 0;
    notice.value = review
      ? `分析完成，其中 ${review} 个零件置信度不足，建议人工确认策略后再生成`
      : "分析完成，可按建议策略生成网格";
  } catch (e) {
    error.value = onVkError(e);
  } finally {
    busy.value = null;
    busyStep.value = "";
  }
}

/** 生成一个零件的网格。partName 为空 = 整份几何一把梭（未分析时的兜底路径） */
async function generate(partName?: string) {
  const sug = partName ? strategyOf(partName) : undefined;
  const meshType = sug?.meshType ?? "surface";
  busy.value = partName ? `gen:${partName}` : "gen:all";
  error.value = "";
  notice.value = "";
  let mid = "";
  try {
    const ticket = await simApi.meshTicket(props.geometry.id);
    // 先登记再跑：网格作业动辄几十分钟，页面得先有一行能显示进度
    const row = await simApi.addMesh(props.geometry.id, {
      mesh_type: meshType,
      mesh_engine: "vektor3d:mesh.generate",
      status: "generating",
      part_filter: partName ?? null,
      mesh_params: sug?.recommendedMinThickness
        ? { minThickness: sug.recommendedMinThickness }
        : undefined,
    });
    mid = row.id;
    meshes.value = [...meshes.value, row];

    const base = `${ticket.meshUrlPrefix}/${mid}/artifact`;
    await vektor3d.runJob<Record<string, unknown>>(
      "mesh.generate",
      {
        sourceUrl: ticket.sourceUrl,
        authToken: ticket.token,
        sourceName: ticket.sourceName,
        uploadUrl: `${base}/solver`,
        ansaUploadUrl: `${base}/ansa`,       // 正本：后续微调/合并/换格式都靠它
        previewUploadUrl: `${base}/preview`,
        reportUploadUrl: `${base}/report`,
        options: {
          meshType,
          solverFormat: "nastran",
          ...(partName ? { partFilter: partName } : {}),
          ...(sug?.recommendedMinThickness
            ? { minThickness: sug.recommendedMinThickness }
            : {}),
        },
      },
      {
        idempotencyKey: `sdm-mesh-${mid}`,
        onProgress: trackProgress(partName ? `生成「${partName}」` : "生成网格"),
      }
    );
    await load();
    notice.value = partName ? `「${partName}」网格已生成` : "网格已生成";
  } catch (e) {
    error.value = onVkError(e);
    // 作业失败要把登记行翻成 failed，否则页面上会永远停在"生成中"
    if (mid) {
      await simApi
        .updateMesh(props.geometry.id, mid, {
          status: "failed",
          quality: { summary: error.value },
        })
        .catch(() => undefined);
      await load();
    }
  } finally {
    busy.value = null;
    busyStep.value = "";
  }
}

/** 逐零件全跑一遍。串行：本机只有一个 ANSA 许可席位，并发只会互相排队 */
async function generateAll() {
  for (const part of inventory.value) {
    const sug = strategyOf(part.name);
    if (sug?.needsReview) continue;   // 待人工的零件不自动跑
    await generate(part.name);
    if (error.value) break;           // 出错就停，不要连环失败刷屏
  }
}

/** 合并回装：逐零件的 .ansa 拼成装配正本（零件已在装配全局坐标，合并不摆位） */
async function merge() {
  const ready = meshes.value.filter((m) => m.status === "ready" && m.ansa_file && m.part_filter);
  if (ready.length < 2) {
    error.value = "至少需要 2 个已生成 .ansa 正本的零件网格才能合并";
    return;
  }
  busy.value = "merge";
  error.value = "";
  notice.value = "";
  let mid = "";
  try {
    const ticket = await simApi.meshTicket(props.geometry.id);
    const row = await simApi.addMesh(props.geometry.id, {
      mesh_type: "assembly",
      mesh_engine: "vektor3d:mesh.merge",
      status: "generating",
      source_mesh_ids: ready.map((m) => m.id),
    });
    mid = row.id;
    const base = `${ticket.meshUrlPrefix}/${mid}/artifact`;
    await vektor3d.runJob<Record<string, unknown>>(
      "mesh.merge",
      {
        ansaUrls: ready.map((m) => `${ticket.meshUrlPrefix}/${m.id}/artifact/ansa`),
        authToken: ticket.token,
        uploadUrl: `${base}/ansa`,
        solverUploadUrl: `${base}/solver`,
        previewUploadUrl: `${base}/preview`,
        options: { solverFormat: "nastran" },
      },
      { idempotencyKey: `sdm-merge-${mid}`, onProgress: trackProgress("合并回装") }
    );
    await load();
    notice.value = `已合并 ${ready.length} 个零件网格`;
  } catch (e) {
    error.value = onVkError(e);
    if (mid) {
      await simApi
        .updateMesh(props.geometry.id, mid, { status: "failed", quality: { summary: error.value } })
        .catch(() => undefined);
      await load();
    }
  } finally {
    busy.value = null;
    busyStep.value = "";
  }
}

/** 质量检查：按质量卡（有就带上）复核既有网格 */
async function check(m: SimMesh) {
  busy.value = `check:${m.id}`;
  error.value = "";
  notice.value = "";
  try {
    const ticket = await simApi.meshTicket(props.geometry.id);
    const r = await vektor3d.runJob<{ violationCount: number; passed: boolean }>(
      "mesh.check",
      {
        meshUrl: `${ticket.meshUrlPrefix}/${m.id}/artifact/solver`,
        meshName: `mesh${m.version_no}.nas`,
        authToken: ticket.token,
        options: { meshFormat: m.solver_format || "nastran" },
      },
      { onProgress: trackProgress("质量检查") }
    );
    await simApi.updateMesh(props.geometry.id, m.id, {
      quality: { ...(m.quality ?? {}), ...r },
    });
    await load();
    notice.value = r.passed
      ? "质量检查通过，无违例单元"
      : `质量检查完成：${r.violationCount} 个违例单元`;
  } catch (e) {
    error.value = onVkError(e);
  } finally {
    busy.value = null;
    busyStep.value = "";
  }
}

/** 换求解器格式导出。.ansa 是正本，导出随时可再来一次，不必重新划网格 */
async function exportAs(m: SimMesh, solverFormat: string) {
  busy.value = `export:${m.id}`;
  error.value = "";
  notice.value = "";
  try {
    const ticket = await simApi.meshTicket(props.geometry.id);
    const base = `${ticket.meshUrlPrefix}/${m.id}/artifact`;
    await vektor3d.runJob<Record<string, unknown>>(
      "mesh.export",
      {
        ansaUrl: `${base}/ansa`,
        authToken: ticket.token,
        uploadUrl: `${base}/solver`,
        previewUploadUrl: `${base}/preview`,
        options: { solverFormat },
      },
      { onProgress: trackProgress(`导出 ${solverFormat}`) }
    );
    await load();
    notice.value = `已导出为 ${solverFormat}`;
  } catch (e) {
    error.value = onVkError(e);
  } finally {
    busy.value = null;
    busyStep.value = "";
  }
}

/** 在本机 ANSA 中打开（检出）。工作副本在工程师机器上，正本仍在 SDM */
async function openInAnsa(m: SimMesh) {
  busy.value = `checkout:${m.id}`;
  error.value = "";
  notice.value = "";
  try {
    const ticket = await simApi.meshTicket(props.geometry.id);
    const r = await vektor3d.runJob<{ checkoutId: string; localPath: string }>(
      "mesh.checkout",
      {
        ansaUrl: `${ticket.meshUrlPrefix}/${m.id}/artifact/ansa`,
        authToken: ticket.token,
        fileName: `mesh-v${m.version_no}.ansa`,
        meta: { gid: props.geometry.id, mid: m.id },
      },
      { onProgress: trackProgress("检出到本机") }
    );
    await simApi.checkoutMesh(props.geometry.id, m.id, r.checkoutId);
    await load();
    notice.value = `已在本机 ANSA 中打开：${r.localPath}。改完保存后点「提交修改」`;
  } catch (e) {
    error.value = onVkError(e);
  } finally {
    busy.value = null;
    busyStep.value = "";
  }
}

/** 提交修改（检入）。显式动作——半成品被自动同步上来比多点一次按钮危险得多 */
async function submitEdits(m: SimMesh) {
  if (!m.checkout_id) return;
  busy.value = `checkin:${m.id}`;
  error.value = "";
  notice.value = "";
  try {
    const ticket = await simApi.meshTicket(props.geometry.id);
    const base = `${ticket.meshUrlPrefix}/${m.id}/artifact`;
    const r = await vektor3d.runJob<{ changed: boolean; elementCount?: number }>(
      "mesh.checkin",
      {
        checkoutId: m.checkout_id,
        authToken: ticket.token,
        uploadUrl: `${base}/ansa`,
        previewUploadUrl: `${base}/preview`,
      },
      { onProgress: trackProgress("提交修改") }
    );
    await simApi.checkinMesh(props.geometry.id, m.id);
    await load();
    notice.value = r.changed
      ? `修改已提交${r.elementCount ? `，当前 ${r.elementCount.toLocaleString()} 单元` : ""}`
      : "工作副本没有变化，已按原样回传并释放占用";
  } catch (e) {
    error.value = onVkError(e);
  } finally {
    busy.value = null;
    busyStep.value = "";
  }
}

function previewMesh(m: SimMesh) {
  emit("preview", {
    src: simApi.meshArtifactUrl(props.geometry.id, m.id, "preview"),
    title: `网格 v${m.version_no}${m.part_filter ? ` · ${m.part_filter}` : ""}`,
  });
}

function elementsOf(m: SimMesh): string {
  const n = (m.quality as { elementCount?: number } | null)?.elementCount;
  return n ? n.toLocaleString() : "—";
}
</script>

<template>
  <div class="space-y-3">
    <!-- 工具条 -->
    <div class="flex flex-wrap items-center gap-2">
      <button
        class="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md border border-slate-300 text-sm hover:bg-slate-50 disabled:opacity-50"
        :disabled="!!busy"
        @click="analyze"
      >
        <Loader2 v-if="busy === 'analyze'" :size="14" class="animate-spin" />
        <ScanSearch v-else :size="14" />
        分析零件形态
      </button>
      <button
        v-if="analyzed && inventory.length"
        class="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md bg-sky-600 text-white text-sm hover:bg-sky-700 disabled:opacity-50"
        :disabled="!!busy"
        @click="generateAll"
      >
        <Grid3x3 :size="14" />
        逐零件生成
      </button>
      <button
        v-else
        class="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md bg-sky-600 text-white text-sm hover:bg-sky-700 disabled:opacity-50"
        :disabled="!!busy"
        @click="generate()"
      >
        <Grid3x3 :size="14" />
        生成网格
      </button>
      <button
        class="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md border border-slate-300 text-sm hover:bg-slate-50 disabled:opacity-50"
        :disabled="!!busy"
        @click="merge"
      >
        <Layers :size="14" />
        合并回装
      </button>
      <span v-if="busyStep" class="inline-flex items-center gap-1.5 text-xs text-slate-500">
        <Loader2 :size="12" class="animate-spin" />{{ busyStep }}
      </span>
    </div>

    <p v-if="error" class="flex items-start gap-1.5 text-xs text-rose-600">
      <AlertTriangle :size="13" class="mt-0.5 shrink-0" />{{ error }}
    </p>
    <p v-if="notice" class="flex items-start gap-1.5 text-xs text-emerald-700">
      <CheckCircle2 :size="13" class="mt-0.5 shrink-0" />{{ notice }}
    </p>

    <!-- 零件清单与策略建议 -->
    <div v-if="analyzed" class="rounded-md border border-slate-200 bg-slate-50/60 p-2.5">
      <div class="text-xs font-medium text-slate-600 mb-1.5">
        零件清单与网格策略
        <span v-if="strategy?.summary" class="font-normal text-slate-400">
          （{{ strategy.summary.partCount }} 个体
          <template v-if="strategy.summary.needsReviewCount">
            · {{ strategy.summary.needsReviewCount }} 个待人工确认
          </template>
          <template v-if="strategy.refineUsed">· 已做 BREP 壁厚复核</template>）
        </span>
      </div>
      <table class="w-full text-xs">
        <tbody>
          <tr v-for="p in inventory" :key="p.index" class="border-t border-slate-200/70">
            <td class="py-1 pr-2 text-slate-700">{{ p.name }}</td>
            <td class="py-1 pr-2 text-slate-400">{{ p.faceCount }} 面</td>
            <td class="py-1 pr-2">
              <span v-if="strategyOf(p.name)" class="text-slate-600">
                {{ MESH_TYPE_TEXT[strategyOf(p.name)!.meshType] ?? strategyOf(p.name)!.meshType }}
                <span
                  v-if="strategyOf(p.name)!.needsReview"
                  class="ml-1 px-1 rounded bg-amber-50 text-amber-700"
                  :title="strategyOf(p.name)!.reasons.join('；')"
                >待人工</span>
                <span v-if="strategyOf(p.name)!.recommendedMinThickness" class="ml-1 text-slate-400">
                  壁厚 {{ strategyOf(p.name)!.recommendedMinThickness }}mm
                </span>
              </span>
              <span v-else class="text-slate-300">—</span>
            </td>
            <td class="py-1 text-right">
              <button
                class="text-sky-600 hover:underline disabled:opacity-40"
                :disabled="!!busy"
                @click="generate(p.name)"
              >
                {{ busy === `gen:${p.name}` ? "生成中…" : "生成" }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 网格版本 -->
    <div v-if="loading" class="text-xs text-slate-400">加载中…</div>
    <div v-else-if="!meshes.length" class="text-xs text-slate-400">
      尚无网格版本。先「分析零件形态」，再按建议策略逐零件生成。
    </div>
    <table v-else class="w-full text-xs">
      <thead class="text-slate-500">
        <tr class="border-b border-slate-200">
          <th class="text-left font-normal py-1">版本</th>
          <th class="text-left font-normal py-1">类型 / 零件</th>
          <th class="text-left font-normal py-1">状态</th>
          <th class="text-left font-normal py-1">单元数</th>
          <th class="text-left font-normal py-1">产物</th>
          <th class="text-right font-normal py-1">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="m in meshes" :key="m.id" class="border-b border-slate-100">
          <td class="py-1.5 text-slate-700">v{{ m.version_no }}</td>
          <td class="py-1.5 text-slate-600">
            {{ MESH_TYPE_TEXT[m.mesh_type] ?? m.mesh_type }}
            <span v-if="m.part_filter" class="text-slate-400">· {{ m.part_filter }}</span>
            <span v-else-if="m.source_mesh_ids?.length" class="text-slate-400">
              · 合并 {{ m.source_mesh_ids.length }} 件
            </span>
          </td>
          <td class="py-1.5">
            <span class="px-1.5 py-0.5 rounded" :class="STATUS_STYLE[m.status] ?? 'bg-slate-100 text-slate-500'">
              {{ STATUS_TEXT[m.status] ?? m.status }}
            </span>
            <span v-if="m.checkout_by" class="ml-1 text-slate-400">{{ m.checkout_by }}</span>
          </td>
          <td class="py-1.5 text-slate-600">{{ elementsOf(m) }}</td>
          <td class="py-1.5 text-slate-400">
            <span v-if="m.ansa_file" class="mr-1.5 text-emerald-600" title=".ansa 正本">正本</span>
            <a
              v-if="m.solver_file"
              :href="simApi.meshArtifactUrl(geometry.id, m.id, 'solver')"
              class="mr-1.5 text-sky-600 hover:underline"
              :title="m.solver_format ?? ''"
            >{{ m.solver_format ?? "求解器" }}</a>
            <a
              v-if="m.report_file"
              :href="simApi.meshArtifactUrl(geometry.id, m.id, 'report')"
              target="_blank"
              class="text-sky-600 hover:underline"
            >报告</a>
          </td>
          <td class="py-1.5 text-right whitespace-nowrap">
            <button
              v-if="m.preview_file"
              class="text-slate-500 hover:text-sky-600 mr-2"
              title="预览网格（带真实单元边线）"
              @click="previewMesh(m)"
            >
              <Eye :size="14" class="inline" />
            </button>
            <button
              v-if="m.solver_file"
              class="text-slate-500 hover:text-sky-600 mr-2 disabled:opacity-40"
              :disabled="!!busy"
              title="按质量卡复核"
              @click="check(m)"
            >
              <Loader2 v-if="busy === `check:${m.id}`" :size="14" class="inline animate-spin" />
              <CheckCircle2 v-else :size="14" class="inline" />
            </button>
            <button
              v-if="m.ansa_file"
              class="text-slate-500 hover:text-sky-600 mr-2 disabled:opacity-40"
              :disabled="!!busy"
              title="导出 LS-DYNA（正本随时可再导，不必重划）"
              @click="exportAs(m, 'lsdyna')"
            >
              <Loader2 v-if="busy === `export:${m.id}`" :size="14" class="inline animate-spin" />
              <Download v-else :size="14" class="inline" />
            </button>
            <button
              v-if="m.ansa_file && !m.checkout_id"
              class="text-slate-500 hover:text-sky-600 mr-2 disabled:opacity-40"
              :disabled="!!busy"
              title="在本机 ANSA 中打开手工微调"
              @click="openInAnsa(m)"
            >
              <Loader2 v-if="busy === `checkout:${m.id}`" :size="14" class="inline animate-spin" />
              <PenLine v-else :size="14" class="inline" />
            </button>
            <button
              v-if="m.checkout_id"
              class="text-sky-600 hover:underline disabled:opacity-40"
              :disabled="!!busy"
              title="把本机改好的网格作为新版本提交回来"
              @click="submitEdits(m)"
            >
              <Loader2 v-if="busy === `checkin:${m.id}`" :size="14" class="inline animate-spin" />
              <Upload v-else :size="14" class="inline" />
              提交修改
            </button>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
