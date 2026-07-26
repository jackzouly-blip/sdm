<script setup lang="ts">
/**
 * 编排画布。
 *
 * 关键设计：**这个编辑器不认识任何具体节点类型**。左侧节点面板与右侧参数表单
 * 全部由 /sim/node-types 返回的 params_schema 渲染。后端注册一个新类型（比如
 * vektor3d 网格能力就绪后的 mesh.generate），这里零改动就能用上。
 */
import { computed, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { VueFlow, useVueFlow, type Connection } from "@vue-flow/core";
import { Background } from "@vue-flow/background";
import { Controls } from "@vue-flow/controls";
import { simApi, errMsg } from "@/api";
import type { DagDoc, JsonSchemaProp, NodeTypeDef, PipelineDef } from "@/api/types";
import { ArrowLeft, Play, Save, Trash2, CheckCircle2, AlertCircle } from "lucide-vue-next";
import "@vue-flow/core/dist/style.css";
import "@vue-flow/core/dist/theme-default.css";
import "@vue-flow/controls/dist/style.css";

const props = defineProps<{ pid: string }>();
const router = useRouter();
const { onConnect, addEdges } = useVueFlow();

/**
 * 画布节点的本地类型。
 *
 * 不直接用 @vue-flow/core 的 Node：它的泛型嵌套很深，computed 推导会触发
 * "Type instantiation is excessively deep"。这里只声明我们实际用到的字段，
 * 结构上与 Vue Flow 兼容，既能通过类型检查又保住了 data 的类型安全。
 */
interface EditorNode {
  id: string;
  position: { x: number; y: number };
  data: { type: string; label: string; params: Record<string, unknown> };
  label: string;
  style: Record<string, string>;
}
interface EditorEdge {
  id: string;
  source: string;
  target: string;
  animated: boolean;
}

const def = ref<PipelineDef | null>(null);
const nodeTypes = ref<NodeTypeDef[]>([]);
const nodes = ref<EditorNode[]>([]);
const edges = ref<EditorEdge[]>([]);
const selectedId = ref<string | null>(null);
const loading = ref(true);
const saving = ref(false);
const error = ref("");
const validation = ref<{ ok: boolean; error?: string } | null>(null);

/** 分类配色：让节点在画布上一眼可辨执行位置。 */
const CATEGORY_STYLE: Record<string, { bg: string; border: string; text: string; label: string }> = {
  internal: { bg: "#eff6ff", border: "#93c5fd", text: "#1d4ed8", label: "SDM 内置" },
  capability: { bg: "#f5f3ff", border: "#c4b5fd", text: "#6d28d9", label: "外部能力" },
  hpc: { bg: "#ecfdf5", border: "#6ee7b7", text: "#047857", label: "集群求解" },
  manual: { bg: "#fffbeb", border: "#fcd34d", text: "#b45309", label: "人工" },
};

const typeMap = computed(
  () => new Map(nodeTypes.value.map((t) => [t.type_id, t]))
);
const selectedNode = computed(() =>
  nodes.value.find((n) => n.id === selectedId.value) ?? null
);
const selectedType = computed(() =>
  selectedNode.value ? typeMap.value.get(selectedNode.value.data.type) ?? null : null
);
const paramProps = computed<[string, JsonSchemaProp][]>(() =>
  Object.entries(selectedType.value?.params_schema?.properties ?? {})
);

function styleOf(typeId: string) {
  const t = typeMap.value.get(typeId);
  return CATEGORY_STYLE[t?.category ?? "internal"] ?? CATEGORY_STYLE.internal;
}

/** DAG 文档 → Vue Flow 图元 */
function docToGraph(doc: DagDoc) {
  nodes.value = (doc.nodes ?? []).map((n) => {
    const s = styleOf(n.type);
    return {
      id: n.id,
      position: n.position ?? { x: 0, y: 0 },
      data: {
        type: n.type,
        label: n.label ?? typeMap.value.get(n.type)?.label ?? n.type,
        params: n.params ?? {},
      },
      label: n.label || typeMap.value.get(n.type)?.label || n.type,
      style: {
        background: s.bg,
        border: `1px solid ${s.border}`,
        color: s.text,
        borderRadius: "8px",
        padding: "8px 12px",
        fontSize: "13px",
        minWidth: "140px",
      },
    };
  });
  edges.value = (doc.edges ?? []).map((e) => ({
    id: `${e.from}->${e.to}`,
    source: e.from,
    target: e.to,
    animated: true,
  }));
}

/** Vue Flow 图元 → DAG 文档 */
function graphToDoc(): DagDoc {
  return {
    nodes: nodes.value.map((n) => ({
      id: n.id,
      type: n.data.type,
      label: n.data.label,
      params: n.data.params ?? {},
      position: { x: Math.round(n.position.x), y: Math.round(n.position.y) },
    })),
    edges: edges.value.map((e) => ({ from: e.source, to: e.target })),
  };
}

onConnect((c: Connection) => {
  if (c.source === c.target) return; // 自环由后端拒绝，这里先挡一道
  addEdges([{ ...c, id: `${c.source}->${c.target}`, animated: true }]);
});

let seq = 0;
function addNode(t: NodeTypeDef) {
  seq += 1;
  const id = `${t.type_id.split(".").pop()}_${Date.now().toString(36)}${seq}`;
  const s = CATEGORY_STYLE[t.category] ?? CATEGORY_STYLE.internal;
  // 依次错开摆放，避免新节点叠在一起
  const pos = { x: 80 + ((nodes.value.length * 40) % 280), y: 60 + nodes.value.length * 70 };
  const defaults: Record<string, unknown> = {};
  for (const [k, p] of Object.entries(t.params_schema?.properties ?? {})) {
    if (p.default !== undefined) defaults[k] = p.default;
  }
  nodes.value.push({
    id,
    position: pos,
    data: { type: t.type_id, label: t.label, params: defaults },
    label: t.label,
    style: {
      background: s.bg,
      border: `1px solid ${s.border}`,
      color: s.text,
      borderRadius: "8px",
      padding: "8px 12px",
      fontSize: "13px",
      minWidth: "140px",
    },
  });
  selectedId.value = id;
}

function removeSelected() {
  if (!selectedId.value) return;
  const id = selectedId.value;
  nodes.value = nodes.value.filter((n) => n.id !== id);
  edges.value = edges.value.filter((e) => e.source !== id && e.target !== id);
  selectedId.value = null;
}

function setParam(key: string, value: unknown) {
  if (!selectedNode.value) return;
  selectedNode.value.data.params = {
    ...(selectedNode.value.data.params ?? {}),
    [key]: value,
  };
}

function setLabel(v: string) {
  if (!selectedNode.value) return;
  const fallback = typeMap.value.get(selectedNode.value.data.type)?.label ?? "";
  selectedNode.value.data.label = v || fallback;
  selectedNode.value.label = v || fallback;
}

async function revalidate() {
  if (!nodes.value.length) {
    validation.value = null;
    return;
  }
  try {
    validation.value = await simApi.validatePipeline(graphToDoc());
  } catch (e) {
    validation.value = { ok: false, error: errMsg(e) };
  }
}

async function save() {
  saving.value = true;
  error.value = "";
  try {
    await simApi.updatePipeline(props.pid, { doc: graphToDoc() });
    def.value = await simApi.getPipeline(props.pid);
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    saving.value = false;
  }
}

async function saveAndRun() {
  await save();
  if (error.value) return;
  try {
    const run = await simApi.startRun(props.pid, {});
    router.push({ name: "sim-run", params: { rid: run.id } });
  } catch (e) {
    error.value = errMsg(e);
  }
}

async function load() {
  loading.value = true;
  try {
    const [types, d] = await Promise.all([
      simApi.listNodeTypes(),
      simApi.getPipeline(props.pid),
    ]);
    nodeTypes.value = types;
    def.value = d;
    docToGraph(d.doc);
    await revalidate();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}

// 图结构变化即校验，把环/悬空边在编辑时就暴露出来
watch(
  () => [nodes.value.length, edges.value.length],
  () => void revalidate()
);

onMounted(load);
</script>

<template>
  <div class="w-full h-[calc(100vh-7rem)] flex flex-col">
    <div class="flex items-center gap-2 mb-3">
      <button
        class="flex items-center gap-1 text-sm text-slate-500 hover:text-slate-700"
        @click="router.push({ name: 'sim-pipelines' })"
      >
        <ArrowLeft :size="15" /> 返回
      </button>
      <h1 class="font-semibold text-slate-800">{{ def?.name ?? "编排" }}</h1>
      <span v-if="def" class="text-xs text-slate-400">v{{ def.version }}</span>

      <div
        v-if="validation"
        class="flex items-center gap-1 text-xs px-2 py-1 rounded"
        :class="validation.ok ? 'bg-emerald-50 text-emerald-700' : 'bg-rose-50 text-rose-700'"
      >
        <CheckCircle2 v-if="validation.ok" :size="13" />
        <AlertCircle v-else :size="13" />
        {{ validation.ok ? "DAG 合法" : validation.error }}
      </div>

      <div class="ml-auto flex gap-2">
        <button
          class="flex items-center gap-1.5 px-3 py-1.5 rounded-md border border-slate-300 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50"
          :disabled="saving"
          @click="save"
        >
          <Save :size="15" /> {{ saving ? "保存中…" : "保存" }}
        </button>
        <button
          class="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50"
          :disabled="saving || !validation?.ok"
          @click="saveAndRun"
        >
          <Play :size="15" /> 保存并运行
        </button>
      </div>
    </div>

    <div v-if="error" class="mb-2 px-3 py-2 rounded-md bg-rose-50 text-rose-700 text-sm">
      {{ error }}
    </div>

    <div class="flex-1 flex gap-3 min-h-0">
      <!-- 节点面板：完全由后端注册表生成 -->
      <div class="w-52 shrink-0 border border-slate-200 rounded-lg bg-white p-2 overflow-auto">
        <div class="text-xs font-medium text-slate-500 px-1 mb-2">节点类型</div>
        <button
          v-for="t in nodeTypes"
          :key="t.type_id"
          class="w-full text-left px-2 py-1.5 rounded-md mb-1 border text-xs hover:shadow-sm transition"
          :style="{
            background: CATEGORY_STYLE[t.category]?.bg,
            borderColor: CATEGORY_STYLE[t.category]?.border,
            color: CATEGORY_STYLE[t.category]?.text,
          }"
          :title="t.description"
          @click="addNode(t)"
        >
          <div class="font-medium">{{ t.label }}</div>
          <div class="opacity-60 mt-0.5">{{ CATEGORY_STYLE[t.category]?.label }}</div>
        </button>
      </div>

      <!-- 画布 -->
      <div class="flex-1 border border-slate-200 rounded-lg bg-slate-50 overflow-hidden min-w-0">
        <VueFlow
          v-model:nodes="nodes"
          v-model:edges="edges"
          :default-viewport="{ zoom: 1 }"
          fit-view-on-init
          @node-click="(e: any) => (selectedId = e.node.id)"
          @pane-click="selectedId = null"
        >
          <Background pattern-color="#cbd5e1" :gap="16" />
          <Controls />
        </VueFlow>
      </div>

      <!-- 参数面板：按 params_schema 渲染 -->
      <div class="w-72 shrink-0 border border-slate-200 rounded-lg bg-white p-3 overflow-auto">
        <template v-if="selectedNode && selectedType">
          <div class="flex items-center gap-2 mb-3">
            <div class="text-sm font-medium text-slate-700">{{ selectedType.label }}</div>
            <button
              class="ml-auto p-1 rounded hover:bg-rose-50 text-slate-400 hover:text-rose-600"
              title="删除节点"
              @click="removeSelected"
            >
              <Trash2 :size="14" />
            </button>
          </div>
          <p class="text-xs text-slate-500 mb-3">{{ selectedType.description }}</p>

          <label class="block text-xs text-slate-600 mb-1">节点名称</label>
          <input
            :value="selectedNode.data.label"
            class="w-full px-2 py-1.5 border border-slate-300 rounded text-sm mb-3"
            @input="setLabel(($event.target as HTMLInputElement).value)"
          />

          <div v-if="!paramProps.length" class="text-xs text-slate-400">该节点无需配置参数</div>
          <div v-for="[key, prop] in paramProps" :key="key" class="mb-3">
            <label class="block text-xs text-slate-600 mb-1">
              {{ prop.title || key }}
              <span
                v-if="selectedType.params_schema.required?.includes(key)"
                class="text-rose-500"
                >*</span
              >
            </label>
            <select
              v-if="prop.enum"
              :value="selectedNode.data.params?.[key]"
              class="w-full px-2 py-1.5 border border-slate-300 rounded text-sm"
              @change="setParam(key, ($event.target as HTMLSelectElement).value)"
            >
              <option v-for="opt in prop.enum" :key="String(opt)" :value="opt">
                {{ opt }}
              </option>
            </select>
            <textarea
              v-else-if="prop.type === 'object'"
              :value="JSON.stringify(selectedNode.data.params?.[key] ?? {}, null, 2)"
              rows="4"
              class="w-full px-2 py-1.5 border border-slate-300 rounded text-xs font-mono"
              @change="
                (e) => {
                  try {
                    setParam(key, JSON.parse((e.target as HTMLTextAreaElement).value));
                  } catch {
                    /* JSON 不合法时保留原值，保存时后端会再校验 */
                  }
                }
              "
            ></textarea>
            <input
              v-else
              :value="selectedNode.data.params?.[key] ?? ''"
              :type="prop.type === 'number' ? 'number' : 'text'"
              class="w-full px-2 py-1.5 border border-slate-300 rounded text-sm"
              @input="setParam(key, ($event.target as HTMLInputElement).value)"
            />
            <p v-if="prop.description" class="text-xs text-slate-400 mt-1">
              {{ prop.description }}
            </p>
          </div>
        </template>
        <p v-else class="text-xs text-slate-400">
          从左侧添加节点，或点击画布上的节点编辑参数。<br /><br />
          拖动节点边缘的连接点可建立依赖关系。
        </p>
      </div>
    </div>
  </div>
</template>
