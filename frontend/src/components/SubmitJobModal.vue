<script setup lang="ts">
import { ref, onMounted } from "vue";
import { api, errMsg } from "@/api";
import type { JobTemplate } from "@/api/types";
import { X, Loader2, Rocket, CheckCircle2 } from "lucide-vue-next";

const props = defineProps<{ initDir: string; inputFile?: string }>();
const emit = defineEmits<{ close: []; submitted: [jobid: string] }>();

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

onMounted(async () => {
  try {
    templates.value = await api.listTemplates();
    if (templates.value.length) templateId.value = templates.value[0].id;
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
  if (!name.value.trim()) return (error.value = "请填写作业名称");
  if (!cores.value || cores.value < 1) return (error.value = "核数无效");
  if (!inputFile.value.trim()) return (error.value = "请填写输入文件（相对初始路径）");
  if (templateId.value === null) return (error.value = "请选择模板");
  submitting.value = true;
  try {
    const r = await api.submitJob({
      name: name.value.trim(),
      cores: Number(cores.value),
      queue: queue.value.trim() || "batch",
      init_dir: initDir.value,
      input_file: inputFile.value.trim(),
      template_id: templateId.value,
    });
    okJobid.value = r.jobid;
    emit("submitted", r.jobid);
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
        <div class="font-medium text-slate-800">提交作业</div>
        <button class="ml-auto p-1.5 rounded hover:bg-slate-100 text-slate-500" @click="emit('close')">
          <X :size="18" />
        </button>
      </div>

      <div class="p-5 overflow-auto space-y-4">
        <div v-if="loading" class="py-8 flex items-center justify-center text-slate-400 gap-2">
          <Loader2 :size="18" class="animate-spin" /> 加载中…
        </div>

        <!-- 提交成功 -->
        <div v-else-if="okJobid" class="py-8 flex flex-col items-center gap-3 text-center">
          <CheckCircle2 :size="40" class="text-emerald-500" />
          <div class="text-slate-700">已提交，作业号</div>
          <div class="font-mono text-lg text-slate-800">{{ okJobid }}</div>
          <div class="text-xs text-slate-400">可在「任务」列表查看（轮询稍后收录）</div>
          <button class="mt-2 px-4 py-1.5 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-700" @click="emit('close')">完成</button>
        </div>

        <template v-else>
          <div class="grid grid-cols-2 gap-3">
            <label class="text-sm">
              <span class="text-slate-500">名称 <span class="text-rose-500">*</span></span>
              <input v-model="name" class="mt-1 w-full px-2 py-1.5 border border-slate-300 rounded-md" placeholder="作业名" />
            </label>
            <label class="text-sm">
              <span class="text-slate-500">核数 <span class="text-rose-500">*</span></span>
              <input v-model.number="cores" type="number" min="1" class="mt-1 w-full px-2 py-1.5 border border-slate-300 rounded-md" />
            </label>
          </div>

          <label class="text-sm block">
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
            <span class="text-slate-500">模板 <span class="text-rose-500">*</span></span>
            <select v-model="templateId" class="mt-1 w-full px-2 py-1.5 border border-slate-300 rounded-md">
              <option v-if="!templates.length" :value="null" disabled>无模板（请管理员先在「模板管理」创建）</option>
              <option v-for="t in templates" :key="t.id" :value="t.id">{{ t.name }}</option>
            </select>
          </label>

          <p v-if="error" class="text-sm text-rose-600">{{ error }}</p>
        </template>
      </div>

      <div v-if="!loading && !okJobid" class="px-5 py-3 border-t border-slate-200 flex justify-end gap-2">
        <button class="px-3 py-1.5 text-sm rounded-md border border-slate-300 hover:bg-slate-50" @click="emit('close')">取消</button>
        <button
          class="flex items-center gap-1.5 px-4 py-1.5 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-60"
          :disabled="submitting"
          @click="submit"
        >
          <Loader2 v-if="submitting" :size="15" class="animate-spin" /><Rocket v-else :size="15" /> 提交作业
        </button>
      </div>
    </div>
  </div>
</template>
