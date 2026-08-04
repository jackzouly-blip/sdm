<script setup lang="ts">
/**
 * 控制卡模板库（整份存档）。
 *
 * 与材料模板相反：控制卡没有 ID、彼此无引用，拆解入库没有收益；而
 * *CONTROL_* 之间的取值是一套互相配合的策略（时间步/接触/沙漏/输出频率），
 * 拆开反而丢了整体性。所以整份 k 文件原样存、原样导出。
 *
 * 按**分析类型**分组而非按零件——策略跟分析类型走：气囊展开和整车碰撞
 * 的沙漏系数、接触设置、输出频率是完全不同的取向。
 *
 * 单位制必须人工声明：控制卡自身推不出（不像材料能从密度反推），
 * 而 ENDTIM/DT2MS/DT 全是时间量纲，蒙错了整份策略都是错的。
 */
import { computed, onMounted, ref } from "vue";
import { simApi, errMsg } from "@/api";
import type { SimControlTemplate, SimTemplateRelease } from "@/api/types";
import { useAuthStore } from "@/stores/auth";
import { parseTemplate, parseHint, type ParsePhase } from "./templateParse";
import {
  AlertTriangle, Download, Loader2, Plus, RefreshCw, Settings2, Trash2, Upload,
} from "lucide-vue-next";

const auth = useAuthStore();
const isAdmin = computed(() => !!auth.me?.is_admin);

const list = ref<SimControlTemplate[]>([]);
const selected = ref<SimControlTemplate | null>(null);
const loading = ref(true);
const error = ref("");
const typeFilter = ref("");

const creating = ref(false);
const form = ref({
  name: "",
  analysis_type: "气囊展开",
  unit_system: "mm-kg-ms",
  description: "",
  keyword_text: "",
  source_name: "",
});

const analysisTypes = computed(() =>
  [...new Set(list.value.map((t) => t.analysis_type).filter(Boolean))].sort()
);
const filtered = computed(() =>
  list.value.filter((t) => !typeFilter.value || t.analysis_type === typeFilter.value)
);

/** 求解策略摘要由 vektor3d cae.template.parse 回填；没接上时为空 */
const summary = computed<Record<string, string>>(() => {
  if (!selected.value?.summary_json) return {};
  try {
    const s = JSON.parse(selected.value.summary_json);
    return s.strategy ?? {};
  } catch {
    return {};
  }
});

/** 解析暴露的问题。ERR 是"求解器不报错但结果是错的"那一类，必须显眼。 */
const issues = computed<{ level: string; message: string; keyword?: string }[]>(() => {
  if (!selected.value?.summary_json) return [];
  try {
    return JSON.parse(selected.value.summary_json).issues ?? [];
  } catch {
    return [];
  }
});
const errCount = computed(() => issues.value.filter((i) => i.level === "ERR").length);

const parsing = ref("");
const releases = ref<SimTemplateRelease[]>([]);
const publishing = ref(false);

async function publish(tid: string) {
  publishing.value = true;
  try {
    await simApi.publishTemplate("control", tid);
    releases.value = await simApi.listTemplateReleases("control", tid);
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
const notice = ref("");

/**
 * 解析（或重新解析）当前模板。解析失败绝不影响已入库的正本。
 *
 * afterCreate 时失败降级为提示而非报错：模板已经存进 SDM 了，用红色报错会
 * 让人以为入库也失败了。手动点「解析」失败才是真的错——那是用户主动要的结果。
 */
async function reparse(tid: string, afterCreate = false) {
  parsing.value = tid;
  if (!afterCreate) error.value = "";
  notice.value = "";
  try {
    const out = await parseTemplate("control", tid, (p, d) => {
      parsePhase.value = (d ? `解析中 · ${d}` : p) as ParsePhase;
    });
    selected.value = await simApi.getControlTemplate(tid);
    releases.value = await simApi.listTemplateReleases("control", tid);
    notice.value = out.errors.length
      ? `解析完成，发现 ${out.errors.length} 个必须处理的问题`
      : out.warnings.length
        ? `解析完成，${out.warnings.length} 条提醒`
        : "解析完成，未发现问题";
  } catch (e) {
    if (afterCreate) notice.value = parseHint(e);
    else error.value = parseHint(e).replace(/^已入库；/, "");
  } finally {
    parsing.value = "";
    parsePhase.value = "";
  }
}

async function load() {
  loading.value = true;
  error.value = "";
  try {
    list.value = await simApi.listControlTemplates();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}

async function open(tid: string) {
  try {
    selected.value = await simApi.getControlTemplate(tid);
    releases.value = await simApi.listTemplateReleases("control", tid);
  } catch (e) {
    error.value = errMsg(e);
  }
}

async function pickFile(ev: Event) {
  const f = (ev.target as HTMLInputElement).files?.[0];
  if (!f) return;
  form.value.keyword_text = await f.text();
  form.value.source_name = f.name;
  if (!form.value.name) form.value.name = f.name.replace(/\.[kK]$|\.key$/, "");
}

async function create() {
  if (!form.value.name.trim() || !form.value.keyword_text) return;
  try {
    const t = await simApi.createControlTemplate({
      name: form.value.name.trim(),
      unit_system: form.value.unit_system,
      keyword_text: form.value.keyword_text,
      analysis_type: form.value.analysis_type,
      description: form.value.description,
      source_name: form.value.source_name,
    });
    creating.value = false;
    form.value = {
      name: "", analysis_type: "气囊展开", unit_system: "mm-kg-ms",
      description: "", keyword_text: "", source_name: "",
    };
    await load();
    await open(t.id);
    await reparse(t.id, true);   // 入库已成功；解析失败只提示，不回滚
  } catch (e) {
    error.value = errMsg(e);
  }
}

async function remove(tid: string) {
  if (!confirm("删除该控制卡模板？")) return;
  try {
    await simApi.deleteControlTemplate(tid);
    if (selected.value?.id === tid) selected.value = null;
    await load();
  } catch (e) {
    error.value = errMsg(e);
  }
}

const keywordCount = computed(() =>
  (selected.value?.keyword_text?.match(/^\*/gm) ?? []).length
);

onMounted(load);
</script>

<template>
  <div class="flex h-full flex-col">
    <div class="flex items-center gap-3 border-b px-4 py-2.5">
      <Settings2 class="h-4 w-4 text-slate-500" />
      <h1 class="text-sm font-medium">控制卡模板库</h1>
      <span class="text-xs text-slate-500">一套控制卡 = 一个模板，整份存档、原样导出</span>
      <div class="flex-1" />
      <select v-model="typeFilter" class="rounded border px-2 py-1 text-xs">
        <option value="">全部分析类型</option>
        <option v-for="t in analysisTypes" :key="t" :value="t">{{ t }}</option>
      </select>
      <button v-if="isAdmin" class="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-slate-50"
              @click="creating = !creating">
        <Plus class="h-3.5 w-3.5" /> 新建
      </button>
    </div>

    <div v-if="creating" class="space-y-2 border-b bg-slate-50/60 px-4 py-3">
      <div class="flex items-end gap-2">
        <label class="text-xs">
          <span class="mb-1 block text-slate-600">名称</span>
          <input v-model="form.name" class="w-52 rounded border px-2 py-1" placeholder="如：气囊展开控制卡" />
        </label>
        <label class="text-xs">
          <span class="mb-1 block text-slate-600">分析类型</span>
          <input v-model="form.analysis_type" class="w-32 rounded border px-2 py-1" list="atypes" />
          <datalist id="atypes">
            <option v-for="t in analysisTypes" :key="t" :value="t" />
            <option value="气囊展开" />
            <option value="整车碰撞" />
            <option value="跌落" />
          </datalist>
        </label>
        <label class="text-xs">
          <span class="mb-1 block text-slate-600">单位制（必填，控制卡推不出）</span>
          <select v-model="form.unit_system" class="rounded border px-2 py-1">
            <option value="mm-kg-ms">mm-kg-ms (kN/GPa/ms)</option>
            <option value="t-mm-s">t-mm-s (N/MPa/s)</option>
            <option value="mm-g-ms">mm-g-ms (N/MPa/ms)</option>
          </select>
        </label>
        <label class="flex-1 text-xs">
          <span class="mb-1 block text-slate-600">说明</span>
          <input v-model="form.description" class="w-full rounded border px-2 py-1" />
        </label>
      </div>
      <div class="flex items-center gap-2">
        <label class="inline-flex cursor-pointer items-center gap-1 rounded border bg-white px-2 py-1 text-xs">
          <Upload class="h-3.5 w-3.5" /> 选择 .k 文件
          <input type="file" accept=".k,.key,.dyn" class="hidden" @change="pickFile" />
        </label>
        <span v-if="form.source_name" class="text-xs text-slate-600">
          {{ form.source_name }}（{{ (form.keyword_text.length / 1024).toFixed(1) }} KB）
        </span>
        <div class="flex-1" />
        <button class="rounded bg-slate-800 px-3 py-1 text-xs text-white"
                :disabled="!form.keyword_text" @click="create">创建</button>
        <button class="rounded border px-3 py-1 text-xs" @click="creating = false">取消</button>
      </div>
    </div>

    <p v-if="error" class="border-b bg-red-50 px-4 py-2 text-xs text-red-700">{{ error }}</p>
    <p v-else-if="notice" class="border-b bg-sky-50 px-4 py-2 text-xs text-sky-800">{{ notice }}</p>

    <div class="flex flex-1 overflow-hidden">
      <div class="w-72 shrink-0 overflow-auto border-r">
        <div v-if="loading" class="flex items-center gap-2 p-4 text-xs text-slate-500">
          <Loader2 class="h-3.5 w-3.5 animate-spin" /> 加载中
        </div>
        <p v-else-if="!filtered.length" class="p-4 text-xs text-slate-500">还没有控制卡模板</p>
        <button v-for="t in filtered" :key="t.id"
                class="block w-full border-b px-3 py-2 text-left hover:bg-slate-50"
                :class="selected?.id === t.id ? 'bg-slate-100' : ''"
                @click="open(t.id)">
          <div class="truncate text-sm">{{ t.name }}</div>
          <div class="mt-0.5 flex items-center gap-1.5 text-[11px] text-slate-500">
            <span v-if="t.analysis_type" class="rounded bg-slate-100 px-1">{{ t.analysis_type }}</span>
            <span>{{ t.unit_system }}</span>
            <span>r{{ t.revision }}</span>
          </div>
        </button>
      </div>

      <div v-if="!selected" class="flex flex-1 items-center justify-center text-xs text-slate-400">
        选择一个模板查看内容
      </div>
      <div v-else class="flex flex-1 flex-col overflow-hidden">
        <div class="flex items-center gap-2 border-b px-4 py-2">
          <span class="text-sm font-medium">{{ selected.name }}</span>
          <span v-if="selected.analysis_type" class="rounded bg-slate-100 px-1.5 py-0.5 text-[11px]">
            {{ selected.analysis_type }}
          </span>
          <span class="rounded bg-slate-100 px-1.5 py-0.5 text-[11px]">{{ selected.unit_system }}</span>
          <span v-if="errCount" class="rounded bg-red-100 px-1.5 py-0.5 text-[11px] text-red-700">
            {{ errCount }} 个问题
          </span>
          <span class="text-xs text-slate-500">{{ selected.description }}</span>
          <div class="flex-1" />
          <button v-if="isAdmin"
                  class="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs
                         hover:bg-slate-50 disabled:opacity-50"
                  :disabled="!!parsing" @click="reparse(selected.id)">
            <Loader2 v-if="parsing === selected.id" class="h-3.5 w-3.5 animate-spin" />
            <RefreshCw v-else class="h-3.5 w-3.5" />
            {{ parsing === selected.id ? (parsePhase || "解析中") : "解析" }}
          </button>
          <button v-if="isAdmin"
                  class="inline-flex items-center gap-1 rounded bg-slate-800 px-2 py-1 text-xs text-white
                         disabled:opacity-50"
                  :disabled="publishing" @click="publish(selected.id)">
            <Loader2 v-if="publishing" class="h-3.5 w-3.5 animate-spin" />
            发布 v{{ (releases[0]?.version_no ?? 0) + 1 }}
          </button>
          <a :href="simApi.controlTemplateExportUrl(selected.id)"
             class="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-slate-50">
            <Download class="h-3.5 w-3.5" /> 导出
          </a>
          <button v-if="isAdmin" class="rounded border px-2 py-1 text-xs text-red-600 hover:bg-red-50"
                  @click="remove(selected.id)">
            <Trash2 class="h-3.5 w-3.5" />
          </button>
        </div>

        <!-- 求解策略摘要：原始文件里只是一堆孤立数字，这些是算出来的 -->
        <div v-if="Object.keys(summary).length" class="grid grid-cols-2 gap-x-6 gap-y-1 border-b px-4 py-2 text-xs
                    md:grid-cols-3">
          <div v-for="(v, k) in summary" :key="k" class="flex gap-2">
            <span class="text-slate-500">{{ k }}</span>
            <span class="font-medium">{{ v }}</span>
          </div>
        </div>
        <div v-if="releases.length" class="border-b px-4 py-2 text-xs">
          <span class="text-slate-500 mr-2">已发布版本（项目引用的是版本快照，不随编辑变化）</span>
          <span v-for="r in releases" :key="r.id" class="mr-3 inline-flex items-center gap-1">
            <a :href="simApi.templateReleaseUrl(r.id)" class="text-indigo-700 hover:underline">
              v{{ r.version_no }}</a>
            <span class="text-slate-400">{{ new Date(r.created_at * 1000).toLocaleDateString() }}</span>
            <button class="text-slate-400 hover:text-slate-700" title="复制版本 id（项目引用用）"
                    @click="copyRid(r.id)">⧉</button>
          </span>
        </div>
        <p v-else class="border-b px-4 py-2 text-[11px] text-slate-400">
          尚无求解策略摘要 —— 点上方「解析」调用 vektor3d 生成（需桌面端已启动）
        </p>

        <!-- ERR 是求解器不会报错、但结果一定错的那一类，单独列出来 -->
        <div v-if="issues.length" class="max-h-32 overflow-auto border-b px-4 py-2 text-xs">
          <div v-for="(it, i) in issues" :key="i" class="flex items-start gap-1.5 py-0.5">
            <AlertTriangle class="mt-0.5 h-3 w-3 shrink-0"
                           :class="it.level === 'ERR' ? 'text-red-600' : 'text-amber-500'" />
            <span :class="it.level === 'ERR' ? 'text-red-700' : 'text-slate-600'">
              <span v-if="it.keyword" class="font-mono">{{ it.keyword }}</span>
              {{ it.message }}
            </span>
          </div>
        </div>

        <div class="flex items-center gap-3 border-b px-4 py-1.5 text-[11px] text-slate-500">
          <span>{{ keywordCount }} 个关键字</span>
          <span v-if="selected.source_name">来源 {{ selected.source_name }}</span>
          <span v-if="selected.source_sha256" class="font-mono">sha {{ selected.source_sha256 }}</span>
        </div>

        <pre class="flex-1 overflow-auto bg-slate-50 px-4 py-2 font-mono text-[11px] leading-relaxed"
        >{{ selected.keyword_text }}</pre>
      </div>
    </div>
  </div>
</template>
