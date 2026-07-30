<script setup lang="ts">
/**
 * 质量卡模板库管理页。
 *
 * 模板分两层：内置模板随代码走、只读（改了它等于悄悄改掉所有历史项目的验收
 * 标准）；用户模板来自客户规范导入，可改元数据、可删。项目里用的质量卡都是
 * 从这里的模板派生的实例——实例文件派生时已拷进项目目录，因此删模板不影响
 * 既有实例，used_by 只是提示影响面。
 *
 * 内容（阈值/网格参数）也能改，但没有"直接改数字"这回事：用户模板的每项改动
 * 必须带依据并追加进 overrides 留痕；内置模板只读，想改就以它为底座派生一张
 * 用户模板。两条路走的都是同一套覆盖机制，管理页不是绕过留痕的口子。
 */
import { onMounted, ref } from "vue";
import { simApi, errMsg } from "@/api";
import type { SimQualityCardDetail, SimQualityTemplate } from "@/api/types";
import {
  ClipboardCheck,
  Loader2,
  Lock,
  Pencil,
  SlidersHorizontal,
  Trash2,
  Upload,
  X,
} from "lucide-vue-next";

const templates = ref<SimQualityTemplate[]>([]);
const loading = ref(true);
const error = ref("");

const expanded = ref<string | null>(null);
const detail = ref<SimQualityCardDetail | null>(null);

async function load() {
  loading.value = true;
  error.value = "";
  try {
    templates.value = await simApi.listQualityTemplates();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}

async function toggle(t: SimQualityTemplate) {
  if (expanded.value === t.id) {
    expanded.value = null;
    return;
  }
  expanded.value = t.id;
  detail.value = null;
  try {
    detail.value = await simApi.getQualityTemplate(t.id);
  } catch (e) {
    error.value = errMsg(e);
  }
}

async function remove(t: SimQualityTemplate) {
  const hint = t.used_by
    ? `已有 ${t.used_by} 个项目实例基于它派生（实例不受影响，但之后无法再选它派生）。`
    : "";
  if (!confirm(`删除模板「${t.name}」？${hint}`)) return;
  try {
    await simApi.deleteQualityTemplate(t.id);
    if (expanded.value === t.id) expanded.value = null;
    await load();
  } catch (e) {
    error.value = errMsg(e);
  }
}

// ── 元数据编辑（只动 template.json，不碰阈值） ───────────────────────────
const editing = ref<SimQualityTemplate | null>(null);
const editForm = ref({ name: "", source: "", revision: "", scope: "", description: "" });
const savingMeta = ref(false);

function startEdit(t: SimQualityTemplate) {
  editing.value = t;
  editForm.value = {
    name: t.name,
    source: t.source,
    revision: t.revision,
    scope: t.scope,
    description: t.description,
  };
}

async function saveMeta() {
  if (!editing.value || !editForm.value.name.trim()) return;
  savingMeta.value = true;
  error.value = "";
  try {
    await simApi.updateQualityTemplate(editing.value.id, {
      ...editForm.value,
      name: editForm.value.name.trim(),
    });
    editing.value = null;
    await load();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    savingMeta.value = false;
  }
}

// ── 内容编辑（阈值/网格参数，逐项留痕） ─────────────────────────────────
// 用户模板：改动直接落卡文件并追加进 overrides；内置模板：以它为底座派生
// 一张用户模板，改动记在新模板的 overrides 里。同一个弹窗，两条保存路径。
const BANDS = ["best", "good", "failed", "worst"] as const;
const MESH_FIELDS = [
  { key: "target_element_length", label: "目标单元 (mm)", detail: "targetElementLength" },
  { key: "general_min_target_len", label: "下限 (mm)", detail: "minTargetLength" },
  { key: "general_max_target_len", label: "上限 (mm)", detail: "maxTargetLength" },
] as const;

const editingContent = ref<SimQualityTemplate | null>(null);
const contentDetail = ref<SimQualityCardDetail | null>(null);
const meshForm = ref<Record<string, string>>({});
const thrForm = ref<Record<string, Record<string, string>>>({});
const contentReason = ref("");
const deriveForm = ref({ new_id: "", name: "" });
const savingContent = ref(false);

function critKey(cr: { name: string; domain: string }) {
  return `${cr.name} [${cr.domain}]`;
}

async function startEditContent(t: SimQualityTemplate) {
  editingContent.value = t;
  contentDetail.value = null;
  contentReason.value = "";
  deriveForm.value = { new_id: "", name: t.builtin ? `${t.name}（客户版）` : "" };
  try {
    const d = await simApi.getQualityTemplate(t.id);
    contentDetail.value = d;
    meshForm.value = Object.fromEntries(
      MESH_FIELDS.map((f) => [f.key, String(d.meshParams[f.detail] ?? "")])
    );
    thrForm.value = Object.fromEntries(
      d.criteria.map((cr) => [
        critKey(cr),
        Object.fromEntries(BANDS.map((b) => [b, cr.thresholds[b] == null ? "" : String(cr.thresholds[b])])),
      ])
    );
  } catch (e) {
    error.value = errMsg(e);
    editingContent.value = null;
  }
}

/** 与打开时的值做 diff，只把真正改了的项做成覆盖。清空一个已有值不算改——
 * 覆盖机制只能改数、不能撤销检查。 */
function buildContentOverrides() {
  const d = contentDetail.value;
  if (!d) return [];
  const src = contentReason.value.trim();
  const out: { target: string; new_value: string; source: string }[] = [];
  for (const f of MESH_FIELDS) {
    const cur = (meshForm.value[f.key] ?? "").trim();
    if (cur !== "" && Number(cur) !== d.meshParams[f.detail])
      out.push({ target: `mesh:${f.key}`, new_value: cur, source: src });
  }
  for (const cr of d.criteria) {
    const row = thrForm.value[critKey(cr)];
    if (!row) continue;
    for (const b of BANDS) {
      const cur = (row[b] ?? "").trim();
      const orig = cr.thresholds[b];
      if (cur !== "" && (orig == null || Number(cur) !== orig))
        out.push({ target: `criteria:${critKey(cr)}:${b}`, new_value: cur, source: src });
    }
  }
  return out;
}

const contentDirty = () => buildContentOverrides().length > 0;

async function saveContent() {
  const t = editingContent.value;
  if (!t || !contentReason.value.trim()) return;
  const overrides = buildContentOverrides();
  if (!overrides.length) {
    editingContent.value = null;
    return;
  }
  savingContent.value = true;
  error.value = "";
  try {
    if (t.builtin) {
      await simApi.deriveQualityTemplate(t.id, {
        new_id: deriveForm.value.new_id.trim(),
        name: deriveForm.value.name.trim(),
        overrides,
        source: contentReason.value.trim(),
        scope: t.scope,
      });
    } else {
      await simApi.editQualityTemplateContent(t.id, overrides);
      if (expanded.value === t.id) detail.value = await simApi.getQualityTemplate(t.id);
    }
    editingContent.value = null;
    await load();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    savingContent.value = false;
  }
}

// ── 导入客户质量卡 ───────────────────────────────────────────────────────
const showImport = ref(false);
const importing = ref(false);
const importForm = ref({
  template_id: "",
  name: "",
  source: "",
  revision: "",
  scope: "",
  description: "",
});
const qualFile = ref<File | null>(null);
const mparFile = ref<File | null>(null);

async function doImport() {
  if (!qualFile.value || !importForm.value.template_id.trim() || !importForm.value.name.trim())
    return;
  importing.value = true;
  error.value = "";
  try {
    await simApi.importQualityTemplate({
      template_id: importForm.value.template_id.trim(),
      name: importForm.value.name.trim(),
      qualFile: qualFile.value,
      mparFile: mparFile.value,
      source: importForm.value.source,
      revision: importForm.value.revision,
      scope: importForm.value.scope,
      description: importForm.value.description,
    });
    showImport.value = false;
    importForm.value = { template_id: "", name: "", source: "", revision: "", scope: "", description: "" };
    qualFile.value = null;
    mparFile.value = null;
    await load();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    importing.value = false;
  }
}

function fmtDt(seconds: number) {
  return seconds > 0 ? `${(seconds * 1e6).toFixed(2)} μs` : "—";
}

// ── 完整内容展示 ─────────────────────────────────────────────────────────
// 一张卡是两个部分的配对：判定侧（.ansa_qual，验收标准）+ 生成侧（.ansa_mpar，
// 工艺配方）。详情按这两部分分区展示，各自铺全（含停用判据与全部生成参数），
// 否则用户会以为整张卡就一屏——而其余内容其实都会原样交给 ANSA。
function disabledCriteria(d: SimQualityCardDetail) {
  return d.allCriteria.filter((c) => !c.enabled);
}

function meshParamCount(d: SimQualityCardDetail) {
  return d.meshGroups.reduce((n, g) => n + g.params.length, 0);
}

/** 留痕按侧拆分：criteria: 是验收标准变更，mesh: 是工艺调优——语义不同，分开看 */
function sideOverrides(t: SimQualityTemplate, side: "criteria" | "mesh") {
  return (t.overrides ?? []).filter((o) => o.target.startsWith(`${side}:`));
}

onMounted(load);
</script>

<template>
  <div class="w-full">
    <div class="flex items-start mb-4">
      <div>
        <h1 class="text-lg font-semibold text-slate-800 mb-1">质量卡模板</h1>
        <p class="text-sm text-slate-500">
          内置模板是所有项目的缺省底座（只读，可派生副本后修改）；客户规范导入后成为用户模板，阈值与网格参数可在此修改，每项改动须注明依据并逐项留痕。项目里的质量卡从模板派生。
        </p>
      </div>
      <button
        class="ml-auto shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-blue-600 text-white text-sm hover:bg-blue-700"
        @click="showImport = true"
      >
        <Upload :size="14" /> 导入质量卡
      </button>
    </div>

    <div v-if="error" class="mb-3 px-3 py-2 rounded-md bg-rose-50 text-rose-700 text-sm">
      {{ error }}
    </div>

    <div v-if="loading" class="flex items-center gap-2 text-slate-500 text-sm py-10 justify-center">
      <Loader2 :size="18" class="animate-spin" /> 加载中…
    </div>

    <div
      v-else-if="!templates.length"
      class="text-center py-16 text-slate-400 border border-dashed border-slate-200 rounded-lg"
    >
      <ClipboardCheck :size="32" class="mx-auto mb-2 opacity-40" />
      <p class="text-sm">模板库为空</p>
    </div>

    <div v-else class="space-y-2">
      <div
        v-for="t in templates"
        :key="t.id"
        class="border border-slate-200 rounded-lg bg-white overflow-hidden"
      >
        <div
          class="flex items-center gap-2 px-4 py-3 cursor-pointer hover:bg-slate-50"
          @click="toggle(t)"
        >
          <ClipboardCheck :size="16" class="text-blue-600 shrink-0" />
          <span class="font-medium text-slate-800">{{ t.name }}</span>
          <span class="px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 text-xs font-mono">{{ t.id }}</span>
          <Lock v-if="t.builtin" :size="13" class="text-slate-400" title="内置模板，只读" />
          <span
            v-if="t.based_on"
            class="px-1.5 py-0.5 rounded bg-violet-50 text-violet-600 text-xs"
            :title="`派生自 ${t.based_on}`"
          >派生</span>
          <span v-if="t.scope" class="text-xs text-slate-400">{{ t.scope }}</span>
          <span class="ml-auto text-xs text-slate-400">
            {{ t.used_by ? `${t.used_by} 个项目实例在用` : "未被引用" }}
          </span>
          <span v-if="t.created_by" class="text-xs text-slate-400">by {{ t.created_by }}</span>
          <button
            class="p-1 rounded hover:bg-slate-100 text-slate-400 hover:text-slate-600"
            :title="t.builtin ? '内置模板只读——以它为底座派生一张用户模板并修改' : '修改阈值与网格参数（逐项留痕）'"
            @click.stop="startEditContent(t)"
          ><SlidersHorizontal :size="14" /></button>
          <template v-if="!t.builtin">
            <button
              class="p-1 rounded hover:bg-slate-100 text-slate-400 hover:text-slate-600"
              title="编辑名称与说明"
              @click.stop="startEdit(t)"
            ><Pencil :size="14" /></button>
            <button
              class="p-1 rounded hover:bg-rose-50 text-slate-400 hover:text-rose-600"
              @click.stop="remove(t)"
            ><Trash2 :size="14" /></button>
          </template>
        </div>

        <div v-if="expanded === t.id" class="border-t border-slate-100 px-4 py-3 bg-slate-50/50">
          <div v-if="!detail" class="text-xs text-slate-400">加载中…</div>
          <div v-else class="space-y-3">
            <p v-if="detail.description" class="text-xs text-slate-500">{{ detail.description }}</p>
            <div class="flex flex-wrap gap-3 text-xs text-slate-600">
              <span v-if="detail.source">来源：{{ detail.source }}</span>
              <span v-if="detail.revision">版本：{{ detail.revision }}</span>
              <span>ANSA {{ detail.ansaVersion }}</span>
            </div>

            <!-- 判定侧：验收标准 -->
            <section class="border border-slate-200 rounded-md bg-white">
              <header class="flex flex-wrap items-center gap-2 px-3 py-2 border-b border-slate-100 bg-slate-50">
                <span class="text-xs font-semibold text-slate-700">判定侧 · 验收标准</span>
                <span class="text-xs text-slate-400">划完的网格算不算好（.ansa_qual，ANSA 质量检查环节消费）</span>
                <span
                  class="ml-auto px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 text-xs"
                  title="判废线上的最小单元长度对应的显式时间步。碰撞里这是机时的总闸：单元越小，时间步越短，机时越贵。"
                >
                  判废线时间步 {{ fmtDt(detail.timeStepAtFailedMinLength) }}
                </span>
              </header>
              <div class="px-3 py-2 space-y-2">
                <div v-if="sideOverrides(t, 'criteria').length">
                  <div class="text-xs font-medium text-slate-600 mb-1">
                    相对 {{ t.based_on || "基础模板" }} 的标准变更
                  </div>
                  <table class="w-full text-xs">
                    <tbody>
                      <tr v-for="(o, i) in sideOverrides(t, 'criteria')" :key="i" class="border-b border-slate-100">
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
                    启用的判据（{{ detail.criteria.length }} 条）
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
                      <tr v-for="cr in detail.criteria" :key="`${cr.name}|${cr.domain}`" class="border-b border-slate-100">
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

                <details>
                  <summary class="text-xs font-medium text-slate-500 cursor-pointer select-none hover:text-slate-700">
                    停用的判据（{{ disabledCriteria(detail).length }} 条）——随卡保存，但在此卡中不检查
                  </summary>
                  <table class="w-full text-xs mt-1 opacity-60">
                    <thead class="text-slate-400">
                      <tr>
                        <th class="text-left font-normal py-1">判据</th>
                        <th class="text-left font-normal py-1">域</th>
                        <th class="text-left font-normal py-1">算法</th>
                        <th class="text-left font-normal py-1">Best</th>
                        <th class="text-left font-normal py-1">Good</th>
                        <th class="text-left font-normal py-1">判废</th>
                        <th class="text-left font-normal py-1">Worst</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="cr in disabledCriteria(detail)" :key="`${cr.name}|${cr.domain}`" class="border-b border-slate-100">
                        <td class="py-1 text-slate-600">{{ cr.name }}</td>
                        <td class="py-1 text-slate-400">{{ cr.domain }}</td>
                        <td class="py-1 text-slate-400">{{ cr.calculation || "—" }}</td>
                        <td class="py-1 text-slate-400">{{ cr.thresholds.best ?? "—" }}</td>
                        <td class="py-1 text-slate-400">{{ cr.thresholds.good ?? "—" }}</td>
                        <td class="py-1 text-slate-400">{{ cr.thresholds.failed ?? "—" }}</td>
                        <td class="py-1 text-slate-400">{{ cr.thresholds.worst ?? "—" }}</td>
                      </tr>
                    </tbody>
                  </table>
                </details>
              </div>
            </section>

            <!-- 生成侧：工艺配方 -->
            <section class="border border-slate-200 rounded-md bg-white">
              <header class="flex flex-wrap items-center gap-2 px-3 py-2 border-b border-slate-100 bg-slate-50">
                <span class="text-xs font-semibold text-slate-700">生成侧 · 工艺配方</span>
                <span class="text-xs text-slate-400">网格该怎么划（.ansa_mpar，ANSA 批处理划网格环节消费）</span>
                <span class="ml-auto text-xs text-slate-600">
                  目标单元 <b>{{ detail.meshParams.targetElementLength }} mm</b>，下限
                  {{ detail.meshParams.minTargetLength }} / 上限 {{ detail.meshParams.maxTargetLength }} mm
                </span>
              </header>
              <div class="px-3 py-2 space-y-2">
                <div v-if="sideOverrides(t, 'mesh').length">
                  <div class="text-xs font-medium text-slate-600 mb-1">
                    相对 {{ t.based_on || "基础模板" }} 的工艺调整
                  </div>
                  <table class="w-full text-xs">
                    <tbody>
                      <tr v-for="(o, i) in sideOverrides(t, 'mesh')" :key="i" class="border-b border-slate-100">
                        <td class="py-1 font-mono text-slate-600">{{ o.target }}</td>
                        <td class="py-1 text-slate-400">{{ o.old_value }} →</td>
                        <td class="py-1 text-slate-800 font-medium">{{ o.new_value }}</td>
                        <td class="py-1 text-slate-500">依据：{{ o.source }}</td>
                        <td class="py-1 text-slate-400 w-12">{{ o.by }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>

                <details>
                  <summary class="text-xs font-medium text-slate-500 cursor-pointer select-none hover:text-slate-700">
                    全部参数（{{ detail.meshGroups.length }} 组 / {{ meshParamCount(detail) }} 项）——原样交给 ANSA
                  </summary>
                  <div class="mt-2 space-y-3">
                    <div v-for="g in detail.meshGroups" :key="g.title || '通用'">
                      <div class="text-xs font-medium text-slate-600 mb-1">{{ g.title || "通用" }}</div>
                      <div class="grid grid-cols-1 md:grid-cols-2 gap-x-6">
                        <div
                          v-for="p in g.params"
                          :key="p.key"
                          class="flex items-baseline gap-2 py-0.5 text-xs border-b border-slate-100"
                        >
                          <span class="font-mono text-slate-600 break-all">{{ p.key }}</span>
                          <span class="ml-auto font-mono text-slate-400 text-right break-all">{{ p.value || "—" }}</span>
                        </div>
                      </div>
                    </div>
                  </div>
                </details>
              </div>
            </section>
          </div>
        </div>
      </div>
    </div>

    <!-- 编辑内容（阈值/网格参数） -->
    <div
      v-if="editingContent"
      class="fixed inset-0 z-40 bg-black/30 flex items-center justify-center p-4"
      @click.self="editingContent = null"
    >
      <div class="bg-white rounded-lg shadow-xl w-full max-w-3xl max-h-[85vh] flex flex-col">
        <div class="flex items-center px-5 pt-4 pb-2">
          <h2 class="font-semibold text-slate-800">
            {{ editingContent.builtin ? "派生用户模板并修改" : "修改模板内容" }}
          </h2>
          <span class="ml-2 px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 text-xs font-mono">{{ editingContent.id }}</span>
          <button class="ml-auto p-1 rounded hover:bg-slate-100 text-slate-500" @click="editingContent = null">
            <X :size="18" />
          </button>
        </div>
        <p class="px-5 text-xs text-slate-400">
          {{
            editingContent.builtin
              ? "内置模板只读——改了它等于悄悄改掉所有历史项目的验收标准。保存时将以它为底座派生一张新的用户模板，改动逐项留痕。"
              : "改动将直接写入模板卡文件并逐项留痕，只影响之后从它派生的实例；既有项目实例不受影响。"
          }}
        </p>

        <div v-if="!contentDetail" class="px-5 py-8 text-sm text-slate-400">加载中…</div>
        <div v-else class="px-5 py-3 space-y-4 overflow-y-auto">
          <div v-if="editingContent.builtin" class="grid grid-cols-2 gap-3">
            <label class="text-sm text-slate-600">
              新模板 id <span class="text-rose-500">*</span>
              <input
                v-model="deriveForm.new_id"
                class="mt-1 w-full border border-slate-200 rounded px-2 py-1.5 text-sm font-mono"
                placeholder="cust-crash-5mm"
              />
            </label>
            <label class="text-sm text-slate-600">
              新模板名称 <span class="text-rose-500">*</span>
              <input v-model="deriveForm.name" class="mt-1 w-full border border-slate-200 rounded px-2 py-1.5 text-sm" />
            </label>
          </div>

          <div>
            <div class="text-xs font-medium text-slate-600 mb-1">
              判定侧 · 验收标准阈值（{{ contentDetail.criteria.length }} 条启用判据）
            </div>
            <table class="w-full text-xs">
              <thead class="text-slate-400">
                <tr>
                  <th class="text-left font-normal py-1">判据</th>
                  <th class="text-left font-normal py-1">算法</th>
                  <th class="text-left font-normal py-1">Best</th>
                  <th class="text-left font-normal py-1">Good</th>
                  <th class="text-left font-normal py-1">判废</th>
                  <th class="text-left font-normal py-1">Worst</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="cr in contentDetail.criteria" :key="critKey(cr)" class="border-b border-slate-100">
                  <td class="py-1 text-slate-700">{{ cr.name }}</td>
                  <td class="py-1 text-slate-500">{{ cr.calculation || "—" }}</td>
                  <td v-for="b in BANDS" :key="b" class="py-1 pr-2">
                    <input
                      v-model="thrForm[critKey(cr)][b]"
                      type="number"
                      step="any"
                      class="w-20 border border-slate-200 rounded px-1.5 py-1 text-xs"
                      :class="b === 'failed' ? 'text-rose-600 font-medium' : 'text-slate-600'"
                    />
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <div>
            <div class="text-xs font-medium text-slate-600 mb-1">生成侧 · 工艺配方（目标单元长度）</div>
            <div class="grid grid-cols-3 gap-3">
              <label v-for="f in MESH_FIELDS" :key="f.key" class="text-sm text-slate-600">
                {{ f.label }}
                <input
                  v-model="meshForm[f.key]"
                  type="number"
                  step="any"
                  class="mt-1 w-full border border-slate-200 rounded px-2 py-1.5 text-sm"
                />
              </label>
            </div>
          </div>

          <label class="block text-sm text-slate-600">
            依据 <span class="text-rose-500">*</span>
            <input
              v-model="contentReason"
              class="mt-1 w-full border border-slate-200 rounded px-2 py-1.5 text-sm"
              placeholder="如：客户技术协议 3.2 节 / 项目评审结论"
            />
            <span class="text-xs text-slate-400">每项改动都会记下这条出处——无出处的阈值会一路流进网格验收。</span>
          </label>
        </div>

        <div class="flex justify-end items-center gap-2 px-5 py-3 border-t border-slate-100">
          <span v-if="contentDetail && !contentDirty()" class="mr-auto text-xs text-slate-400">尚无改动</span>
          <button class="px-3 py-1.5 rounded-md text-sm text-slate-600 hover:bg-slate-100" @click="editingContent = null">取消</button>
          <button
            class="px-3 py-1.5 rounded-md bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50"
            :disabled="
              savingContent ||
              !contentDetail ||
              !contentDirty() ||
              !contentReason.trim() ||
              (editingContent.builtin && (!deriveForm.new_id.trim() || !deriveForm.name.trim()))
            "
            @click="saveContent"
          >
            {{ savingContent ? "保存中…" : editingContent.builtin ? "派生并保存" : "保存改动" }}
          </button>
        </div>
      </div>
    </div>

    <!-- 编辑元数据 -->
    <div
      v-if="editing"
      class="fixed inset-0 z-40 bg-black/30 flex items-center justify-center p-4"
      @click.self="editing = null"
    >
      <div class="bg-white rounded-lg shadow-xl w-full max-w-lg p-5 space-y-3">
        <div class="flex items-center">
          <h2 class="font-semibold text-slate-800">编辑模板信息</h2>
          <span class="ml-2 px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 text-xs font-mono">{{ editing.id }}</span>
          <button class="ml-auto p-1 rounded hover:bg-slate-100 text-slate-500" @click="editing = null">
            <X :size="18" />
          </button>
        </div>
        <p class="text-xs text-slate-400">
          这里只改说明性信息。判据阈值与网格参数请用列表里的"修改阈值"入口——那边每项改动都逐项留痕。
        </p>
        <div class="grid grid-cols-2 gap-3">
          <label class="col-span-2 text-sm text-slate-600">
            名称
            <input v-model="editForm.name" class="mt-1 w-full border border-slate-200 rounded px-2 py-1.5 text-sm" />
          </label>
          <label class="text-sm text-slate-600">
            来源
            <input v-model="editForm.source" class="mt-1 w-full border border-slate-200 rounded px-2 py-1.5 text-sm" placeholder="如：奇瑞 T29 技术协议" />
          </label>
          <label class="text-sm text-slate-600">
            版本
            <input v-model="editForm.revision" class="mt-1 w-full border border-slate-200 rounded px-2 py-1.5 text-sm" />
          </label>
          <label class="col-span-2 text-sm text-slate-600">
            适用范围
            <input v-model="editForm.scope" class="mt-1 w-full border border-slate-200 rounded px-2 py-1.5 text-sm" placeholder="如：碰撞 / NVH / 内饰" />
          </label>
          <label class="col-span-2 text-sm text-slate-600">
            说明
            <textarea v-model="editForm.description" rows="3" class="mt-1 w-full border border-slate-200 rounded px-2 py-1.5 text-sm" />
          </label>
        </div>
        <div class="flex justify-end gap-2 pt-1">
          <button class="px-3 py-1.5 rounded-md text-sm text-slate-600 hover:bg-slate-100" @click="editing = null">取消</button>
          <button
            class="px-3 py-1.5 rounded-md bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50"
            :disabled="savingMeta || !editForm.name.trim()"
            @click="saveMeta"
          >
            {{ savingMeta ? "保存中…" : "保存" }}
          </button>
        </div>
      </div>
    </div>

    <!-- 导入客户质量卡 -->
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
          <label class="text-sm text-slate-600">
            模板 id <span class="text-rose-500">*</span>
            <input
              v-model="importForm.template_id"
              class="mt-1 w-full border border-slate-200 rounded px-2 py-1.5 text-sm font-mono"
              placeholder="chery-t29-ip"
            />
          </label>
          <label class="text-sm text-slate-600">
            名称 <span class="text-rose-500">*</span>
            <input v-model="importForm.name" class="mt-1 w-full border border-slate-200 rounded px-2 py-1.5 text-sm" placeholder="奇瑞 T29 IP 质量卡" />
          </label>
          <label class="text-sm text-slate-600">
            来源
            <input v-model="importForm.source" class="mt-1 w-full border border-slate-200 rounded px-2 py-1.5 text-sm" placeholder="客户规范文号" />
          </label>
          <label class="text-sm text-slate-600">
            版本
            <input v-model="importForm.revision" class="mt-1 w-full border border-slate-200 rounded px-2 py-1.5 text-sm" />
          </label>
          <label class="col-span-2 text-sm text-slate-600">
            适用范围
            <input v-model="importForm.scope" class="mt-1 w-full border border-slate-200 rounded px-2 py-1.5 text-sm" placeholder="如：碰撞 / NVH / 内饰" />
          </label>
          <label class="col-span-2 text-sm text-slate-600">
            判定侧 .ansa_qual <span class="text-rose-500">*</span>
            <input
              type="file"
              accept=".ansa_qual,.txt"
              class="mt-1 w-full text-sm"
              @change="qualFile = ($event.target as HTMLInputElement).files?.[0] ?? null"
            />
          </label>
          <label class="col-span-2 text-sm text-slate-600">
            生成侧 .ansa_mpar（可省略，省略时沿用内置模板的生成参数）
            <input
              type="file"
              accept=".ansa_mpar,.txt"
              class="mt-1 w-full text-sm"
              @change="mparFile = ($event.target as HTMLInputElement).files?.[0] ?? null"
            />
          </label>
          <label class="col-span-2 text-sm text-slate-600">
            说明
            <textarea v-model="importForm.description" rows="2" class="mt-1 w-full border border-slate-200 rounded px-2 py-1.5 text-sm" />
          </label>
        </div>
        <div class="flex justify-end gap-2 pt-1">
          <button class="px-3 py-1.5 rounded-md text-sm text-slate-600 hover:bg-slate-100" @click="showImport = false">取消</button>
          <button
            class="px-3 py-1.5 rounded-md bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50"
            :disabled="importing || !qualFile || !importForm.template_id.trim() || !importForm.name.trim()"
            @click="doImport"
          >
            {{ importing ? "导入中…" : "导入" }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
