<script setup lang="ts">
import { ref, computed } from "vue";
import { api, errMsg } from "@/api";
import type { NetdiskShare, NetdiskShareInput, SharePreviewItem } from "@/api/types";
import { fmtBytes } from "@/lib/format";
import { X, Loader2, CloudDownload, Folder, FileIcon, CornerLeftUp, Check } from "lucide-vue-next";

// share 为空表示新建；否则编辑。
const props = defineProps<{ share?: NetdiskShare | null; inboxBase?: string }>();
const emit = defineEmits<{ close: []; saved: [] }>();

const form = ref<NetdiskShareInput>({
  name: props.share?.name ?? "",
  share_url: props.share?.share_url ?? "",
  pwd: props.share?.pwd ?? "",
  sub_dir: props.share?.sub_dir ?? "",
  local_dir: props.share?.local_dir ?? "",
  enabled: props.share?.enabled ?? true,
  poll_interval: props.share?.poll_interval ?? 0,
});

const submitting = ref(false);
const error = ref("");

// --- 链接校验 + 目录浏览 ---
// 先验后存：错链接当场就能发现，不会变成一个永远同步失败的源。
const verifying = ref(false);
const verified = ref(false);
const items = ref<SharePreviewItem[]>([]);
const browseDir = ref("");
const fileCount = ref(0);
const totalBytes = ref(0);

const dirs = computed(() => items.value.filter((i) => i.isdir));
const files = computed(() => items.value.filter((i) => !i.isdir));

async function verify(dir = "") {
  if (!form.value.share_url.trim()) {
    error.value = "请先填写分享链接";
    return;
  }
  error.value = "";
  verifying.value = true;
  try {
    const r = await api.previewShare({
      share_url: form.value.share_url.trim(),
      pwd: form.value.pwd.trim(),
      sub_dir: dir,
    });
    items.value = r.items;
    browseDir.value = dir;
    fileCount.value = r.file_count;
    totalBytes.value = r.total_bytes;
    verified.value = true;
  } catch (e) {
    verified.value = false;
    items.value = [];
    error.value = errMsg(e);
  } finally {
    verifying.value = false;
  }
}

function enterDir(item: SharePreviewItem) {
  void verify(item.path);
}

function goUp() {
  const parent = browseDir.value.replace(/\/[^/]*$/, "");
  void verify(parent);
}

/** 把当前浏览到的目录设为同步子目录。 */
function useCurrentDir() {
  form.value.sub_dir = browseDir.value;
}

// 同步策略：下拉的分钟数 ↔ 后端的秒
const intervalOptions = [
  { label: "仅手动同步", value: 0 },
  { label: "每 10 分钟", value: 600 },
  { label: "每 30 分钟", value: 1800 },
  { label: "每小时", value: 3600 },
  { label: "每 6 小时", value: 21600 },
  { label: "每天", value: 86400 },
];

const derivedLocalDir = computed(() => {
  if (form.value.local_dir.trim()) return form.value.local_dir.trim();
  if (!props.inboxBase || !form.value.name.trim()) return "";
  return `${props.inboxBase}/<你的用户名>/${form.value.name.trim()}`;
});

async function submit() {
  if (!form.value.name.trim()) {
    error.value = "请填写名称";
    return;
  }
  if (!form.value.share_url.trim()) {
    error.value = "请填写分享链接";
    return;
  }
  error.value = "";
  submitting.value = true;
  try {
    if (props.share) {
      await api.updateShare(props.share.id, form.value);
    } else {
      await api.createShare(form.value);
    }
    emit("saved");
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div
    class="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-6"
    @click.self="emit('close')"
  >
    <div class="bg-white rounded-xl shadow-xl w-full max-w-2xl flex flex-col max-h-[90vh]">
      <div class="flex items-center px-5 py-3 border-b border-slate-200">
        <CloudDownload :size="18" class="text-blue-600 mr-2" />
        <div class="font-medium text-slate-800">
          {{ share ? "编辑共享目录" : "添加共享目录" }}
        </div>
        <button
          class="ml-auto p-1.5 rounded hover:bg-slate-100 text-slate-500"
          @click="emit('close')"
        >
          <X :size="18" />
        </button>
      </div>

      <div class="p-5 flex flex-col gap-4 overflow-auto">
        <p class="text-xs text-slate-500 bg-blue-50 border border-blue-100 rounded-md px-3 py-2">
          请在百度网盘中把存放数据的文件夹<span class="font-medium">分享</span>出来（建议设置提取码、
          有效期选「永久」），然后把链接和提取码填在这里。后续你往该文件夹里新增的文件，平台同步时会自动识别。
        </p>

        <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <label class="block sm:col-span-2">
            <span class="text-sm text-slate-600">分享链接</span>
            <input
              v-model="form.share_url"
              type="text"
              placeholder="https://pan.baidu.com/s/1..."
              class="mt-1 w-full px-3 py-2 rounded-md border border-slate-300 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none text-sm"
            />
          </label>
          <label class="block">
            <span class="text-sm text-slate-600">提取码</span>
            <input
              v-model="form.pwd"
              type="text"
              placeholder="如 a1b2"
              class="mt-1 w-full px-3 py-2 rounded-md border border-slate-300 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none text-sm font-mono"
            />
          </label>
        </div>

        <div>
          <button
            type="button"
            class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-slate-300 bg-white hover:bg-slate-50 disabled:opacity-60"
            :disabled="verifying"
            @click="verify('')"
          >
            <Loader2 v-if="verifying" :size="15" class="animate-spin" />
            <Check v-else-if="verified" :size="15" class="text-emerald-600" />
            {{ verified ? "重新验证" : "验证链接并浏览" }}
          </button>
        </div>

        <!-- 目录浏览器：挑要同步的子目录 -->
        <div v-if="verified" class="border border-slate-200 rounded-lg overflow-hidden">
          <div class="flex items-center gap-2 px-3 py-2 bg-slate-50 border-b border-slate-200 text-xs">
            <button
              v-if="browseDir"
              class="p-1 rounded hover:bg-slate-200 text-slate-500"
              title="上一级"
              @click="goUp"
            >
              <CornerLeftUp :size="14" />
            </button>
            <span class="font-mono text-slate-600 truncate">
              {{ browseDir || "/（分享根目录）" }}
            </span>
            <span class="ml-auto text-slate-400 shrink-0">
              {{ fileCount }} 个文件 / {{ fmtBytes(totalBytes) }}
            </span>
            <button
              class="px-2 py-0.5 rounded text-xs shrink-0"
              :class="
                form.sub_dir === browseDir
                  ? 'bg-emerald-100 text-emerald-700'
                  : 'bg-blue-600 text-white hover:bg-blue-700'
              "
              @click="useCurrentDir"
            >
              {{ form.sub_dir === browseDir ? "已选为同步目录" : "同步此目录" }}
            </button>
          </div>
          <div class="max-h-48 overflow-auto divide-y divide-slate-100">
            <button
              v-for="d in dirs"
              :key="d.fs_id"
              class="w-full flex items-center gap-2 px-3 py-1.5 text-sm text-left hover:bg-slate-50"
              @click="enterDir(d)"
            >
              <Folder :size="15" class="text-amber-500 shrink-0" />
              <span class="truncate text-slate-700">{{ d.name }}</span>
            </button>
            <div
              v-for="f in files"
              :key="f.fs_id"
              class="flex items-center gap-2 px-3 py-1.5 text-sm text-slate-500"
            >
              <FileIcon :size="15" class="text-slate-300 shrink-0" />
              <span class="truncate">{{ f.name }}</span>
              <span class="ml-auto text-xs text-slate-400 shrink-0">
                {{ fmtBytes(f.size) }}
              </span>
            </div>
            <div v-if="!items.length" class="px-3 py-6 text-center text-sm text-slate-400">
              该目录为空
            </div>
          </div>
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <label class="block">
            <span class="text-sm text-slate-600">名称</span>
            <input
              v-model="form.name"
              type="text"
              placeholder="如：某某项目输入数据"
              class="mt-1 w-full px-3 py-2 rounded-md border border-slate-300 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none text-sm"
            />
          </label>
          <label class="block">
            <span class="text-sm text-slate-600">同步策略</span>
            <select
              v-model.number="form.poll_interval"
              class="mt-1 w-full px-3 py-2 rounded-md border border-slate-300 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none text-sm bg-white"
            >
              <option v-for="o in intervalOptions" :key="o.value" :value="o.value">
                {{ o.label }}
              </option>
            </select>
          </label>
        </div>

        <label class="block">
          <span class="text-sm text-slate-600">同步子目录</span>
          <input
            v-model="form.sub_dir"
            type="text"
            placeholder="留空 = 同步整个分享"
            class="mt-1 w-full px-3 py-2 rounded-md border border-slate-300 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none text-sm font-mono"
          />
        </label>

        <label class="block">
          <span class="text-sm text-slate-600">服务器落点</span>
          <input
            v-model="form.local_dir"
            type="text"
            placeholder="留空自动分配"
            class="mt-1 w-full px-3 py-2 rounded-md border border-slate-300 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none text-sm font-mono"
          />
          <p v-if="derivedLocalDir" class="mt-1 text-xs text-slate-400 font-mono truncate">
            {{ derivedLocalDir }}
          </p>
          <p class="mt-1 text-xs text-slate-400">
            同步下来的文件先落在这里，你可以在「文件浏览」里把它们移到自己的工作目录。
          </p>
        </label>

        <label class="flex items-center gap-2 text-sm text-slate-600 border-t border-slate-100 pt-3">
          <input v-model="form.enabled" type="checkbox" /> 启用
        </label>

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
