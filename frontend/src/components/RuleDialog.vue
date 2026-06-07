<script setup lang="ts">
import { ref } from "vue";
import { api, errMsg } from "@/api";
import type { ExtractRule, ExtractRuleInput } from "@/api/types";
import { X, Loader2, FileCog } from "lucide-vue-next";

// rule 为空表示新建；否则编辑。
const props = defineProps<{ rule?: ExtractRule | null }>();
const emit = defineEmits<{ close: []; saved: [] }>();

const form = ref<ExtractRuleInput>({
  name: props.rule?.name ?? "",
  command: props.rule?.command ?? "",
  enabled: props.rule?.enabled ?? true,
  priority: props.rule?.priority ?? 100,
});

const submitting = ref(false);
const error = ref("");

async function submit() {
  if (!form.value.name.trim() || !form.value.command.trim()) {
    error.value = "规则名称与命令不能为空";
    return;
  }
  error.value = "";
  submitting.value = true;
  try {
    if (props.rule) {
      await api.updateRule(props.rule.id, form.value);
    } else {
      await api.createRule(form.value);
    }
    emit("saved");
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    submitting.value = false;
  }
}

const placeholders = [
  "{workdir}",
  "{jobid}",
  "{short_id}",
  "{name}",
  "{owner}",
  "{queue}",
];

function insertPlaceholder(ph: string) {
  form.value.command = `${form.value.command}${ph}`;
}
</script>

<template>
  <div
    class="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-6"
    @click.self="emit('close')"
  >
    <div
      class="bg-white rounded-xl shadow-xl w-full max-w-lg flex flex-col max-h-[90vh]"
    >
      <div class="flex items-center px-5 py-3 border-b border-slate-200">
        <FileCog :size="18" class="text-blue-600 mr-2" />
        <div class="font-medium text-slate-800">
          {{ rule ? "编辑后处理工具" : "新建后处理工具" }}
        </div>
        <button
          class="ml-auto p-1.5 rounded hover:bg-slate-100 text-slate-500"
          @click="emit('close')"
        >
          <X :size="18" />
        </button>
      </div>

      <div class="p-5 flex flex-col gap-4 overflow-auto">
        <label class="block">
          <span class="text-sm text-slate-600">规则名称</span>
          <input
            v-model="form.name"
            type="text"
            placeholder="如：CFD 结果提取"
            class="mt-1 w-full px-3 py-2 rounded-md border border-slate-300 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none text-sm"
          />
        </label>

        <label class="block">
          <span class="text-sm text-slate-600">提取命令</span>
          <textarea
            v-model="form.command"
            rows="2"
            placeholder="如：/opt/tools/extract.sh {workdir} {jobid}"
            class="mt-1 w-full px-3 py-2 rounded-md border border-slate-300 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none text-sm font-mono"
          ></textarea>
          <div class="mt-1.5 flex flex-wrap gap-1">
            <button
              v-for="ph in placeholders"
              :key="ph"
              type="button"
              class="px-1.5 py-0.5 rounded text-xs font-mono bg-slate-100 text-slate-600 hover:bg-blue-100 hover:text-blue-700"
              @click="insertPlaceholder(ph)"
            >
              {{ ph }}
            </button>
          </div>
          <p class="mt-1 text-xs text-slate-400">
            以任务属主身份在工作目录中执行；按 shell 词法分词但不经过 shell。
          </p>
        </label>

        <p
          class="text-xs text-slate-500 bg-slate-50 border border-slate-100 rounded-md px-3 py-2"
        >
          该规则会在<span class="font-medium text-slate-600">所有任务</span
          >完成后自动执行。
        </p>

        <div class="flex items-center gap-5 border-t border-slate-100 pt-3">
          <label class="flex items-center gap-2 text-sm text-slate-600">
            <input v-model="form.enabled" type="checkbox" /> 启用
          </label>
          <label class="flex items-center gap-2 text-sm text-slate-600">
            优先级
            <input
              v-model.number="form.priority"
              type="number"
              class="w-20 px-2 py-1 rounded-md border border-slate-300 outline-none text-sm"
            />
            <span class="text-xs text-slate-400">越小越先执行</span>
          </label>
        </div>

        <p v-if="error" class="text-sm text-rose-600">{{ error }}</p>
      </div>

      <div class="px-5 py-3 border-t border-slate-200 flex justify-end gap-2">
        <button
          class="px-4 py-2 text-sm rounded-md border border-slate-300 hover:bg-slate-50"
          @click="emit('close')"
        >
          取消
        </button>
        <button
          class="px-4 py-2 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-60 flex items-center gap-2"
          :disabled="submitting"
          @click="submit"
        >
          <Loader2 v-if="submitting" :size="15" class="animate-spin" />
          保存
        </button>
      </div>
    </div>
  </div>
</template>
