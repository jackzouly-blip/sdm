<script setup lang="ts">
/**
 * 材料库管理页（全局资产，与质量卡模板库同级）。
 *
 * 读对所有登录用户开放；导入/编辑/删除仅管理员——错误的材料数据会污染其后
 * 所有引用它的计算，后端同样强制校验，这里只是不显示入口。
 *
 * 数据分两层：物理材料（列表行）与模型变体（详情里的求解器卡：主卡/伴生
 * NULL 卡）。性能/曲线/卡不提供单点编辑——只能整体重新导入（新修订），
 * 单点改数会让卡原文与结构化参数两处真相分叉。设计见 docs/sdm-material-library.md。
 */
import { computed, onMounted, ref } from "vue";
import { simApi, errMsg } from "@/api";
import type { SimMaterial, SimMaterialCurve, SimMaterialDetail, SimMaterialImportReport } from "@/api/types";
import { useAuthStore } from "@/stores/auth";
import {
  FileText,
  Layers,
  Loader2,
  Pencil,
  Search,
  Trash2,
  Upload,
  X,
} from "lucide-vue-next";

const auth = useAuthStore();
const isAdmin = computed(() => !!auth.me?.is_admin);

const CATEGORY_LABELS: Record<string, string> = {
  steel: "钢",
  aluminum: "铝",
  plastic: "塑料",
  rubber: "橡胶",
  foam: "泡沫/蜂窝",
  glass: "玻璃",
  adhesive: "胶粘",
  other: "其他",
};
const catLabel = (c: string) => CATEGORY_LABELS[c] ?? c;

const materials = ref<SimMaterial[]>([]);
const loading = ref(true);
const error = ref("");
const q = ref("");
const category = ref("");

const categories = computed(() =>
  [...new Set(materials.value.map((m) => m.category))].sort()
);
const filtered = computed(() =>
  materials.value.filter(
    (m) =>
      (!category.value || m.category === category.value) &&
      (!q.value || m.name.toLowerCase().includes(q.value.toLowerCase()))
  )
);

async function load() {
  loading.value = true;
  error.value = "";
  try {
    materials.value = await simApi.listMaterials();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}

// ── 详情 ────────────────────────────────────────────────────────────────
const expanded = ref<string | null>(null);
const detail = ref<SimMaterialDetail | null>(null);
const shownCard = ref<string | null>(null); // 展开原文的卡 id

async function toggle(m: SimMaterial) {
  if (expanded.value === m.id) {
    expanded.value = null;
    return;
  }
  expanded.value = m.id;
  detail.value = null;
  shownCard.value = null;
  try {
    detail.value = await simApi.getMaterial(m.id);
  } catch (e) {
    error.value = errMsg(e);
  }
}

const PROP_LABELS: Record<string, string> = {
  density: "密度",
  youngs_modulus: "弹性模量",
  poisson_ratio: "泊松比",
  yield_strength: "屈服强度",
};

const VARIANT_LABELS: Record<string, string> = {
  primary: "主卡",
  null: "NULL 伴生",
  alt: "变体",
};

function fmtNum(v: number): string {
  if (v === 0) return "0";
  const a = Math.abs(v);
  if (a >= 1e5 || a < 1e-3) return v.toExponential(3);
  return String(Number(v.toPrecision(6)));
}

// ── 曲线族与 SVG 预览 ───────────────────────────────────────────────────
// 真实值 = SFA*(x+OFFA) / SFO*(y+OFFO)（LS-DYNA *DEFINE_CURVE 语义）；
// points 存的是文件原始数值，缩放只在展示侧套用，原文块才是导出真相。
const CURVE_COLORS = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2", "#be185d", "#65a30d"];

interface Family {
  key: string;
  type: string;
  yUnit: string;
  curves: SimMaterialCurve[];
}

const families = computed<Family[]>(() => {
  if (!detail.value) return [];
  const by = new Map<string, SimMaterialCurve[]>();
  for (const c of detail.value.curves) {
    const k = c.family_key || c.id;
    if (!by.has(k)) by.set(k, []);
    by.get(k)!.push(c);
  }
  return [...by.entries()].map(([key, curves]) => ({
    key,
    type: curves[0].curve_type,
    yUnit: curves[0].y_unit,
    curves,
  }));
});

function realPoints(c: SimMaterialCurve): [number, number][] {
  const s = c.scale ?? { sfa: 1, sfo: 1, offa: 0, offo: 0 };
  return c.points.map(([x, y]) => [s.sfa * (x + s.offa), s.sfo * (y + s.offo)]);
}

const VB_W = 320;
const VB_H = 170;
const PAD = 10;

/** 一族曲线共用坐标范围，画在同一张图里才能对比应变率的影响 */
function familyPlot(f: Family) {
  const all = f.curves.map(realPoints);
  const xs = all.flat().map((p) => p[0]);
  const ys = all.flat().map((p) => p[1]);
  if (!xs.length) return { lines: [], xMin: 0, xMax: 0, yMin: 0, yMax: 0 };
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const yMin = Math.min(...ys), yMax = Math.max(...ys);
  const sx = (x: number) =>
    PAD + ((x - xMin) / (xMax - xMin || 1)) * (VB_W - 2 * PAD);
  const sy = (y: number) =>
    VB_H - PAD - ((y - yMin) / (yMax - yMin || 1)) * (VB_H - 2 * PAD);
  return {
    xMin, xMax, yMin, yMax,
    lines: all.map((pts) => pts.map(([x, y]) => `${sx(x).toFixed(1)},${sy(y).toFixed(1)}`).join(" ")),
  };
}

function curveLegend(c: SimMaterialCurve): string {
  if (c.condition?.strain_rate != null)
    return `${fmtNum(c.condition.strain_rate)} ${c.condition.unit ?? "1/s"}`;
  return c.title || `LCID ${c.source_lcid ?? "?"}`;
}

const CURVE_TYPE_LABELS: Record<string, string> = {
  stress_strain: "应力-应变",
  strain_rate_scale: "应变率缩放",
  force_deflection: "力-位移",
  generic: "曲线",
};

// ── 导入（仅管理员） ────────────────────────────────────────────────────
const showImport = ref(false);
const importFile = ref<File | null>(null);
const unitSystem = ref("t-mm-s");
const importing = ref(false);
const report = ref<SimMaterialImportReport | null>(null);

function pickFile(e: Event) {
  importFile.value = (e.target as HTMLInputElement).files?.[0] ?? null;
}

async function doImport() {
  if (!importFile.value) return;
  importing.value = true;
  error.value = "";
  try {
    report.value = await simApi.importMaterials(importFile.value, unitSystem.value);
    importFile.value = null;
    await load();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    importing.value = false;
  }
}

// ── 元数据编辑 / 删除（仅管理员） ───────────────────────────────────────
const editing = ref<SimMaterial | null>(null);
const editForm = ref({ name: "", category: "", standard_code: "", description: "", source: "", status: "active" });
const savingMeta = ref(false);

function startEdit(m: SimMaterial) {
  editing.value = m;
  editForm.value = {
    name: m.name,
    category: m.category,
    standard_code: m.standard_code ?? "",
    description: m.description ?? "",
    source: m.source ?? "",
    status: m.status,
  };
}

async function saveMeta() {
  if (!editing.value || !editForm.value.name.trim()) return;
  savingMeta.value = true;
  error.value = "";
  try {
    await simApi.updateMaterial(editing.value.id, {
      ...editForm.value,
      name: editForm.value.name.trim(),
      status: editForm.value.status as SimMaterial["status"],
    });
    editing.value = null;
    await load();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    savingMeta.value = false;
  }
}

async function remove(m: SimMaterial) {
  if (!confirm(`删除材料「${m.name}」？其性能、曲线与求解器卡将一并删除。`)) return;
  try {
    await simApi.deleteMaterial(m.id);
    if (expanded.value === m.id) expanded.value = null;
    await load();
  } catch (e) {
    error.value = errMsg(e);
  }
}

onMounted(load);
</script>

<template>
  <div class="w-full">
    <div class="flex items-start mb-4">
      <div>
        <h1 class="text-lg font-semibold text-slate-800 mb-1">材料库</h1>
        <p class="text-sm text-slate-500">
          全局材料资产：物理材料带性能参数、按应变率分族的曲线、以及经过工程验证的求解器卡原文（导出直接用原文，不重新生成）。
          <span v-if="!isAdmin">（只读，材料数据由管理员维护）</span>
        </p>
      </div>
      <button
        v-if="isAdmin"
        class="ml-auto shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-blue-600 text-white text-sm hover:bg-blue-700"
        @click="showImport = true; report = null"
      >
        <Upload :size="14" /> 导入关键字文件
      </button>
    </div>

    <div v-if="error" class="mb-3 px-3 py-2 rounded-md bg-rose-50 text-rose-700 text-sm">
      {{ error }}
    </div>

    <!-- 筛选 -->
    <div class="flex items-center gap-2 mb-3">
      <div class="relative">
        <Search :size="14" class="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" />
        <input
          v-model="q"
          placeholder="按牌号搜索…"
          class="pl-8 pr-3 py-1.5 w-56 rounded-md border border-slate-200 text-sm focus:outline-none focus:ring-2 focus:ring-blue-100"
        />
      </div>
      <select
        v-model="category"
        class="px-2 py-1.5 rounded-md border border-slate-200 text-sm text-slate-600"
      >
        <option value="">全部分类</option>
        <option v-for="c in categories" :key="c" :value="c">{{ catLabel(c) }}</option>
      </select>
      <span class="ml-auto text-xs text-slate-400">{{ filtered.length }} / {{ materials.length }} 种材料</span>
    </div>

    <div v-if="loading" class="flex items-center gap-2 text-slate-500 text-sm py-10 justify-center">
      <Loader2 :size="18" class="animate-spin" /> 加载中…
    </div>

    <div
      v-else-if="!filtered.length"
      class="text-center py-16 text-slate-400 border border-dashed border-slate-200 rounded-lg"
    >
      <Layers :size="32" class="mx-auto mb-2 opacity-40" />
      <p class="text-sm">{{ materials.length ? "没有匹配的材料" : "材料库为空" }}</p>
      <p v-if="!materials.length && isAdmin" class="text-xs mt-1">导入 LS-DYNA 关键字材料库（.k/.key）即可建库</p>
    </div>

    <div v-else class="space-y-2">
      <div
        v-for="m in filtered"
        :key="m.id"
        class="border border-slate-200 rounded-lg bg-white overflow-hidden"
      >
        <!-- 行 -->
        <div
          class="flex items-center gap-2 px-4 py-3 cursor-pointer hover:bg-slate-50"
          @click="toggle(m)"
        >
          <Layers :size="16" class="text-blue-600 shrink-0" />
          <span class="font-medium text-slate-800">{{ m.name }}</span>
          <span class="px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 text-xs">{{ catLabel(m.category) }}</span>
          <span v-if="m.standard_code" class="text-xs text-slate-400">{{ m.standard_code }}</span>
          <span
            v-if="m.status === 'deprecated'"
            class="px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 text-xs"
          >已停用</span>
          <span class="ml-auto text-xs text-slate-400">
            {{ m.card_count ?? 0 }} 卡 · {{ m.curve_count ?? 0 }} 曲线 · rev{{ m.revision }}
          </span>
          <template v-if="isAdmin">
            <button
              class="p-1 rounded hover:bg-slate-100 text-slate-400 hover:text-slate-600"
              title="编辑分类与说明"
              @click.stop="startEdit(m)"
            ><Pencil :size="14" /></button>
            <button
              class="p-1 rounded hover:bg-rose-50 text-slate-400 hover:text-rose-600"
              @click.stop="remove(m)"
            ><Trash2 :size="14" /></button>
          </template>
        </div>

        <!-- 详情 -->
        <div v-if="expanded === m.id" class="border-t border-slate-100 px-4 py-3 bg-slate-50/50">
          <div v-if="!detail" class="text-xs text-slate-400">加载中…</div>
          <div v-else class="space-y-3">
            <p v-if="detail.description" class="text-xs text-slate-500">{{ detail.description }}</p>
            <div class="flex flex-wrap gap-3 text-xs text-slate-600">
              <span v-if="detail.source">来源：{{ detail.source }}</span>
              <span>修订 {{ detail.revision }}</span>
              <span v-if="detail.created_by">导入 by {{ detail.created_by }}</span>
            </div>

            <!-- 性能参数 -->
            <section v-if="detail.properties.length" class="border border-slate-200 rounded-md bg-white">
              <header class="px-3 py-2 border-b border-slate-100 bg-slate-50">
                <span class="text-xs font-semibold text-slate-700">性能参数</span>
                <span class="text-xs text-slate-400 ml-2">展示与检索用；导出以求解器卡原文为准</span>
              </header>
              <div class="px-3 py-2 flex flex-wrap gap-x-6 gap-y-1">
                <span v-for="p in detail.properties" :key="p.id" class="text-xs text-slate-600">
                  {{ PROP_LABELS[p.name] ?? p.name }}：
                  <span class="font-mono text-slate-800">{{ fmtNum(p.value) }}</span>
                  <span class="text-slate-400"> {{ p.unit }}</span>
                </span>
              </div>
            </section>

            <!-- 曲线族 -->
            <section
              v-for="f in families"
              :key="f.key"
              class="border border-slate-200 rounded-md bg-white"
            >
              <header class="px-3 py-2 border-b border-slate-100 bg-slate-50 flex items-center gap-2">
                <span class="text-xs font-semibold text-slate-700">
                  {{ CURVE_TYPE_LABELS[f.type] ?? f.type }}曲线
                </span>
                <span v-if="f.curves.length > 1" class="text-xs text-slate-400">
                  曲线族 · {{ f.curves.length }} 条（按应变率）
                </span>
                <span class="ml-auto text-xs text-slate-400 font-mono">{{ f.key }}</span>
              </header>
              <div class="px-3 py-2 flex flex-wrap items-start gap-4">
                <svg
                  :viewBox="`0 0 ${VB_W} ${VB_H}`"
                  class="w-80 max-w-full border border-slate-100 rounded bg-white"
                >
                  <template v-for="(line, i) in familyPlot(f).lines" :key="i">
                    <polyline
                      :points="line"
                      fill="none"
                      :stroke="CURVE_COLORS[i % CURVE_COLORS.length]"
                      stroke-width="1.5"
                    />
                  </template>
                  <text :x="PAD" :y="VB_H - 2" class="fill-slate-400" font-size="8">
                    {{ fmtNum(familyPlot(f).xMin) }}
                  </text>
                  <text :x="VB_W - PAD" :y="VB_H - 2" text-anchor="end" class="fill-slate-400" font-size="8">
                    {{ fmtNum(familyPlot(f).xMax) }}
                  </text>
                  <text :x="PAD" :y="8" class="fill-slate-400" font-size="8">
                    {{ fmtNum(familyPlot(f).yMax) }} {{ f.yUnit }}
                  </text>
                </svg>
                <ul class="text-xs space-y-1 pt-1">
                  <li v-for="(c, i) in f.curves" :key="c.id" class="flex items-center gap-1.5">
                    <span
                      class="inline-block w-3 h-0.5 rounded"
                      :style="{ background: CURVE_COLORS[i % CURVE_COLORS.length] }"
                    />
                    <span class="text-slate-600">{{ curveLegend(c) }}</span>
                    <span class="text-slate-400">（{{ c.points.length }} 点）</span>
                  </li>
                </ul>
              </div>
            </section>

            <!-- 求解器卡 -->
            <section class="border border-slate-200 rounded-md bg-white">
              <header class="px-3 py-2 border-b border-slate-100 bg-slate-50">
                <span class="text-xs font-semibold text-slate-700">求解器卡（{{ detail.cards.length }}）</span>
                <span class="text-xs text-slate-400 ml-2">自包含关键字原文，可直接进 deck</span>
              </header>
              <div class="divide-y divide-slate-100">
                <div v-for="c in detail.cards" :key="c.id" class="px-3 py-2">
                  <div class="flex items-center gap-2 text-xs">
                    <span
                      class="px-1.5 py-0.5 rounded"
                      :class="c.variant === 'primary' ? 'bg-blue-50 text-blue-700'
                        : c.variant === 'null' ? 'bg-slate-100 text-slate-500'
                        : 'bg-violet-50 text-violet-600'"
                    >{{ VARIANT_LABELS[c.variant] ?? c.variant }}</span>
                    <span class="font-mono text-slate-700">*MAT_{{ c.mat_type }}</span>
                    <span class="text-slate-500">{{ c.title }}</span>
                    <span class="text-slate-400">MID {{ c.source_mid ?? "—" }} · {{ c.unit_system }} · {{ c.solver_type }}</span>
                    <button
                      class="ml-auto inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-slate-400 hover:bg-slate-100 hover:text-slate-600"
                      @click="shownCard = shownCard === c.id ? null : c.id"
                    >
                      <FileText :size="12" /> {{ shownCard === c.id ? "收起原文" : "查看原文" }}
                    </button>
                  </div>
                  <pre
                    v-if="shownCard === c.id"
                    class="mt-2 p-2 rounded bg-slate-900 text-slate-100 text-[11px] leading-4 overflow-x-auto max-h-80 overflow-y-auto"
                  >{{ c.keyword_text }}</pre>
                </div>
              </div>
            </section>
          </div>
        </div>
      </div>
    </div>

    <!-- 导入弹窗 -->
    <div v-if="showImport" class="fixed inset-0 z-50 flex items-center justify-center bg-black/30" @click.self="showImport = false">
      <div class="w-[28rem] max-w-[92vw] rounded-lg bg-white shadow-xl">
        <div class="flex items-center px-4 py-3 border-b border-slate-100">
          <span class="font-medium text-slate-800 text-sm">导入 LS-DYNA 关键字材料库</span>
          <button class="ml-auto p-1 rounded hover:bg-slate-100 text-slate-400" @click="showImport = false">
            <X :size="16" />
          </button>
        </div>
        <div class="px-4 py-3 space-y-3 text-sm">
          <p class="text-xs text-slate-500">
            解析 *MAT_* / *DEFINE_CURVE / *DEFINE_TABLE，按牌号归一为物理材料（NULL 伴生卡与重名变体自动归组）。
            导入是幂等的：内容未变的材料跳过，变了的整体替换并生成新修订。
          </p>
          <div>
            <label class="block text-xs text-slate-500 mb-1">关键字文件（.k / .key）</label>
            <input type="file" accept=".k,.key,.dyn,.txt" class="text-xs" @change="pickFile" />
          </div>
          <div>
            <label class="block text-xs text-slate-500 mb-1">单位制（按文件声明存储，不强转）</label>
            <select v-model="unitSystem" class="px-2 py-1.5 rounded-md border border-slate-200 text-sm">
              <option value="t-mm-s">t-mm-s（碰撞常用：密度 t/mm³，模量 MPa）</option>
              <option value="SI">SI（kg-m-s）</option>
            </select>
          </div>

          <div v-if="report" class="rounded-md bg-slate-50 border border-slate-200 px-3 py-2 text-xs space-y-1">
            <div class="text-slate-700">
              新建 {{ report.materials_created }} · 更新 {{ report.materials_updated }} ·
              未变 {{ report.materials_unchanged }} · 卡 {{ report.cards }} · 曲线 {{ report.curves }}
            </div>
            <div v-for="(w, i) in report.warnings" :key="i" class="text-amber-700">⚠ {{ w }}</div>
          </div>
        </div>
        <div class="flex justify-end gap-2 px-4 py-3 border-t border-slate-100">
          <button class="px-3 py-1.5 rounded-md text-sm text-slate-600 hover:bg-slate-100" @click="showImport = false">
            {{ report ? "完成" : "取消" }}
          </button>
          <button
            class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50"
            :disabled="!importFile || importing"
            @click="doImport"
          >
            <Loader2 v-if="importing" :size="14" class="animate-spin" />
            <Upload v-else :size="14" />
            导入
          </button>
        </div>
      </div>
    </div>

    <!-- 元数据编辑弹窗 -->
    <div v-if="editing" class="fixed inset-0 z-50 flex items-center justify-center bg-black/30" @click.self="editing = null">
      <div class="w-[26rem] max-w-[92vw] rounded-lg bg-white shadow-xl">
        <div class="flex items-center px-4 py-3 border-b border-slate-100">
          <span class="font-medium text-slate-800 text-sm">编辑材料元数据</span>
          <button class="ml-auto p-1 rounded hover:bg-slate-100 text-slate-400" @click="editing = null">
            <X :size="16" />
          </button>
        </div>
        <div class="px-4 py-3 space-y-2.5 text-sm">
          <p class="text-xs text-slate-400">性能、曲线与卡不在此编辑——那些只能整体重新导入生成新修订。</p>
          <div>
            <label class="block text-xs text-slate-500 mb-1">牌号</label>
            <input v-model="editForm.name" class="w-full px-2 py-1.5 rounded-md border border-slate-200" />
          </div>
          <div class="grid grid-cols-2 gap-2">
            <div>
              <label class="block text-xs text-slate-500 mb-1">分类</label>
              <select v-model="editForm.category" class="w-full px-2 py-1.5 rounded-md border border-slate-200">
                <option v-for="(label, c) in CATEGORY_LABELS" :key="c" :value="c">{{ label }}</option>
              </select>
            </div>
            <div>
              <label class="block text-xs text-slate-500 mb-1">状态</label>
              <select v-model="editForm.status" class="w-full px-2 py-1.5 rounded-md border border-slate-200">
                <option value="active">在用</option>
                <option value="deprecated">停用</option>
              </select>
            </div>
          </div>
          <div>
            <label class="block text-xs text-slate-500 mb-1">标准号</label>
            <input v-model="editForm.standard_code" class="w-full px-2 py-1.5 rounded-md border border-slate-200" placeholder="如 GB/T 700" />
          </div>
          <div>
            <label class="block text-xs text-slate-500 mb-1">来源</label>
            <input v-model="editForm.source" class="w-full px-2 py-1.5 rounded-md border border-slate-200" placeholder="手册 / 试验报告 / 供应商数据" />
          </div>
          <div>
            <label class="block text-xs text-slate-500 mb-1">说明</label>
            <textarea v-model="editForm.description" rows="2" class="w-full px-2 py-1.5 rounded-md border border-slate-200" />
          </div>
        </div>
        <div class="flex justify-end gap-2 px-4 py-3 border-t border-slate-100">
          <button class="px-3 py-1.5 rounded-md text-sm text-slate-600 hover:bg-slate-100" @click="editing = null">取消</button>
          <button
            class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50"
            :disabled="!editForm.name.trim() || savingMeta"
            @click="saveMeta"
          >
            <Loader2 v-if="savingMeta" :size="14" class="animate-spin" /> 保存
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
