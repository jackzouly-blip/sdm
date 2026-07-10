<script setup lang="ts">
import { ref, computed, watch, onMounted } from "vue";
import { api, errMsg } from "@/api";
import type { JobTemplate } from "@/api/types";
import { X, Loader2, Rocket, CheckCircle2, FlaskConical } from "lucide-vue-next";

const props = defineProps<{ initDir: string; inputFile?: string }>();
const emit = defineEmits<{ close: []; submitted: [jobid: string] }>();

// 任务类型：pbs=常规(提交到 PBS 队列) / trial=试算(管理节点直跑，不进 PBS)
const taskType = ref<"pbs" | "trial">("pbs");
const isTrial = computed(() => taskType.value === "trial");

// 作业名默认取初始路径的目录名（用户可手动修改）
const name = ref(props.initDir.split("/").filter(Boolean).pop() || "");
const cores = ref(64);
const queue = ref("batch");
const initDir = ref(props.initDir);
const inputFile = ref(props.inputFile || ""); // 相对初始路径
const templateId = ref<number | null>(null);
const templates = ref<JobTemplate[]>([]);
const kfiles = ref<string[]>([]); // 目录下递归找到的 .k/.key（相对路径）
const manualInput = ref(false); // 手动输入路径（无匹配或用户主动切换）

const loading = ref(true);
const submitting = ref(false);
const error = ref("");
const okJobid = ref("");
const queuedInfo = ref<{ id: number; total: number } | null>(null); // 本地排队中(未进入PBS)
const trialOk = ref<{ id: number } | null>(null); // 试算已启动

// 按任务类型过滤模板（作业模板 vs 试算模板）
const visibleTemplates = computed(() =>
  templates.value.filter((t) =>
    isTrial.value ? t.kind === "trial" : (t.kind || "pbs") === "pbs"
  )
);

// 切换类型后，把模板选择重置为当前类型下的第一个
watch([taskType, templates], () => {
  const cur = visibleTemplates.value;
  if (!cur.some((t) => t.id === templateId.value)) {
    templateId.value = cur.length ? cur[0].id : null;
  }
});

onMounted(async () => {
  try {
    templates.value = await api.listTemplates();
    // 仅列当前目录的 .k/.key（不递归）；子目录文件用"手动输入路径"
    try {
      const r = await api.listDir(props.initDir);
      kfiles.value = r.entries
        .filter((e) => !e.is_dir && /\.(k|key)$/i.test(e.name))
        .map((e) => e.name);
    } catch {
      /* 忽略 */
    }
    if (props.inputFile) {
      inputFile.value = props.inputFile; // 文件列表里已勾选的优先
      if (!kfiles.value.includes(props.inputFile)) manualInput.value = true;
    } else if (kfiles.value.length === 1) {
      inputFile.value = kfiles.value[0]; // 只有一个直接选中
    }
    if (kfiles.value.length === 0 && !inputFile.value) manualInput.value = true;
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
});

async function submit() {
  error.value = "";
  if (!name.value.trim()) return (error.value = "请填写任务名称");
  if (!isTrial.value && (!cores.value || cores.value < 1))
    return (error.value = "核数无效");
  if (!inputFile.value.trim()) return (error.value = "请填写输入文件（相对初始路径）");
  if (templateId.value === null)
    return (error.value = isTrial.value ? "请选择试算模板" : "请选择模板");
  submitting.value = true;
  try {
    if (isTrial.value) {
      const r = await api.submitTrial({
        name: name.value.trim(),
        init_dir: initDir.value,
        input_file: inputFile.value.trim(),
        template_id: templateId.value,
      });
      trialOk.value = { id: r.id };
      // 不立即 emit submitted（避免父级关闭弹窗），让成功页给出"去看输出"的引导
      return;
    }
    const r = await api.submitJob({
      name: name.value.trim(),
      cores: Number(cores.value),
      queue: queue.value.trim() || "batch",
      init_dir: initDir.value,
      input_file: inputFile.value.trim(),
      template_id: templateId.value,
    });
    if (r.status === "queued") {
      queuedInfo.value = { id: r.queue_id!, total: r.queued_total ?? 1 };
      emit("submitted", `local:${r.queue_id}`);
    } else {
      okJobid.value = r.jobid!;
      emit("submitted", r.jobid!);
    }
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div class="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-6" @click.self="emit('close')">
    <div class="bg-white rounded-xl shadow-xl w-full max-w-lg flex flex-col max-h-[88vh]">
      <div class="flex items-center px-5 py-3 border-b border-slate-200">
        <div class="font-medium text-slate-800">提交任务</div>
        <button class="ml-auto p-1.5 rounded hover:bg-slate-100 text-slate-500" @click="emit('close')">
          <X :size="18" />
        </button>
      </div>

      <div class="p-5 overflow-auto space-y-4">
        <div v-if="loading" class="py-8 flex items-center justify-center text-slate-400 gap-2">
          <Loader2 :size="18" class="animate-spin" /> 加载中…
        </div>

        <!-- 提交成功：已直接进入 PBS -->
        <div v-else-if="okJobid" class="py-8 flex flex-col items-center gap-3 text-center">
          <CheckCircle2 :size="40" class="text-emerald-500" />
          <div class="text-slate-700">已提交，作业号</div>
          <div class="font-mono text-lg text-slate-800">{{ okJobid }}</div>
          <div class="text-xs text-slate-400">可在「任务」列表查看（轮询稍后收录）</div>
          <button class="mt-2 px-4 py-1.5 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-700" @click="emit('close')">完成</button>
        </div>

        <!-- 本地排队中：受并发配额或核数余量限制，暂未进入 PBS -->
        <div v-else-if="queuedInfo" class="py-8 flex flex-col items-center gap-3 text-center">
          <Loader2 :size="40" class="text-amber-500" />
          <div class="text-slate-700">已加入本地排队</div>
          <div class="text-xs text-slate-400">
            当前并发已达配额上限或集群核数暂不充裕，有空位会自动提交，无需重新操作
          </div>
          <div class="text-xs text-slate-400">可在「任务」列表查看排队状态，也可随时取消</div>
          <button class="mt-2 px-4 py-1.5 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-700" @click="emit('close')">完成</button>
        </div>

        <!-- 试算已启动：在管理节点直接运行 -->
        <div v-else-if="trialOk" class="py-8 flex flex-col items-center gap-3 text-center">
          <FlaskConical :size="40" class="text-indigo-500" />
          <div class="text-slate-700">试算已启动（T{{ trialOk.id }}）</div>
          <div class="text-xs text-slate-400">
            正在管理节点直接运行，可在「任务」列表实时查看输出，并随时中断
          </div>
          <button class="mt-2 px-4 py-1.5 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-700" @click="emit('submitted', `trial:${trialOk.id}`); emit('close')">完成</button>
        </div>

        <template v-else>
          <!-- 任务类型：常规(PBS) / 试算(管理节点直跑) -->
          <div class="flex gap-2">
            <button
              type="button"
              class="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 text-sm rounded-md border transition"
              :class="!isTrial ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-slate-600 border-slate-300 hover:bg-slate-50'"
              @click="taskType = 'pbs'"
            >
              <Rocket :size="15" /> 常规作业
            </button>
            <button
              type="button"
              class="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 text-sm rounded-md border transition"
              :class="isTrial ? 'bg-indigo-600 text-white border-indigo-600' : 'bg-white text-slate-600 border-slate-300 hover:bg-slate-50'"
              @click="taskType = 'trial'"
            >
              <FlaskConical :size="15" /> 试算
            </button>
          </div>
          <p v-if="isTrial" class="text-xs text-indigo-600 -mt-1">
            试算不进 PBS 队列，直接在管理节点运行所选试算模板的命令，适合快速验证。
          </p>

          <div class="grid gap-3" :class="isTrial ? 'grid-cols-1' : 'grid-cols-2'">
            <label class="text-sm">
              <span class="text-slate-500">名称 <span class="text-rose-500">*</span></span>
              <input v-model="name" class="mt-1 w-full px-2 py-1.5 border border-slate-300 rounded-md" placeholder="任务名" />
            </label>
            <label v-if="!isTrial" class="text-sm">
              <span class="text-slate-500">核数 <span class="text-rose-500">*</span></span>
              <input v-model.number="cores" type="number" min="1" class="mt-1 w-full px-2 py-1.5 border border-slate-300 rounded-md" />
            </label>
          </div>

          <label v-if="!isTrial" class="text-sm block">
            <span class="text-slate-500">队列</span>
            <input v-model="queue" class="mt-1 w-full px-2 py-1.5 border border-slate-300 rounded-md" placeholder="batch" />
          </label>

          <label class="text-sm block">
            <span class="text-slate-500">初始路径 <span class="text-rose-500">*</span></span>
            <input v-model="initDir" class="mt-1 w-full px-2 py-1.5 border border-slate-300 rounded-md font-mono text-xs" />
          </label>

          <div class="text-sm">
            <div class="flex items-baseline">
              <span class="text-slate-500">输入文件（K 文件）<span class="text-rose-500">*</span></span>
              <span v-if="kfiles.length" class="ml-2 text-xs text-slate-400">找到 {{ kfiles.length }} 个</span>
              <button
                v-if="kfiles.length"
                type="button"
                class="ml-auto text-xs text-blue-600 hover:underline"
                @click="manualInput = !manualInput"
              >
                {{ manualInput ? "从列表选择" : "手动输入路径" }}
              </button>
            </div>
            <!-- 多个/有 .k 时下拉选择；手动模式或无匹配时文本输入 -->
            <select
              v-if="!manualInput && kfiles.length"
              v-model="inputFile"
              class="mt-1 w-full px-2 py-1.5 border border-slate-300 rounded-md font-mono text-xs"
            >
              <option value="" disabled>请选择 .k 文件</option>
              <option v-for="f in kfiles" :key="f" :value="f">{{ f }}</option>
            </select>
            <input
              v-else
              v-model="inputFile"
              class="mt-1 w-full px-2 py-1.5 border border-slate-300 rounded-md font-mono text-xs"
              placeholder="相对初始路径，如 CASE04/xxx/800_xxx.k"
            />
            <span v-if="!kfiles.length" class="text-xs text-amber-600">该目录(含子目录)未找到 .k/.key 文件，请手动填写相对路径</span>
          </div>

          <label class="text-sm block">
            <span class="text-slate-500">{{ isTrial ? "试算模板" : "模板" }} <span class="text-rose-500">*</span></span>
            <select v-model="templateId" class="mt-1 w-full px-2 py-1.5 border border-slate-300 rounded-md">
              <option v-if="!visibleTemplates.length" :value="null" disabled>
                {{ isTrial ? "无试算模板（请管理员在「模板管理」新建试算模板）" : "无模板（请管理员先在「模板管理」创建）" }}
              </option>
              <option v-for="t in visibleTemplates" :key="t.id" :value="t.id">{{ t.name }}</option>
            </select>
          </label>

          <p v-if="error" class="text-sm text-rose-600">{{ error }}</p>
        </template>
      </div>

      <div v-if="!loading && !okJobid && !queuedInfo && !trialOk" class="px-5 py-3 border-t border-slate-200 flex justify-end gap-2">
        <button class="px-3 py-1.5 text-sm rounded-md border border-slate-300 hover:bg-slate-50" @click="emit('close')">取消</button>
        <button
          class="flex items-center gap-1.5 px-4 py-1.5 text-sm rounded-md text-white disabled:opacity-60"
          :class="isTrial ? 'bg-indigo-600 hover:bg-indigo-700' : 'bg-blue-600 hover:bg-blue-700'"
          :disabled="submitting"
          @click="submit"
        >
          <Loader2 v-if="submitting" :size="15" class="animate-spin" />
          <FlaskConical v-else-if="isTrial" :size="15" />
          <Rocket v-else :size="15" />
          {{ isTrial ? "开始试算" : "提交作业" }}
        </button>
      </div>
    </div>
  </div>
</template>
