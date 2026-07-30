<script setup lang="ts">
/**
 * 客户需求 + 质量卡实例。
 *
 * 这两块是同一条链的两端：客户的 CAE 分析规范/技术协议进来，最终要变成一张能
 * 驱动 ANSA 批处理、也能喂给 mesh.check 的质量卡。中间那步「AI 读需求 → 选模板 →
 * 生成实例」尚未接入（缺真实需求文档样本），所以现在是**人工选模板 + 手工填依据**。
 * AI 到位后替掉的是「填依据」这一步，不是换一条链。
 *
 * 一条贯穿的规矩：**每项覆盖都必须有依据出处**。没出处的阈值就是编的，
 * 而这个数字会一路流进网格验收。
 */
import { computed, onMounted, ref } from "vue";
import { simApi, errMsg } from "@/api";
import { vektor3d, Vektor3dError } from "@/api/vektor3d";
import DocPreview from "./DocPreview.vue";
import type {
  SimQualityCard,
  SimRequirementItem,
  SimQualityCardDetail,
  SimQualityTemplate,
  SimRequirementDoc,
} from "@/api/types";
import {
  ChevronRight, Check, Download, Eye, FileText, FileUp, Loader2, Pencil, Plus,
  Ruler, ScanText, Search, Sparkles, Trash2, Upload, X,
} from "lucide-vue-next";

const props = defineProps<{ pid: string }>();

const docs = ref<SimRequirementDoc[]>([]);
const cards = ref<SimQualityCard[]>([]);
const templates = ref<SimQualityTemplate[]>([]);
const loading = ref(true);
const error = ref("");
// 与 error 分开:AI 回落不是"操作失败",是"成功了但降级了"。
// 更要紧的是 load() 开头会清空 error —— 回落后紧接着刷新列表,
// 消息刚显示就被自己抹掉,表现为"报错闪一下就没了"。notice 不参与那次清空。
const notice = ref("");
const uploading = ref(false);
const progress = ref(0);
const fileInput = ref<HTMLInputElement | null>(null);
const docType = ref("agreement");

const DOC_TYPES: { value: string; label: string }[] = [
  { value: "agreement", label: "技术协议" },
  { value: "spec", label: "分析规范" },
  { value: "standard", label: "行业标准" },
  { value: "other", label: "其他" },
];

const ANALYSIS_TEXT: Record<string, string> = {
  pending: "待分析",
  analyzing: "分析中",
  done: "已分析",
  failed: "分析失败",
};

async function load() {
  loading.value = true;
  error.value = "";
  try {
    const [d, c, t] = await Promise.all([
      simApi.listRequirements(props.pid),
      simApi.listQualityCards(props.pid),
      simApi.listQualityTemplates(),
    ]);
    docs.value = d;
    cards.value = c;
    templates.value = t;
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
    await simApi.uploadRequirement(props.pid, file, docType.value, (p) => (progress.value = p));
    await load();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    uploading.value = false;
    input.value = "";
  }
}

async function delDoc(d: SimRequirementDoc) {
  if (!confirm(`删除需求文档「${d.name}」？`)) return;
  try {
    await simApi.deleteRequirement(d.id);
    await load();
  } catch (e) {
    error.value = errMsg(e);
  }
}

// ── 需求条目 ─────────────────────────────────────────────────────────────
// 条目是需求进入平台后的最小可追溯单元：带原文出处、量纲化指标、是门槛还是参考。
// 当前抽取走确定性规则（extractor: "rule"），AI 尚未接入。
const items = ref<Record<string, SimRequirementItem[]>>({});
const extracting = ref<string | null>(null);
const openDoc = ref<string | null>(null);

const CATEGORY_TEXT: Record<string, string> = {
  subject: "工况",
  loading: "加载条件",
  mesh: "网格",
  delivery: "交付",
  other: "其他",
};
const SOURCE_TEXT: Record<string, string> = { ai: "AI", rule: "规则", manual: "人工" };
const METRIC_KIND_TEXT: Record<string, string> = {
  internal: "内控",
  external: "对外",
  override: "例外",
};

async function toggleDoc(d: SimRequirementDoc) {
  if (openDoc.value === d.id) {
    openDoc.value = null;
    return;
  }
  openDoc.value = d.id;
  if (!items.value[d.id]) {
    try {
      items.value = { ...items.value, [d.id]: await simApi.listRequirementItems(d.id) };
    } catch (e) {
      error.value = errMsg(e);
    }
  }
}

const extractStep = ref("");
/** AI 的疑点从落库的 analysis 里取,不再靠前端内存——刷新后仍在 */
function docNotes(d: SimRequirementDoc): string[] {
  const raw = (d.analysis as { notes?: unknown } | null)?.notes;
  return Array.isArray(raw) ? raw.map(String) : [];
}

/** 规则/AI 交叉核对统计。AI 回写时服务端独立跑规则抽取合并的结果,见后端 merge.py */
function docMerge(d: SimRequirementDoc) {
  const m = (d.analysis as { merge?: unknown } | null)?.merge;
  return m && typeof m === "object"
    ? (m as { agreed: number; conflicts: number; ruleOnly: number; aiOnly: number })
    : null;
}

/**
 * 解析需求文档 → 需求条目。
 *
 * **优先走 vektor3d 的 doc.analyze**：它有完整的文档解析器（pptx/docx/xlsx/pdf，
 * 扫描件还能走视觉模型），并由 agent 理解自由文本里的约束与工况关联。SDM 这边的
 * 规则抽取只认 pptx 的规整表格。
 *
 * **vektor3d 不可用时回落到规则抽取**，而不是直接失败——有一条可复现的基线，
 * 总好过整条链瘫掉。两者产出落同一 schema，`extracted_by` 标明是谁抽的。
 */
async function extract(d: SimRequirementDoc, { preferAi = true } = {}) {
  extracting.value = d.id;
  extractStep.value = "";
  error.value = "";
  notice.value = "";
  try {
    if (preferAi && vkUsable()) {
      try {
        await extractByAi(d);
        return;
      } catch (e) {
        // AI 这条路断了就回落，但必须把原因留在页面上——静默降级会让人以为 AI 跑过了。
        // 细节同时打进控制台:能力返回的原始错误往往比这一行更能定位问题。
        const detail = e instanceof Vektor3dError ? e.message : errMsg(e);
        console.error("[需求解析] AI 解析失败，回落规则抽取：", e);
        notice.value = `AI 解析失败，已回落到规则抽取：${detail}`;
      }
    }
    extractStep.value = "规则抽取…";
    const out = await simApi.extractRequirementItems(d.id);
    items.value = { ...items.value, [d.id]: out.items };
    openDoc.value = d.id;
    await load();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    extracting.value = null;
    extractStep.value = "";
  }
}

async function extractByAi(d: SimRequirementDoc) {
  extractStep.value = "申请只读票据…";
  const ticket = await simApi.analyzeTicket(d.id);
  const result = await vektor3d.runJob<{
    items: Record<string, unknown>[];
    notes?: string[];
    itemCount: number;
    projectCode: string;
    parserType: string;
    loadPointCoordsAvailable: boolean;
  }>(
    "doc.analyze",
    {
      projectId: props.pid,
      name: projectName.value,
      sourceUrl: ticket.sourceUrl,
      authToken: ticket.token,
      sourceName: ticket.sourceName,
    },
    {
      idempotencyKey: `sdm-doc-${d.id}`,
      onProgress: (p, job) => {
        extractStep.value = p
          ? `${p.step}${p.detail ? ` · ${p.detail}` : ""}`
          : job.queuePosition != null
            ? `排队中（第 ${job.queuePosition + 1} 位）`
            : "分析中…";
      },
    }
  );

  extractStep.value = "写回条目…";
  const out = await simApi.putRequirementItems(d.id, result.items, {
    projectCode: result.projectCode,
    parserType: result.parserType,
    loadPointCoordsAvailable: result.loadPointCoordsAvailable,
    // notes 必须随 summary 落库:它记的是"AI 在哪里拿不准、为什么这么判",
    // 只留在前端内存里,刷新一次就没了——而这些恰恰是要拿去和客户对的东西。
    notes: result.notes ?? [],
  });
  items.value = { ...items.value, [d.id]: out.items };
  openDoc.value = d.id;
  await load();
}

// ── vektor3d 连接（AI 解析要靠它）─────────────────────────────────────────
const vkHealth = ref<Awaited<ReturnType<typeof vektor3d.health>> | null>(null);
const vkError = ref("");
const projectName = ref("");

// 能力目录里 doc.analyze 的就绪状态。
// ⚠ 不能只看 health.notReady:那份清单只列**已注册但未就绪**的能力,
// 而旧版 vektor3d 压根没有 doc.analyze —— 它不会出现在 notReady 里,
// 于是"没有这个能力"被误判成"能力可用",点下去才在提交作业时撞上"未知能力"。
// 必须查能力目录里到底有没有它。
const vkDocAnalyze = ref<{ present: boolean; ready: boolean; reason: string }>({
  present: false, ready: false, reason: "",
});

function vkUsable(): boolean {
  return !!vkHealth.value?.authorized && vkDocAnalyze.value.present && vkDocAnalyze.value.ready;
}

/** 未就绪的原因，直接说给用户看——「连不上」和「版本旧」要给完全不同的提示 */
function vkReason(): string {
  if (vkError.value) return vkError.value;
  if (!vkHealth.value?.authorized) return vkHealth.value?.authMessage || "本页面未被授权";
  if (!vkDocAnalyze.value.present) {
    return "该 vektor3d 版本没有 doc.analyze 能力，需升级到含「CAE 分析要求解析」的版本";
  }
  if (!vkDocAnalyze.value.ready) return vkDocAnalyze.value.reason || "AI 引擎未就绪";
  return "";
}

async function checkVektor3d() {
  try {
    vkHealth.value = await vektor3d.health();
    vkError.value = "";
    const caps = await vektor3d.capabilities();
    const doc = caps.find((c) => c.id === "doc.analyze");
    vkDocAnalyze.value = {
      present: !!doc,
      ready: !!doc?.ready,
      reason: doc?.notReadyReason || "",
    };
  } catch (e) {
    vkHealth.value = null;
    vkDocAnalyze.value = { present: false, ready: false, reason: "" };
    vkError.value = e instanceof Error ? e.message : String(e);
  }
}

// ── 在线预览 ─────────────────────────────────────────────────────────────
const previewing = ref<SimRequirementDoc | null>(null);

async function confirmItem(it: SimRequirementItem) {
  try {
    const updated = await simApi.updateRequirementItem(it.id, {
      status: it.status === "confirmed" ? "draft" : "confirmed",
    });
    const list = (items.value[it.doc_id] ?? []).map((x) => (x.id === it.id ? updated : x));
    items.value = { ...items.value, [it.doc_id]: list };
  } catch (e) {
    error.value = errMsg(e);
  }
}

// ── 条目检索与过滤 ───────────────────────────────────────────────────────
// 一份技术协议动辄二三十条,平铺看不动。筛选维度直接取自条目本身的字段,
// 不另造分类——用户在表里看到什么,就能按什么筛。
const query = ref("");
const filterCategory = ref("");
const filterBaseline = ref("");
const filterFlag = ref("");     // clarify=待澄清 | confirmed=已确认 | open=未确认
const filterSource = ref("");   // rule | ai | manual

const hasFilter = computed(() =>
  !!(query.value.trim() || filterCategory.value || filterBaseline.value
     || filterFlag.value || filterSource.value)
);

function clearFilters() {
  query.value = "";
  filterCategory.value = "";
  filterBaseline.value = "";
  filterFlag.value = "";
  filterSource.value = "";
}

/** 点同一个筛选器再点一次 = 取消,省一个"清除"按钮 */
function toggle(target: "baseline" | "flag" | "category" | "source", value: string) {
  const map = {
    baseline: filterBaseline,
    flag: filterFlag,
    category: filterCategory,
    source: filterSource,
  } as const;
  const ref_ = map[target];
  ref_.value = ref_.value === value ? "" : value;
}

/**
 * 关键词匹配范围要够宽:工程师可能搜「50mm」「T29」「第7页」「膝碰」,
 * 这些分别落在指标、scope、出处、标题里。只搜标题等于没搜。
 */
function matchesQuery(it: SimRequirementItem, q: string): boolean {
  if (!q) return true;
  const hay = [
    it.title,
    it.raw_text,
    it.source_ref,
    it.project_note ?? "",
    it.clarification_hint ?? "",
    ...it.metrics.map((m) => `${m.quantity}${m.op}${m.value}${m.unit}${m.scope}${m.raw}`),
  ].join(" ").toLowerCase();
  return hay.includes(q);
}

function visibleItems(docId: string): SimRequirementItem[] {
  const list = items.value[docId] ?? [];
  const q = query.value.trim().toLowerCase();
  return list.filter((it) => {
    if (filterCategory.value && it.category !== filterCategory.value) return false;
    if (filterBaseline.value && it.baseline !== filterBaseline.value) return false;
    if (filterSource.value && it.extracted_by !== filterSource.value) return false;
    if (filterFlag.value === "clarify" && !it.needs_clarification) return false;
    if (filterFlag.value === "confirmed" && it.status !== "confirmed") return false;
    if (filterFlag.value === "open" && it.status === "confirmed") return false;
    return matchesQuery(it, q);
  });
}

/** 各分类的条数,用于筛选器上的角标——为 0 的分类不显示,免得点了一片空白 */
function categoryCounts(docId: string): { key: string; label: string; count: number }[] {
  const list = items.value[docId] ?? [];
  return Object.keys(CATEGORY_TEXT)
    .map((key) => ({
      key,
      label: CATEGORY_TEXT[key],
      count: list.filter((i) => i.category === key).length,
    }))
    .filter((c) => c.count > 0);
}

// ── 质量卡实例 ───────────────────────────────────────────────────────────
const showCreate = ref(false);
const creating = ref(false);
const form = ref({ name: "", template_id: "generic-crash-5mm", requirement_doc_id: "" });
const overrides = ref<{ target: string; new_value: string; source: string }[]>([]);
const preview = ref<SimQualityCardDetail | null>(null);
const expanded = ref<string | null>(null);
const expandedDetail = ref<SimQualityCardDetail | null>(null);

/** 可覆盖项：生成侧取常用旋钮，判定侧取模板里启用的判据 */
const overridableTargets = computed(() => {
  const out: { value: string; label: string }[] = [
    { value: "mesh:target_element_length", label: "目标单元尺寸 (mm)" },
    { value: "mesh:general_min_target_len", label: "单元尺寸下限 (mm)" },
    { value: "mesh:general_max_target_len", label: "单元尺寸上限 (mm)" },
  ];
  for (const c of preview.value?.criteria ?? []) {
    out.push({
      value: `criteria:${c.name} [${c.domain}]:failed`,
      label: `${c.name} 判废线${c.calculation ? `（${c.calculation}）` : ""}`,
    });
  }
  return out;
});

async function openCreate() {
  showCreate.value = true;
  overrides.value = [];
  form.value.name = "";
  await loadPreview();
}

async function loadPreview() {
  try {
    preview.value = await simApi.getQualityTemplate(form.value.template_id);
  } catch (e) {
    error.value = errMsg(e);
  }
}

async function createCard() {
  if (!form.value.name.trim()) return;
  creating.value = true;
  error.value = "";
  try {
    await simApi.createQualityCard(props.pid, {
      name: form.value.name.trim(),
      template_id: form.value.template_id,
      requirement_doc_id: form.value.requirement_doc_id || null,
      overrides: overrides.value.filter((o) => o.target && o.new_value && o.source),
    });
    showCreate.value = false;
    await load();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    creating.value = false;
  }
}

async function toggleCard(c: SimQualityCard) {
  if (expanded.value === c.id) {
    expanded.value = null;
    return;
  }
  expanded.value = c.id;
  expandedDetail.value = null;
  try {
    expandedDetail.value = (await simApi.getQualityCard(c.id)).card ?? null;
  } catch (e) {
    error.value = errMsg(e);
  }
}

async function delCard(c: SimQualityCard) {
  if (!confirm(`删除质量卡「${c.name}」？其 ANSA 卡文件将一并删除。`)) return;
  try {
    await simApi.deleteQualityCard(c.id);
    await load();
  } catch (e) {
    error.value = errMsg(e);
  }
}

// ── 导入客户质量卡文件 ───────────────────────────────────────────────────
const showImport = ref(false);
const importing = ref(false);
const importForm = ref({ template_id: "", name: "", source: "", scope: "" });
const qualFile = ref<File | null>(null);
const mparFile = ref<File | null>(null);

async function doImport() {
  if (!qualFile.value || !importForm.value.template_id.trim() || !importForm.value.name.trim()) return;
  importing.value = true;
  error.value = "";
  try {
    await simApi.importQualityTemplate({
      template_id: importForm.value.template_id.trim(),
      name: importForm.value.name.trim(),
      qualFile: qualFile.value,
      mparFile: mparFile.value,
      source: importForm.value.source,
      scope: importForm.value.scope,
    });
    showImport.value = false;
    qualFile.value = null;
    mparFile.value = null;
    await load();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    importing.value = false;
  }
}

// ── 在线编辑已派生的实例 ─────────────────────────────────────────────────
const editing = ref<SimQualityCard | null>(null);
const edits = ref<{ target: string; new_value: string; source: string }[]>([]);
const saving = ref(false);

function startEdit(c: SimQualityCard) {
  editing.value = c;
  edits.value = [{ target: "", new_value: "", source: "" }];
  if (!preview.value || preview.value.id !== c.template_id) {
    simApi.getQualityTemplate(c.template_id).then((d) => (preview.value = d)).catch(() => undefined);
  }
}

async function saveEdits() {
  if (!editing.value) return;
  const valid = edits.value.filter((o) => o.target && o.new_value && o.source);
  if (!valid.length) return;
  saving.value = true;
  error.value = "";
  try {
    await simApi.editQualityCard(editing.value.id, valid);
    editing.value = null;
    await load();
    expanded.value = null;
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    saving.value = false;
  }
}

function fmt(ts: number) {
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
}
function fmtSize(n?: number) {
  if (!n) return "—";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}
/** 时间步用微秒显示——碰撞工程师看的就是这个量级 */
function fmtDt(seconds: number) {
  return seconds > 0 ? `${(seconds * 1e6).toFixed(2)} μs` : "—";
}
function docName(id: string | null) {
  return docs.value.find((d) => d.id === id)?.name ?? "";
}

onMounted(async () => {
  await load();
  checkVektor3d();
  try {
    projectName.value = (await simApi.getProject(props.pid)).name;
  } catch { /* 项目名只用于给 AI 上下文命名，取不到不影响解析 */ }
});
</script>

<template>
  <div class="w-full">
    <div v-if="error" class="mb-3 px-3 py-2 rounded-md bg-rose-50 text-rose-700 text-sm">
      {{ error }}
    </div>
    <div
      v-if="notice"
      class="mb-3 px-3 py-2 rounded-md bg-amber-50 text-amber-800 text-sm flex items-start gap-2"
    >
      <span class="flex-1">{{ notice }}</span>
      <button class="p-0.5 rounded hover:bg-amber-100 shrink-0" title="知道了" @click="notice = ''">
        <X :size="14" />
      </button>
    </div>
    <div v-if="loading" class="flex items-center gap-2 text-slate-500 text-sm py-8 justify-center">
      <Loader2 :size="18" class="animate-spin" /> 加载中…
    </div>

    <template v-else>
      <!-- 需求文档 -->
      <div class="flex items-center gap-2 mb-3">
        <FileText :size="15" class="text-slate-400" />
        <span class="text-sm font-medium text-slate-700">客户需求文档</span>
        <span class="text-xs text-slate-400">CAE 分析规范 / 技术协议，质量卡由它推导</span>
        <select
          v-model="docType"
          class="ml-auto px-2 py-1.5 border border-slate-300 rounded-md text-sm"
        >
          <option v-for="t in DOC_TYPES" :key="t.value" :value="t.value">{{ t.label }}</option>
        </select>
        <input ref="fileInput" type="file" class="hidden" @change="onPick" />
        <button
          class="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50"
          :disabled="uploading"
          @click="fileInput?.click()"
        >
          <Loader2 v-if="uploading" :size="15" class="animate-spin" />
          <Upload v-else :size="15" />
          {{ uploading ? `上传中 ${progress}%` : "上传文档" }}
        </button>
      </div>

      <div
        v-if="extracting && extractStep"
        class="flex items-center gap-2 mb-3 px-3 py-2 rounded-md bg-indigo-50 text-indigo-800 text-xs"
      >
        <Loader2 :size="13" class="animate-spin shrink-0" />
        <span class="truncate">{{ extractStep }}</span>
        <span class="ml-auto text-indigo-500 shrink-0">文档原件由 vektor3d 直连拉取</span>
      </div>
      <p
        v-else-if="docs.length && !vkUsable()"
        class="mb-3 px-3 py-1.5 rounded-md bg-slate-50 border border-slate-200 text-xs text-slate-500"
      >
        AI 解析不可用（{{ vkReason() }}）——将改用规则抽取：只认 pptx 的规整表格，
        自由文本里的约束条件抽不到。
      </p>

      <p v-if="!docs.length" class="text-sm text-slate-400 py-6 text-center border border-dashed border-slate-200 rounded-lg">
        还没有需求文档。上传主机厂的 CAE 分析规范或技术协议。
      </p>
      <table v-else class="w-full text-sm mb-6">
        <thead class="text-slate-500 border-b border-slate-200">
          <tr>
            <th class="text-left font-medium py-2">文件</th>
            <th class="text-left font-medium py-2 w-24">类型</th>
            <th class="text-left font-medium py-2 w-20">大小</th>
            <th class="text-left font-medium py-2 w-24">分析状态</th>
            <th class="text-left font-medium py-2">上传时间</th>
            <th class="w-20"></th>
          </tr>
        </thead>
        <tbody>
          <template v-for="d in docs" :key="d.id">
          <tr class="border-b border-slate-100 hover:bg-slate-50 cursor-pointer" @click="toggleDoc(d)">
            <td class="py-2 text-slate-800 truncate max-w-xs">
              <ChevronRight
                :size="13"
                class="inline -mt-0.5 mr-1 text-slate-400 transition-transform"
                :class="openDoc === d.id ? 'rotate-90' : ''"
              />
              {{ d.name }}
            </td>
            <td class="py-2 text-slate-500">
              {{ DOC_TYPES.find((t) => t.value === d.doc_type)?.label ?? d.doc_type }}
            </td>
            <td class="py-2 text-slate-500">{{ fmtSize(d.source_file?.size) }}</td>
            <td class="py-2">
              <span
                class="px-1.5 py-0.5 rounded text-xs bg-slate-100 text-slate-500"
                title="AI 需求分析尚未接入：目前质量卡由人工选模板 + 手工填依据生成"
              >
                {{ ANALYSIS_TEXT[d.analysis_status] ?? d.analysis_status }}
              </span>
            </td>
            <td class="py-2 text-slate-500">{{ fmt(d.created_at) }}</td>
            <td class="py-2">
              <div class="flex items-center gap-1" @click.stop>
                <button
                  class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs text-blue-600 hover:bg-blue-50 disabled:opacity-40"
                  :disabled="!!extracting"
                  :title="
                    vkUsable()
                      ? '交 vektor3d 的 AI 解析（读原件、理解自由文本）；失败自动回落到规则抽取'
                      : 'vektor3d 未连接，将用规则抽取（只认 pptx 的规整表格）'
                  "
                  @click="extract(d)"
                >
                  <Loader2 v-if="extracting === d.id" :size="12" class="animate-spin" />
                  <Sparkles v-else-if="vkUsable()" :size="12" />
                  <ScanText v-else :size="12" />
                  {{ vkUsable() ? "AI 解析" : "规则解析" }}
                </button>
                <button
                  class="p-1 rounded hover:bg-slate-100 text-slate-400 hover:text-slate-600"
                  title="在线预览"
                  @click="previewing = d"
                ><Eye :size="14" /></button>
                <a
                  :href="simApi.requirementDownloadUrl(d.id)"
                  class="p-1 rounded hover:bg-slate-100 text-slate-400 hover:text-slate-600"
                  title="下载"
                ><Download :size="14" /></a>
                <button
                  class="p-1 rounded hover:bg-rose-50 text-slate-400 hover:text-rose-600"
                  @click="delDoc(d)"
                ><Trash2 :size="14" /></button>
              </div>
            </td>
          </tr>

          <!-- 需求条目 -->
          <tr v-if="openDoc === d.id">
            <td colspan="6" class="bg-slate-50/60 px-4 py-3">
              <p v-if="!(items[d.id] ?? []).length" class="text-xs text-slate-400">
                还没有条目。点「解析」把文档拆成可追溯的需求条目。
              </p>
              <div v-else class="space-y-2">
                <!-- 统计数字本身就是筛选器：想看哪一类，点它 -->
                <div class="flex flex-wrap items-center gap-1.5 text-xs">
                  <span class="text-slate-500 mr-1">
                    共 <b>{{ items[d.id].length }}</b> 条
                    <template v-if="hasFilter">
                      · 显示 <b class="text-blue-600">{{ visibleItems(d.id).length }}</b>
                    </template>
                  </span>

                  <button
                    class="px-1.5 py-0.5 rounded border transition"
                    :class="filterBaseline === 'required'
                      ? 'bg-emerald-600 text-white border-emerald-600'
                      : 'bg-emerald-50 text-emerald-700 border-transparent hover:border-emerald-300'"
                    @click="toggle('baseline', 'required')"
                  >门槛 {{ items[d.id].filter((i) => i.baseline === "required").length }}</button>

                  <button
                    class="px-1.5 py-0.5 rounded border transition"
                    :class="filterBaseline === 'reference'
                      ? 'bg-slate-600 text-white border-slate-600'
                      : 'bg-slate-100 text-slate-600 border-transparent hover:border-slate-300'"
                    @click="toggle('baseline', 'reference')"
                  >参考 {{ items[d.id].filter((i) => i.baseline === "reference").length }}</button>

                  <button
                    v-if="items[d.id].some((i) => i.needs_clarification)"
                    class="px-1.5 py-0.5 rounded border transition"
                    :class="filterFlag === 'clarify'
                      ? 'bg-amber-600 text-white border-amber-600'
                      : 'bg-amber-50 text-amber-700 border-transparent hover:border-amber-300'"
                    title="原文里尚未定死的条款——这些最需要拿去和客户对"
                    @click="toggle('flag', 'clarify')"
                  >待澄清 {{ items[d.id].filter((i) => i.needs_clarification).length }}</button>

                  <button
                    class="px-1.5 py-0.5 rounded border transition"
                    :class="filterFlag === 'open'
                      ? 'bg-sky-600 text-white border-sky-600'
                      : 'bg-sky-50 text-sky-700 border-transparent hover:border-sky-300'"
                    title="还没确认过的条目"
                    @click="toggle('flag', 'open')"
                  >未确认 {{ items[d.id].filter((i) => i.status !== "confirmed").length }}</button>

                  <span class="w-px h-3 bg-slate-200 mx-0.5"></span>

                  <button
                    v-for="c in categoryCounts(d.id)"
                    :key="c.key"
                    class="px-1.5 py-0.5 rounded border transition"
                    :class="filterCategory === c.key
                      ? 'bg-slate-700 text-white border-slate-700'
                      : 'bg-white text-slate-600 border-slate-200 hover:border-slate-400'"
                    @click="toggle('category', c.key)"
                  >{{ c.label }} {{ c.count }}</button>

                  <template v-if="new Set(items[d.id].map((i) => i.extracted_by)).size > 1">
                    <span class="w-px h-3 bg-slate-200 mx-0.5"></span>
                    <button
                      v-for="src in ['ai', 'rule', 'manual']"
                      :key="src"
                      v-show="items[d.id].some((i) => i.extracted_by === src)"
                      class="px-1.5 py-0.5 rounded border transition"
                      :class="filterSource === src
                        ? 'bg-violet-600 text-white border-violet-600'
                        : 'bg-white text-slate-500 border-slate-200 hover:border-violet-300'"
                      title="按抽取来源筛选：规则与 AI 的结果并存时，用它对照两边差异"
                      @click="toggle('source', src)"
                    >{{ SOURCE_TEXT[src] }} {{ items[d.id].filter((i) => i.extracted_by === src).length }}</button>
                  </template>

                  <span
                    v-if="items[d.id].some((i) => i.load_points)"
                    class="px-1.5 py-0.5 rounded bg-slate-100 text-slate-500"
                    title="加载点位的位置画在图上，文字层只有编号——数量可得，坐标必须人工在 CAE 里定"
                  >
                    加载点位 {{ items[d.id].reduce((a, i) => a + i.load_points, 0) }} 个（坐标需人工确定）
                  </span>

                  <div class="ml-auto flex items-center gap-1">
                    <div class="relative">
                      <Search :size="12" class="absolute left-1.5 top-1.5 text-slate-400" />
                      <input
                        v-model="query"
                        class="pl-6 pr-2 py-0.5 border border-slate-300 rounded w-52"
                        placeholder="搜标题/原文/指标/出处，如 50mm、T29、第7页"
                      />
                    </div>
                    <button
                      v-if="hasFilter"
                      class="px-1.5 py-0.5 rounded text-slate-500 hover:bg-slate-100"
                      @click="clearFilters"
                    >清除</button>
                  </div>
                </div>
                <div
                  v-if="docMerge(d)"
                  class="text-xs rounded px-2 py-1.5"
                  :class="docMerge(d)!.conflicts
                    ? 'bg-amber-50/70 text-amber-800'
                    : 'bg-emerald-50/70 text-emerald-800'"
                  title="AI 回写时服务端独立跑了一遍规则抽取做交叉核对：两边独立阅读、代码比对。冲突条目已标为待澄清，两个读数都在条目的澄清提示里；规则兜底是 AI 未覆盖（如视觉解析失败页）、由规则从文字层补上的条目"
                >
                  规则/AI 交叉核对：一致 {{ docMerge(d)!.agreed }} 条
                  <template v-if="docMerge(d)!.conflicts">
                    · <b>读数冲突 {{ docMerge(d)!.conflicts }} 条（已转待澄清）</b>
                  </template>
                  <template v-if="docMerge(d)!.ruleOnly">
                    · 规则兜底 {{ docMerge(d)!.ruleOnly }} 条
                  </template>
                  <template v-if="docMerge(d)!.aiOnly">
                    · 仅 AI 抽到 {{ docMerge(d)!.aiOnly }} 条
                  </template>
                </div>
                <div
                  v-if="docNotes(d).length"
                  class="text-xs bg-amber-50/70 text-amber-800 rounded px-2 py-1.5 space-y-0.5"
                >
                  <div class="font-medium">
                    AI 提出的疑点（{{ docNotes(d).length }} 条，已随分析结果留档）
                  </div>
                  <div v-for="(n, ni) in docNotes(d)" :key="ni">· {{ n }}</div>
                </div>
                <table class="w-full text-xs">
                  <thead class="text-slate-400">
                    <tr>
                      <th class="text-left font-normal py-1 w-16">分类</th>
                      <th class="text-left font-normal py-1">分析项 / 条目</th>
                      <th class="text-left font-normal py-1">指标</th>
                      <th class="text-left font-normal py-1 w-16">基准</th>
                      <th class="text-left font-normal py-1 w-28">出处</th>
                      <th class="w-16"></th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="it in visibleItems(d.id)"
                      :key="it.id"
                      class="border-b border-slate-100 align-top"
                      :class="it.status === 'confirmed' ? 'opacity-60' : ''"
                    >
                      <td class="py-1 text-slate-500">{{ CATEGORY_TEXT[it.category] ?? it.category }}</td>
                      <td class="py-1 text-slate-700">
                        {{ it.title }}
                        <span
                          v-if="it.needs_clarification"
                          class="ml-1 px-1 rounded bg-amber-50 text-amber-700"
                          :title="'原文出现「' + (it.clarification_hint ?? '') + '」，要求尚未定死'"
                        >待澄清</span>
                        <span v-if="it.extracted_by === 'manual'" class="ml-1 px-1 rounded bg-sky-50 text-sky-700">人工</span>
                        <span v-else-if="it.extracted_by === 'ai'" class="ml-1 px-1 rounded bg-violet-50 text-violet-700">AI</span>
                        <span v-else class="ml-1 px-1 rounded bg-slate-100 text-slate-500">规则</span>
                        <div v-if="it.load_points" class="text-slate-400">
                          {{ it.load_points }} 个加载点位<template v-if="it.indenter_diameter_mm">
                          · 压头 φ{{ it.indenter_diameter_mm }}mm</template>
                        </div>
                        <div class="text-slate-400 truncate max-w-md" :title="it.raw_text">{{ it.raw_text }}</div>
                      </td>
                      <td class="py-1">
                        <div v-for="(m, mi) in it.metrics" :key="mi" class="text-slate-600 whitespace-nowrap">
                          <span class="text-slate-400">{{ m.quantity }}</span>
                          <b>{{ m.op }}{{ m.value }}{{ m.unit }}</b>
                          <span
                            v-if="m.kind !== 'target'"
                            class="ml-1 px-1 rounded bg-violet-50 text-violet-700"
                          >{{ METRIC_KIND_TEXT[m.kind] }}{{ m.scope ? "·" + m.scope : "" }}</span>
                        </div>
                      </td>
                      <td class="py-1">
                        <span
                          v-if="it.baseline === 'required'"
                          class="px-1 rounded bg-emerald-50 text-emerald-700"
                        >门槛</span>
                        <span
                          v-else-if="it.baseline === 'reference'"
                          class="px-1 rounded bg-slate-100 text-slate-500"
                        >参考</span>
                        <span v-else class="text-slate-300">—</span>
                      </td>
                      <td class="py-1 text-slate-400">{{ it.source_ref }}</td>
                      <td class="py-1">
                        <button
                          class="p-1 rounded hover:bg-emerald-50 text-slate-400 hover:text-emerald-600"
                          :title="it.status === 'confirmed' ? '撤销确认' : '确认这条'"
                          @click="confirmItem(it)"
                        ><Check :size="13" /></button>
                      </td>
                    </tr>
                  </tbody>
                </table>
                <p
                  v-if="!visibleItems(d.id).length"
                  class="text-xs text-slate-400 py-4 text-center"
                >
                  没有符合条件的条目。<button class="text-blue-600 hover:underline" @click="clearFilters">清除筛选</button>
                </p>
              </div>
            </td>
          </tr>
          </template>
        </tbody>
      </table>

      <!-- 质量卡实例 -->
      <div class="flex items-center gap-2 mb-3">
        <Ruler :size="15" class="text-slate-400" />
        <span class="text-sm font-medium text-slate-700">质量卡实例</span>
        <span class="text-xs text-slate-400">
          既驱动 ANSA 批处理网格，也作为网格体检的判据——同一份，不各存一份
        </span>
        <button
          class="ml-auto flex items-center gap-1 px-3 py-1.5 rounded-md border border-slate-300 text-slate-700 text-sm hover:bg-slate-50"
          title="导入客户自己的 .ansa_qual / .ansa_mpar，成为模板库里的一张模板"
          @click="showImport = true"
        >
          <FileUp :size="15" /> 导入质量卡
        </button>
        <button
          class="flex items-center gap-1 px-3 py-1.5 rounded-md bg-blue-600 text-white text-sm hover:bg-blue-700"
          @click="openCreate"
        >
          <Plus :size="15" /> 从模板派生
        </button>
      </div>

      <p v-if="!cards.length" class="text-sm text-slate-400 py-6 text-center border border-dashed border-slate-200 rounded-lg">
        还没有质量卡。从模板库派生一张，或先上传需求文档再据此调整参数。
      </p>
      <table v-else class="w-full text-sm">
        <thead class="text-slate-500 border-b border-slate-200">
          <tr>
            <th class="text-left font-medium py-2">名称</th>
            <th class="text-left font-medium py-2">基于模板</th>
            <th class="text-left font-medium py-2 w-20">覆盖项</th>
            <th class="text-left font-medium py-2">依据文档</th>
            <th class="text-left font-medium py-2">创建时间</th>
            <th class="w-28"></th>
          </tr>
        </thead>
        <tbody>
          <template v-for="c in cards" :key="c.id">
            <tr class="border-b border-slate-100 hover:bg-slate-50 cursor-pointer" @click="toggleCard(c)">
              <td class="py-2 text-slate-800">{{ c.name }}</td>
              <td class="py-2 text-slate-500 font-mono text-xs">{{ c.template_id }}</td>
              <td class="py-2 text-slate-500">{{ c.overrides?.length ?? 0 }}</td>
              <td class="py-2 text-slate-500 truncate max-w-xs">
                {{ docName(c.derived_from_doc_id) || "—" }}
              </td>
              <td class="py-2 text-slate-500">{{ fmt(c.created_at) }}</td>
              <td class="py-2">
                <div class="flex items-center gap-1" @click.stop>
                  <a
                    :href="simApi.qualityCardExportUrl(c.id, 'qual')"
                    class="px-1.5 py-0.5 rounded text-xs text-blue-600 hover:bg-blue-50"
                    title="导出判定侧 .ansa_qual"
                  >qual</a>
                  <a
                    :href="simApi.qualityCardExportUrl(c.id, 'mpar')"
                    class="px-1.5 py-0.5 rounded text-xs text-blue-600 hover:bg-blue-50"
                    title="导出生成侧 .ansa_mpar"
                  >mpar</a>
                  <button
                    class="p-1 rounded hover:bg-slate-100 text-slate-400 hover:text-slate-600"
                    title="在线编辑阈值与网格参数"
                    @click="startEdit(c)"
                  ><Pencil :size="14" /></button>
                  <button
                    class="p-1 rounded hover:bg-rose-50 text-slate-400 hover:text-rose-600"
                    @click="delCard(c)"
                  ><Trash2 :size="14" /></button>
                </div>
              </td>
            </tr>
            <tr v-if="expanded === c.id">
              <td colspan="6" class="bg-slate-50/60 px-4 py-3">
                <div v-if="!expandedDetail" class="text-xs text-slate-400">加载中…</div>
                <div v-else class="space-y-3">
                  <div class="flex flex-wrap gap-3 text-xs text-slate-600">
                    <span>目标单元 <b>{{ expandedDetail.meshParams.targetElementLength }} mm</b></span>
                    <span>下限 {{ expandedDetail.meshParams.minTargetLength }} / 上限 {{ expandedDetail.meshParams.maxTargetLength }} mm</span>
                    <span>ANSA {{ expandedDetail.ansaVersion }}</span>
                    <span
                      class="px-1.5 py-0.5 rounded bg-amber-50 text-amber-700"
                      title="判废线上的最小单元长度对应的显式时间步。碰撞里这是机时的总闸：单元越小，时间步越短，机时越贵。"
                    >
                      判废线时间步 {{ fmtDt(expandedDetail.timeStepAtFailedMinLength) }}
                    </span>
                  </div>

                  <div v-if="c.overrides?.length">
                    <div class="text-xs font-medium text-slate-600 mb-1">相对模板的改动</div>
                    <table class="w-full text-xs">
                      <tbody>
                        <tr v-for="(o, i) in c.overrides" :key="i" class="border-b border-slate-100">
                          <td class="py-1 font-mono text-slate-600">{{ o.target }}</td>
                          <td class="py-1 text-slate-400">{{ o.old_value }} →</td>
                          <td class="py-1 text-slate-800 font-medium">{{ o.new_value }}</td>
                          <td class="py-1 text-slate-500">依据：{{ o.source }}</td>
                          <td class="py-1 text-slate-400 w-12">{{ o.by }}</td>
                        </tr>
                      </tbody>
                    </table>
                  </div>

                  <div>
                    <div class="text-xs font-medium text-slate-600 mb-1">
                      启用的判据（{{ expandedDetail.criteria.length }} 条）
                    </div>
                    <table class="w-full text-xs">
                      <thead class="text-slate-400">
                        <tr>
                          <th class="text-left font-normal py-1">判据</th>
                          <th class="text-left font-normal py-1" title="同一指标按不同算法族算出的数值不同，卡里规定的不只是阈值">算法</th>
                          <th class="text-left font-normal py-1">Best</th>
                          <th class="text-left font-normal py-1">Good</th>
                          <th class="text-left font-normal py-1">判废</th>
                          <th class="text-left font-normal py-1">Worst</th>
                          <th class="text-left font-normal py-1">方向</th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr v-for="cr in expandedDetail.criteria" :key="cr.name" class="border-b border-slate-100">
                          <td class="py-1 text-slate-700">{{ cr.name }}</td>
                          <td class="py-1 text-slate-500">{{ cr.calculation || "—" }}</td>
                          <td class="py-1 text-slate-500">{{ cr.thresholds.best ?? "—" }}</td>
                          <td class="py-1 text-slate-500">{{ cr.thresholds.good ?? "—" }}</td>
                          <td class="py-1 text-rose-600 font-medium">{{ cr.thresholds.failed ?? "—" }}</td>
                          <td class="py-1 text-slate-500">{{ cr.thresholds.worst ?? "—" }}</td>
                          <td class="py-1 text-slate-400">{{ cr.higherIsBetter ? "越大越好" : "越小越好" }}</td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                </div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </template>

    <!-- 需求文档在线预览（组件懒加载，不进主包） -->
    <DocPreview
      v-if="previewing"
      :url="simApi.requirementDownloadUrl(previewing.id)"
      :file-name="previewing.name"
      @close="previewing = null"
    />

    <!-- 导入客户质量卡文件 -->
    <div
      v-if="showImport"
      class="fixed inset-0 z-40 bg-black/30 flex items-center justify-center p-4"
      @click.self="showImport = false"
    >
      <div class="bg-white rounded-lg shadow-xl w-full max-w-lg p-5 space-y-3">
        <div class="flex items-center">
          <h2 class="font-semibold text-slate-800">导入质量卡</h2>
          <button class="ml-auto p-1 rounded hover:bg-slate-100 text-slate-500" @click="showImport = false">
            <X :size="18" />
          </button>
        </div>
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="block text-sm text-slate-600 mb-1">模板 id</label>
            <input
              v-model="importForm.template_id"
              class="w-full px-3 py-2 border border-slate-300 rounded-md text-sm font-mono"
              placeholder="cust-a-5mm"
            />
          </div>
          <div>
            <label class="block text-sm text-slate-600 mb-1">名称</label>
            <input
              v-model="importForm.name"
              class="w-full px-3 py-2 border border-slate-300 rounded-md text-sm"
              placeholder="客户A 碰撞 5mm 卡"
            />
          </div>
        </div>
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="block text-sm text-slate-600 mb-1">来源</label>
            <input v-model="importForm.source" class="w-full px-3 py-2 border border-slate-300 rounded-md text-sm" placeholder="客户 / 规范号" />
          </div>
          <div>
            <label class="block text-sm text-slate-600 mb-1">适用范围</label>
            <input v-model="importForm.scope" class="w-full px-3 py-2 border border-slate-300 rounded-md text-sm" placeholder="碰撞 / 钣金 / 5mm" />
          </div>
        </div>
        <div>
          <label class="block text-sm text-slate-600 mb-1">
            判定侧 .ansa_qual <span class="text-rose-500">*</span>
          </label>
          <input
            type="file"
            accept=".ansa_qual,.qual,text/plain"
            class="w-full text-sm"
            @change="qualFile = ($event.target as HTMLInputElement).files?.[0] ?? null"
          />
        </div>
        <div>
          <label class="block text-sm text-slate-600 mb-1">生成侧 .ansa_mpar（可选）</label>
          <input
            type="file"
            accept=".ansa_mpar,.mpar,text/plain"
            class="w-full text-sm"
            @change="mparFile = ($event.target as HTMLInputElement).files?.[0] ?? null"
          />
          <p class="text-xs text-slate-400 mt-1">
            不传则沿用内置模板的网格参数，并在说明里注明——塞一份空文件会让人
            误以为客户规定了这些参数。
          </p>
        </div>
        <div class="flex justify-end gap-2 pt-1">
          <button class="px-3 py-1.5 rounded-md text-sm text-slate-600 hover:bg-slate-100" @click="showImport = false">取消</button>
          <button
            class="px-3 py-1.5 rounded-md text-sm bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
            :disabled="importing || !qualFile || !importForm.template_id.trim() || !importForm.name.trim()"
            @click="doImport"
          >{{ importing ? "导入中…" : "导入" }}</button>
        </div>
      </div>
    </div>

    <!-- 在线编辑实例 -->
    <div
      v-if="editing"
      class="fixed inset-0 z-40 bg-black/30 flex items-center justify-center p-4"
      @click.self="editing = null"
    >
      <div class="bg-white rounded-lg shadow-xl w-full max-w-3xl p-5 space-y-3">
        <div class="flex items-center">
          <h2 class="font-semibold text-slate-800">编辑「{{ editing.name }}」</h2>
          <button class="ml-auto p-1 rounded hover:bg-slate-100 text-slate-500" @click="editing = null">
            <X :size="18" />
          </button>
        </div>
        <p class="text-xs text-slate-500 bg-slate-50 rounded px-3 py-2">
          改动会直接写进这张卡的 .ansa_qual / .ansa_mpar，编辑完仍是一份可直接交回
          ANSA 的合法卡。每项都必须填依据。
        </p>
        <div v-for="(o, i) in edits" :key="i" class="flex gap-2">
          <select v-model="o.target" class="flex-1 px-2 py-1.5 border border-slate-300 rounded text-xs">
            <option value="">选择要改的项</option>
            <option v-for="t in overridableTargets" :key="t.value" :value="t.value">{{ t.label }}</option>
          </select>
          <input v-model="o.new_value" class="w-24 px-2 py-1.5 border border-slate-300 rounded text-xs" placeholder="新值" />
          <input v-model="o.source" class="flex-1 px-2 py-1.5 border border-slate-300 rounded text-xs" placeholder="依据，如：评审结论 / 协议 3.2" />
          <button class="p-1 rounded hover:bg-rose-50 text-slate-400 hover:text-rose-600" @click="edits.splice(i, 1)">
            <Trash2 :size="13" />
          </button>
        </div>
        <button
          class="px-2 py-1 rounded text-xs text-blue-600 hover:bg-blue-50"
          @click="edits.push({ target: '', new_value: '', source: '' })"
        >+ 再改一项</button>
        <div class="flex justify-end gap-2 pt-1">
          <button class="px-3 py-1.5 rounded-md text-sm text-slate-600 hover:bg-slate-100" @click="editing = null">取消</button>
          <button
            class="px-3 py-1.5 rounded-md text-sm bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
            :disabled="saving || !edits.some((o) => o.target && o.new_value && o.source)"
            @click="saveEdits"
          >{{ saving ? "保存中…" : "保存" }}</button>
        </div>
      </div>
    </div>

    <!-- 派生质量卡 -->
    <div
      v-if="showCreate"
      class="fixed inset-0 z-40 bg-black/30 flex items-center justify-center p-4"
      @click.self="showCreate = false"
    >
      <div class="bg-white rounded-lg shadow-xl w-full max-w-3xl max-h-[85vh] flex flex-col">
        <div class="flex items-center px-5 py-3 border-b border-slate-200">
          <h2 class="font-semibold text-slate-800">从模板派生质量卡</h2>
          <button class="ml-auto p-1 rounded hover:bg-slate-100 text-slate-500" @click="showCreate = false">
            <X :size="18" />
          </button>
        </div>

        <div class="p-5 overflow-auto space-y-4">
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-sm text-slate-600 mb-1">名称</label>
              <input
                v-model="form.name"
                class="w-full px-3 py-2 border border-slate-300 rounded-md text-sm"
                placeholder="如：X90 座椅碰撞网格卡"
              />
            </div>
            <div>
              <label class="block text-sm text-slate-600 mb-1">基于模板</label>
              <select
                v-model="form.template_id"
                class="w-full px-3 py-2 border border-slate-300 rounded-md text-sm"
                @change="loadPreview"
              >
                <option v-for="t in templates" :key="t.id" :value="t.id">
                  {{ t.name }}{{ t.builtin ? "（内置）" : "" }}
                </option>
              </select>
            </div>
          </div>

          <div>
            <label class="block text-sm text-slate-600 mb-1">依据的需求文档（可选）</label>
            <select
              v-model="form.requirement_doc_id"
              class="w-full px-3 py-2 border border-slate-300 rounded-md text-sm"
            >
              <option value="">不关联</option>
              <option v-for="d in docs" :key="d.id" :value="d.id">{{ d.name }}</option>
            </select>
          </div>

          <div v-if="preview" class="text-xs text-slate-500 bg-slate-50 rounded-md px-3 py-2">
            模板现值：目标单元 <b>{{ preview.meshParams.targetElementLength }} mm</b>，
            启用 {{ preview.criteria.length }} 条判据，判废线时间步
            {{ fmtDt(preview.timeStepAtFailedMinLength) }}。未覆盖的项原样继承。
          </div>

          <div>
            <div class="flex items-center gap-2 mb-2">
              <span class="text-sm text-slate-600">按客户要求覆盖</span>
              <span class="text-xs text-slate-400">每项都必须填依据——无出处的阈值会一路流进网格验收</span>
              <button
                class="ml-auto px-2 py-1 rounded text-xs text-blue-600 hover:bg-blue-50"
                @click="overrides.push({ target: '', new_value: '', source: '' })"
              >
                + 添加一项
              </button>
            </div>
            <div v-for="(o, i) in overrides" :key="i" class="flex gap-2 mb-2">
              <select v-model="o.target" class="flex-1 px-2 py-1.5 border border-slate-300 rounded text-xs">
                <option value="">选择要覆盖的项</option>
                <option v-for="t in overridableTargets" :key="t.value" :value="t.value">{{ t.label }}</option>
              </select>
              <input
                v-model="o.new_value"
                class="w-24 px-2 py-1.5 border border-slate-300 rounded text-xs"
                placeholder="新值"
              />
              <input
                v-model="o.source"
                class="flex-1 px-2 py-1.5 border border-slate-300 rounded text-xs"
                placeholder="依据，如：技术协议 3.2 节"
              />
              <button
                class="p-1 rounded hover:bg-rose-50 text-slate-400 hover:text-rose-600"
                @click="overrides.splice(i, 1)"
              ><Trash2 :size="13" /></button>
            </div>
          </div>
        </div>

        <div class="flex justify-end gap-2 px-5 py-3 border-t border-slate-200">
          <button class="px-3 py-1.5 rounded-md text-sm text-slate-600 hover:bg-slate-100" @click="showCreate = false">
            取消
          </button>
          <button
            class="px-3 py-1.5 rounded-md text-sm bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
            :disabled="creating || !form.name.trim()"
            @click="createCard"
          >
            {{ creating ? "派生中…" : "派生" }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
