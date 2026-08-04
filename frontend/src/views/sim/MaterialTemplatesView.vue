<script setup lang="ts">
/**
 * 材料模板文件（组装式）。
 *
 * 模板只记"选了哪几张材料卡"，导出时按各卡原文拼出一份可 *INCLUDE 的 MAT.K。
 * 材料库才是正本——不另存整文件，否则改了库内材料而模板不变（或反之）必然分叉。
 *
 * 选卡时实时跑组装校验。拦的三类问题都是"语法合法但求解器静默算错"：
 * 单位制不一致（差 1000 倍）、MID 撞车（后者覆盖前者）、LCID 撞车（曲线张冠李戴）。
 * 所以校验结果要显眼——用户不会主动去点"检查"。
 *
 * 读对所有登录用户开放；建/改/删仅管理员，后端同样强制。
 */
import { computed, onMounted, ref, watch } from "vue";
import { simApi, errMsg } from "@/api";
import type {
  SimMaterial,
  SimMaterialDetail,
  SimMaterialTemplate,
  SimMaterialTemplateDetail,
  SimTemplateCheck,
  SimTemplateRelease,
} from "@/api/types";
import { useAuthStore } from "@/stores/auth";
import { parseTemplate, parseHint, type ParsePhase } from "./templateParse";
import {
  AlertTriangle,
  CheckCircle2,
  Download,
  FileCode,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  Trash2,
} from "lucide-vue-next";

const auth = useAuthStore();
const isAdmin = computed(() => !!auth.me?.is_admin);

const templates = ref<SimMaterialTemplate[]>([]);
const selected = ref<SimMaterialTemplateDetail | null>(null);
const loading = ref(true);
const error = ref("");
const notice = ref("");

/**
 * vektor3d 的复核结果。与页面自带的 check 不重复而是加深一层：
 * check 用正则扫曲线号，只知道"文本里出现过这个数"；vektor3d 带关键字 schema，
 * 知道**每个材料类型的哪个字段位才是曲线号**——8.314 这种气体常数不会被当成曲线，
 * 而 *MAT_FABRIC 少写三张必需卡这类问题只有它能发现。
 */
const parsing = ref(false);
const releases = ref<SimTemplateRelease[]>([]);
const publishing = ref(false);

async function publish(tid: string) {
  publishing.value = true;
  try {
    await simApi.publishTemplate("material", tid);
    releases.value = await simApi.listTemplateReleases("material", tid);
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    publishing.value = false;
  }
}

function copyRid(rid: string) {
  navigator.clipboard?.writeText(rid);
}
const parsePhase = ref<ParsePhase>("");
const parseIssues = computed<{ level: string; message: string; keyword?: string }[]>(() => {
  if (!selected.value?.summary_json) return [];
  try {
    return JSON.parse(selected.value.summary_json).issues ?? [];
  } catch {
    return [];
  }
});
const parseErrCount = computed(() => parseIssues.value.filter((i) => i.level === "ERR").length);

/** 复核当前模板。失败只提示，不动已入库的模板。 */
async function reparse(tid: string, afterCreate = false) {
  parsing.value = true;
  if (!afterCreate) error.value = "";
  notice.value = "";
  try {
    const out = await parseTemplate("material", tid, (p, d) => {
      parsePhase.value = (d ? `复核中 · ${d}` : p) as ParsePhase;
    });
    selected.value = await simApi.getMaterialTemplate(tid);
    notice.value = out.errors.length
      ? `复核完成，发现 ${out.errors.length} 个必须处理的问题`
      : out.warnings.length
        ? `复核完成，${out.warnings.length} 条提醒`
        : "复核完成，未发现问题";
  } catch (e) {
    if (afterCreate) notice.value = parseHint(e);
    else error.value = parseHint(e).replace(/^已入库；/, "");
  } finally {
    parsing.value = false;
    parsePhase.value = "";
  }
}

// 选卡面板
const editing = ref(false);
const materials = ref<SimMaterial[]>([]);
const expanded = ref<Record<string, SimMaterialDetail>>({});
const picked = ref<string[]>([]);
const check = ref<SimTemplateCheck | null>(null);
const checking = ref(false);
const q = ref("");

// 新建
const creating = ref(false);
const form = ref({ name: "", unit_system: "t-mm-s", description: "" });

const filteredMaterials = computed(() =>
  materials.value.filter((m) => !q.value || m.name.toLowerCase().includes(q.value.toLowerCase()))
);

async function load() {
  loading.value = true;
  error.value = "";
  try {
    templates.value = await simApi.listMaterialTemplates();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}

async function open(tid: string) {
  try {
    selected.value = await simApi.getMaterialTemplate(tid);
    picked.value = selected.value.items.map((i) => i.card_id);
    releases.value = await simApi.listTemplateReleases("material", tid);
    editing.value = false;
    check.value = null;
  } catch (e) {
    error.value = errMsg(e);
  }
}

async function startEdit() {
  editing.value = true;
  if (!materials.value.length) {
    try {
      materials.value = await simApi.listMaterials();
    } catch (e) {
      error.value = errMsg(e);
    }
  }
  runCheck();
}

/** 展开一个材料看它有哪些卡（主卡/伴生 NULL 卡）——按需拉，列表页不预取 */
async function expand(mid: string) {
  if (expanded.value[mid]) return;
  try {
    expanded.value = { ...expanded.value, [mid]: await simApi.getMaterial(mid) };
  } catch (e) {
    error.value = errMsg(e);
  }
}

function toggleCard(cardId: string) {
  picked.value = picked.value.includes(cardId)
    ? picked.value.filter((x) => x !== cardId)
    : [...picked.value, cardId];
}

let checkTimer: ReturnType<typeof setTimeout> | undefined;
watch(picked, () => {
  if (!editing.value) return;
  clearTimeout(checkTimer);
  checkTimer = setTimeout(runCheck, 250);
});

async function runCheck() {
  if (!picked.value.length) {
    check.value = null;
    return;
  }
  checking.value = true;
  try {
    check.value = await simApi.checkMaterialTemplate(
      picked.value,
      selected.value?.unit_system || form.value.unit_system
    );
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    checking.value = false;
  }
}

async function saveItems() {
  if (!selected.value) return;
  const tid = selected.value.id;
  try {
    await simApi.setMaterialTemplateItems(tid, picked.value);
    await load();
    await open(tid);
  } catch (e) {
    error.value = errMsg(e);
    return;
  }
  // 成员定下来才有东西可复核（模板是空着建出来的，创建时刻无内容）。
  // 后端已在换成员时清掉上一次的结果，这里重新算一份。
  if (picked.value.length) await reparse(tid, true);
}

async function create() {
  if (!form.value.name.trim()) return;
  try {
    const t = await simApi.createMaterialTemplate({
      name: form.value.name.trim(),
      unit_system: form.value.unit_system,
      description: form.value.description,
    });
    creating.value = false;
    form.value = { name: "", unit_system: "t-mm-s", description: "" };
    await load();
    await open(t.id);
  } catch (e) {
    error.value = errMsg(e);
  }
}

async function remove(tid: string) {
  if (!confirm("删除该模板？材料库内的材料不受影响。")) return;
  try {
    await simApi.deleteMaterialTemplate(tid);
    if (selected.value?.id === tid) selected.value = null;
    await load();
  } catch (e) {
    error.value = errMsg(e);
  }
}

const problemTone = (level: string) =>
  level === "REJECT" ? "text-red-700 bg-red-50 border-red-200"
    : level === "CONFLICT" ? "text-amber-800 bg-amber-50 border-amber-200"
      : "text-slate-700 bg-slate-50 border-slate-200";

onMounted(load);
</script>

<template>
  <div class="flex h-full flex-col">
    <!-- 工具条 -->
    <div class="flex items-center gap-3 border-b px-4 py-2.5">
      <FileCode class="h-4 w-4 text-slate-500" />
      <h1 class="text-sm font-medium">材料模板文件</h1>
      <span class="text-xs text-slate-500">一组材料卡的具名选择，导出即一份可 *INCLUDE 的 MAT.K</span>
      <div class="flex-1" />
      <button v-if="isAdmin" class="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-slate-50"
              @click="creating = !creating">
        <Plus class="h-3.5 w-3.5" /> 新建模板
      </button>
    </div>

    <div v-if="creating" class="flex items-end gap-2 border-b bg-slate-50/60 px-4 py-3">
      <label class="text-xs">
        <span class="mb-1 block text-slate-600">名称</span>
        <input v-model="form.name" class="w-56 rounded border px-2 py-1" placeholder="如：气囊用材料" />
      </label>
      <label class="text-xs">
        <span class="mb-1 block text-slate-600">单位制</span>
        <select v-model="form.unit_system" class="rounded border px-2 py-1">
          <option value="t-mm-s">t-mm-s (MPa)</option>
          <option value="mm-kg-ms">mm-kg-ms (GPa)</option>
          <option value="SI">SI</option>
        </select>
      </label>
      <label class="flex-1 text-xs">
        <span class="mb-1 block text-slate-600">说明</span>
        <input v-model="form.description" class="w-full rounded border px-2 py-1" />
      </label>
      <button class="rounded bg-slate-800 px-3 py-1 text-xs text-white" @click="create">创建</button>
      <button class="rounded border px-3 py-1 text-xs" @click="creating = false">取消</button>
    </div>

    <p v-if="error" class="border-b bg-red-50 px-4 py-2 text-xs text-red-700">{{ error }}</p>
    <p v-else-if="notice" class="border-b bg-sky-50 px-4 py-2 text-xs text-sky-800">{{ notice }}</p>

    <div class="flex flex-1 overflow-hidden">
      <!-- 左：模板列表 -->
      <div class="w-72 shrink-0 overflow-auto border-r">
        <div v-if="loading" class="flex items-center gap-2 p-4 text-xs text-slate-500">
          <Loader2 class="h-3.5 w-3.5 animate-spin" /> 加载中
        </div>
        <p v-else-if="!templates.length" class="p-4 text-xs text-slate-500">还没有模板</p>
        <button v-for="t in templates" :key="t.id"
                class="block w-full border-b px-3 py-2 text-left hover:bg-slate-50"
                :class="selected?.id === t.id ? 'bg-slate-100' : ''"
                @click="open(t.id)">
          <div class="flex items-center gap-2">
            <span class="truncate text-sm">{{ t.name }}</span>
            <span v-if="t.status === 'deprecated'" class="rounded bg-slate-200 px-1 text-[10px]">停用</span>
          </div>
          <div class="mt-0.5 text-[11px] text-slate-500">
            {{ t.card_count ?? 0 }} 张卡 · {{ t.unit_system }} · r{{ t.revision }}
          </div>
        </button>
      </div>

      <!-- 右：详情 -->
      <div v-if="!selected" class="flex flex-1 items-center justify-center text-xs text-slate-400">
        选择一个模板查看成员
      </div>
      <div v-else class="flex flex-1 flex-col overflow-hidden">
        <div class="flex items-center gap-2 border-b px-4 py-2">
          <span class="text-sm font-medium">{{ selected.name }}</span>
          <span class="rounded bg-slate-100 px-1.5 py-0.5 text-[11px]">{{ selected.unit_system }}</span>
          <span v-if="parseErrCount" class="rounded bg-red-100 px-1.5 py-0.5 text-[11px] text-red-700">
            {{ parseErrCount }} 个问题
          </span>
          <span class="text-xs text-slate-500">{{ selected.description }}</span>
          <div class="flex-1" />
          <button v-if="isAdmin && !editing"
                  class="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs
                         hover:bg-slate-50 disabled:opacity-50"
                  :disabled="parsing || !selected.items.length"
                  :title="selected.items.length ? '调用 vektor3d 按 LS-DYNA 关键字定义深度复核' : '空模板无需复核'"
                  @click="reparse(selected.id)">
            <Loader2 v-if="parsing" class="h-3.5 w-3.5 animate-spin" />
            <RefreshCw v-else class="h-3.5 w-3.5" />
            {{ parsing ? (parsePhase || "复核中") : "深度复核" }}
          </button>
          <button v-if="isAdmin"
                  class="inline-flex items-center gap-1 rounded bg-slate-800 px-2 py-1 text-xs text-white
                         disabled:opacity-50"
                  :disabled="publishing" @click="publish(selected.id)">
            发布 v{{ (releases[0]?.version_no ?? 0) + 1 }}
          </button>
          <span v-if="releases.length" class="text-xs">
            <span v-for="r in releases" :key="r.id" class="mr-2 inline-flex items-center gap-1">
              <a :href="simApi.templateReleaseUrl(r.id)" class="text-indigo-700 hover:underline">v{{ r.version_no }}</a>
              <button class="text-slate-400 hover:text-slate-700" title="复制版本 id（项目引用用）"
                      @click="copyRid(r.id)">⧉</button>
            </span>
          </span>
          <a :href="simApi.materialTemplateExportUrl(selected.id)"
             class="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-slate-50">
            <Download class="h-3.5 w-3.5" /> 导出 MAT.K
          </a>
          <button v-if="isAdmin && !editing"
                  class="rounded border px-2 py-1 text-xs hover:bg-slate-50" @click="startEdit">编辑成员</button>
          <button v-if="isAdmin && editing"
                  class="rounded bg-slate-800 px-2 py-1 text-xs text-white"
                  :disabled="!!check && !check.ok" @click="saveItems">保存</button>
          <button v-if="isAdmin" class="rounded border px-2 py-1 text-xs text-red-600 hover:bg-red-50"
                  @click="remove(selected.id)">
            <Trash2 class="h-3.5 w-3.5" />
          </button>
        </div>

        <!-- vektor3d 深度复核结果：比页面自带的 check 多一层字段级语义 -->
        <div v-if="!editing && parseIssues.length"
             class="max-h-32 overflow-auto border-b px-4 py-2 text-xs">
          <div v-for="(it, i) in parseIssues" :key="i" class="flex items-start gap-1.5 py-0.5">
            <AlertTriangle class="mt-0.5 h-3 w-3 shrink-0"
                           :class="it.level === 'ERR' ? 'text-red-600' : 'text-amber-500'" />
            <span :class="it.level === 'ERR' ? 'text-red-700' : 'text-slate-600'">
              <span v-if="it.keyword" class="font-mono">{{ it.keyword }}</span>
              {{ it.message }}
            </span>
          </div>
        </div>

        <!-- 组装校验：结论要显眼，用户不会主动去点检查 -->
        <div v-if="editing && check" class="border-b px-4 py-2">
          <div v-if="check.ok" class="flex items-center gap-2 text-xs text-emerald-700">
            <CheckCircle2 class="h-4 w-4" />
            可组装：{{ check.cards.length }} 张卡 · MID {{ check.id_ranges.MID.length }} 个 ·
            曲线 {{ check.id_ranges.LCID.length }} 条 · {{ check.unit_system }}
          </div>
          <div v-else class="space-y-1">
            <div class="flex items-center gap-2 text-xs font-medium text-red-700">
              <AlertTriangle class="h-4 w-4" /> 无法组装（{{ check.problems.length }} 项）
            </div>
            <p v-for="(p, i) in check.problems" :key="i"
               class="rounded border px-2 py-1 text-[11px]" :class="problemTone(p.level)">
              [{{ p.level }}] {{ p.message }}
            </p>
          </div>
        </div>

        <!-- 成员列表 / 选卡 -->
        <div v-if="!editing" class="flex-1 overflow-auto">
          <table class="w-full text-xs">
            <thead class="sticky top-0 bg-slate-50 text-left text-slate-600">
              <tr>
                <th class="px-4 py-1.5 font-normal">#</th>
                <th class="px-2 py-1.5 font-normal">材料</th>
                <th class="px-2 py-1.5 font-normal">卡</th>
                <th class="px-2 py-1.5 font-normal">类型</th>
                <th class="px-2 py-1.5 font-normal">MID</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(it, i) in selected.items" :key="it.card_id" class="border-b last:border-0">
                <td class="px-4 py-1.5 text-slate-400">{{ i + 1 }}</td>
                <td class="px-2 py-1.5">{{ it.material_name }}</td>
                <td class="px-2 py-1.5 text-slate-600">{{ it.title }}</td>
                <td class="px-2 py-1.5 text-slate-500">{{ it.mat_type }}</td>
                <td class="px-2 py-1.5 font-mono text-slate-500">{{ it.source_mid ?? "—" }}</td>
              </tr>
            </tbody>
          </table>
          <p v-if="!selected.items.length" class="p-4 text-xs text-slate-500">
            还没有成员，点「编辑成员」从材料库挑选。
          </p>
        </div>

        <div v-else class="flex flex-1 overflow-hidden">
          <div class="flex-1 overflow-auto">
            <div class="sticky top-0 flex items-center gap-2 border-b bg-white px-4 py-2">
              <Search class="h-3.5 w-3.5 text-slate-400" />
              <input v-model="q" placeholder="搜材料牌号" class="flex-1 text-xs outline-none" />
              <span class="text-[11px] text-slate-500">已选 {{ picked.length }} 张</span>
              <Loader2 v-if="checking" class="h-3.5 w-3.5 animate-spin text-slate-400" />
            </div>
            <div v-for="m in filteredMaterials" :key="m.id" class="border-b">
              <button class="flex w-full items-center gap-2 px-4 py-1.5 text-left text-xs hover:bg-slate-50"
                      @click="expand(m.id)">
                <span class="flex-1 truncate">{{ m.name }}</span>
                <span class="text-[11px] text-slate-400">{{ m.card_count ?? 0 }} 卡</span>
              </button>
              <div v-if="expanded[m.id]" class="bg-slate-50/60 pb-1">
                <label v-for="c in expanded[m.id].cards" :key="c.id"
                       class="flex cursor-pointer items-center gap-2 px-8 py-1 text-[11px] hover:bg-slate-100">
                  <input type="checkbox" :checked="picked.includes(c.id)" @change="toggleCard(c.id)" />
                  <span class="flex-1 truncate">{{ c.title || c.mat_type }}</span>
                  <span class="rounded bg-white px-1 text-slate-500">{{ c.variant }}</span>
                  <span class="font-mono text-slate-400">MID {{ c.source_mid ?? "—" }}</span>
                  <span class="text-slate-400">{{ c.unit_system }}</span>
                </label>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
