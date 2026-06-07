<script setup lang="ts">
import { ref, computed, watch, onMounted } from "vue";
import { useRouter } from "vue-router";
import { api, errMsg } from "@/api";
import type { FsEntry } from "@/api/types";
import { fmtBytes, fmtTime } from "@/lib/format";
import PreviewModal from "@/components/PreviewModal.vue";
import PackageDialog from "@/components/PackageDialog.vue";
import SubmitJobModal from "@/components/SubmitJobModal.vue";
import {
  Folder,
  FileText,
  Link2,
  ChevronRight,
  Download,
  Eye,
  Loader2,
  RefreshCw,
  PackageIcon,
  FolderPlus,
  FolderUp,
  Upload,
  Check,
  X,
  Rocket,
  Star,
  Pencil,
  Trash2,
  Pause,
  Play,
} from "lucide-vue-next";
import {
  startDownload,
  CancelledError,
  type DownloadControls,
} from "@/lib/downloader";

const props = defineProps<{
  // 初始目录；为空时让后端用第一个白名单根。
  initialPath: string;
  // 是否允许跳出 initialPath 之上（任务详情限定在 workdir 内，文件页可在根间切换）。
  rootLock?: boolean;
  // 归档命名提示（通常为作业名），打包对话框据此预填默认归档名。
  nameHint?: string;
}>();

const router = useRouter();
const path = ref(props.initialPath);
const roots = ref<string[]>([]);
const entries = ref<FsEntry[]>([]);
const loading = ref(false);
const error = ref("");

// 选中待打包的绝对路径集合（跨目录保留）。
const selected = ref<Set<string>>(new Set());
const previewPath = ref<string | null>(null);
const showPackage = ref(false);
const showSubmit = ref(false);
const downloading = ref<string | null>(null);
const downloadPct = ref(0); // 当前下载进度百分比(总大小已知时)
const downloadRate = ref(0); // 当前下载速率(B/s)
const downloadLoaded = ref(0); // 已下载字节(打包时总大小未知，用它展示)
const dlControls = ref<DownloadControls | null>(null); // 当前下载的控制句柄
const dlPaused = ref(false); // 是否已暂停

// 目录收藏（按账号，仅非 rootLock 的文件页显示）
const favorites = ref<string[]>([]);
const isFav = computed(() => favorites.value.includes(path.value));
async function loadFavorites() {
  try {
    favorites.value = await api.listFavorites();
  } catch {
    /* 忽略 */
  }
}
async function toggleFav() {
  try {
    if (isFav.value) await api.removeFavorite(path.value);
    else await api.addFavorite(path.value);
    await loadFavorites();
  } catch (e) {
    error.value = errMsg(e);
  }
}
async function removeFav(p: string) {
  try {
    await api.removeFavorite(p);
    await loadFavorites();
  } catch (e) {
    error.value = errMsg(e);
  }
}
function favName(p: string): string {
  return p.split("/").filter(Boolean).pop() || p;
}

// 新建目录 / 上传
const creatingDir = ref(false);
const newDirName = ref("");
const savingDir = ref(false);
const uploading = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);
const dirInput = ref<HTMLInputElement | null>(null);
// 上传进度状态
const uploadName = ref("");
const uploadPercent = ref(0); // 0-100
const uploadLoaded = ref(0); // 已传字节
const uploadTotal = ref(0); // 总字节
const uploadSpeed = ref(0); // 瞬时速率，字节/秒
const uploadFileIdx = ref(0); // 目录上传：当前第几个文件
const uploadFileCount = ref(0); // 目录上传：文件总数（>0 表示在传目录）

function joinPath(name: string): string {
  const base = path.value.replace(/\/+$/, "");
  return `${base}/${name}`;
}

async function load(p?: string) {
  loading.value = true;
  error.value = "";
  try {
    const resp = await api.listDir(p ?? path.value);
    path.value = resp.path;
    roots.value = resp.roots;
    // 目录在前，名称排序。
    entries.value = resp.entries.slice().sort((a, b) => {
      if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    lastIndex.value = null; // 切目录后重置 shift 区间锚点
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}

function enter(entry: FsEntry) {
  if (entry.is_dir) load(joinPath(entry.name));
}

// 某路径是否可作为导航目标：必须落在某个白名单根之内（含根本身），
// rootLock 时进一步限制不得越过 initialPath。越界的祖先段渲染为不可点击。
function navigable(p: string): boolean {
  if (props.rootLock) {
    const base = props.initialPath.replace(/\/+$/, "");
    return p === base || p.startsWith(base + "/");
  }
  if (!roots.value.length) return true; // 根列表未知前不拦截
  return roots.value.some((r) => p === r || p.startsWith(r + "/"));
}

// 面包屑：把当前路径拆成段，越界的祖先段不可点击。
const crumbs = computed(() => {
  const segs = path.value.split("/").filter(Boolean);
  const out: { label: string; path: string }[] = [];
  let acc = "";
  for (const s of segs) {
    acc += "/" + s;
    out.push({ label: s, path: navigable(acc) ? acc : "" });
  }
  return out;
});

// 按条件批量勾选当前目录的文件（全部已选则再点取消）
const H3D_RE = /\.h3d$/i;
function selectByPredicate(pred: (e: FsEntry) => boolean, label: string) {
  const matches = entries.value.filter((e) => !e.is_dir && pred(e));
  if (!matches.length) {
    error.value = `当前目录没有 ${label} 文件`;
    return;
  }
  const paths = matches.map((e) => joinPath(e.name));
  const allSel = paths.every((p) => selected.value.has(p));
  const next = new Set(selected.value);
  paths.forEach((p) => (allSel ? next.delete(p) : next.add(p)));
  selected.value = next;
}
// mes* ：以 mes 开头的文件（mes0000、mes0001 …）
const MES_RE = /^mes/i;
function selectH3d() {
  selectByPredicate((e) => H3D_RE.test(e.name), "h3d");
}
function selectD3plot() {
  selectByPredicate((e) => D3PLOT_RE.test(e.name), "d3plot");
}
function selectMes() {
  selectByPredicate((e) => MES_RE.test(e.name), "mes");
}
function clearSelection() {
  selected.value = new Set();
  lastIndex.value = null;
}

function toggle(entry: FsEntry) {
  const full = joinPath(entry.name);
  const next = new Set(selected.value);
  if (next.has(full)) next.delete(full);
  else next.add(full);
  selected.value = next;
}

// 行勾选：支持 shift 选中区间（上次点击行 ↔ 本次行 之间全部选中）
const lastIndex = ref<number | null>(null);
const rowShift = ref(false); // 点击时是否按住 Shift（@click 先于 @change 触发时捕获）
function onRowChange(entry: FsEntry, index: number) {
  if (rowShift.value && lastIndex.value !== null) {
    const a = Math.min(lastIndex.value, index);
    const b = Math.max(lastIndex.value, index);
    const next = new Set(selected.value);
    for (let i = a; i <= b; i++) {
      const e = entries.value[i];
      if (e) next.add(joinPath(e.name));
    }
    selected.value = next;
  } else {
    toggle(entry);
  }
  lastIndex.value = index;
}

function isSelected(entry: FsEntry): boolean {
  return selected.value.has(joinPath(entry.name));
}

// 页面内下载会把整个文件先放进浏览器内存：超过此阈值先提醒，避免吃满内存。
const BIG_DOWNLOAD = 4 * 1024 * 1024 * 1024; // 4 GB

function confirmBig(bytes: number, label: string): boolean {
  if (bytes <= BIG_DOWNLOAD) return true;
  const gb = (bytes / 1073741824).toFixed(1);
  return window.confirm(
    `「${label}」约 ${gb} GB，页面内下载会先占用同等浏览器内存，可能导致卡顿或失败。\n是否继续？`
  );
}

function resetDownload() {
  downloading.value = null;
  downloadPct.value = 0;
  downloadRate.value = 0;
  downloadLoaded.value = 0;
  dlControls.value = null;
  dlPaused.value = false;
}

// 统一发起一次可控下载（单文件或打包），并把进度接到浮层。
async function runDownload(
  label: string,
  opts: { path?: string; archivePaths?: string[]; archiveName?: string }
) {
  if (dlControls.value) return; // 同一时刻仅允许一个下载
  downloading.value = label;
  downloadPct.value = 0;
  downloadRate.value = 0;
  downloadLoaded.value = 0;
  dlPaused.value = false;
  const ctl = startDownload({
    ...opts,
    onProgress: (s) => {
      downloadPct.value = s.total ? Math.round((s.loaded / s.total) * 100) : 0;
      downloadRate.value = s.rate;
      downloadLoaded.value = s.loaded;
      dlPaused.value = s.paused;
    },
  });
  dlControls.value = ctl;
  try {
    await ctl.promise;
  } catch (e) {
    if (!(e instanceof CancelledError)) error.value = errMsg(e);
  } finally {
    resetDownload();
  }
}

function togglePause() {
  const c = dlControls.value;
  if (!c || !c.resumable) return;
  if (dlPaused.value) c.resume();
  else c.pause();
  dlPaused.value = !dlPaused.value;
}

function cancelDownload() {
  dlControls.value?.cancel();
}

async function download(entry: FsEntry) {
  if (!confirmBig(entry.size, entry.name)) return;
  await runDownload(entry.name, { path: joinPath(entry.name) });
}

// 本地打包下载目标：勾选了就打包选中项；未勾选则打包当前整个目录。
const packagePaths = computed<string[]>(() =>
  selected.value.size ? Array.from(selected.value) : [path.value]
);

// 打包 d3plot 到网盘：只取当前目录的 d3plot 家族文件(d3plot / d3plot01 / d3plotaa ...)
const D3PLOT_RE = /^d3plot(\d+|[a-z]+)?$/i;
const d3plotPaths = computed<string[]>(() =>
  entries.value.filter((e) => !e.is_dir && D3PLOT_RE.test(e.name)).map((e) => joinPath(e.name))
);
const pkgPaths = ref<string[]>([]); // 实际传给 PackageDialog 的路径
// 网盘打包按钮：有手动勾选→打包选中项；否则→当前目录的 d3plot 家族
const netdiskCount = computed(() =>
  selected.value.size ? selected.value.size : d3plotPaths.value.length
);
const netdiskLabel = computed(() =>
  selected.value.size
    ? `打包选中文件到网盘 (${selected.value.size})`
    : `打包d3plot到网盘${d3plotPaths.value.length ? ` (${d3plotPaths.value.length})` : ""}`
);
function openNetdiskPackage() {
  if (selected.value.size) {
    pkgPaths.value = Array.from(selected.value); // 用户手动选的(可含非 d3plot)
  } else if (d3plotPaths.value.length) {
    pkgPaths.value = d3plotPaths.value; // 默认:当前目录 d3plot
  } else {
    error.value = "请勾选要打包的文件，或在含 d3plot 的目录使用";
    return;
  }
  showPackage.value = true;
}

// 提交作业默认输入文件：当前目录里已勾选的文件（优先 .k/.key），取文件名（相对当前目录）
const submitInput = computed(() => {
  const sel = entries.value.filter((e) => !e.is_dir && isSelected(e));
  const k = sel.find((e) => /\.(k|key)$/i.test(e.name)) || sel[0];
  return k ? k.name : "";
});

// 打包下载到本地（不走网盘），统一走页面内 XHR(快链路)+ 进度浮层。
const arcLoading = ref(false);
async function downloadArchive() {
  const ps = packagePaths.value;
  // 单个“文件”不打包：直接下载该文件(带进度)。
  if (ps.length === 1) {
    const name = ps[0].split("/").filter(Boolean).pop();
    const ent = entries.value.find((e) => e.name === name);
    if (ent && !ent.is_dir) {
      await download(ent);
      return;
    }
  }
  // 多个文件 / 目录：流式 tar 打包，边打边传。
  const base = (props.nameHint || path.value.split("/").filter(Boolean).pop() || "download")
    .replace(/[^\w.\-]+/g, "_");
  // 估算总量用于超大提示(选中项大小之和；目录无法预知，给出已知部分)
  const known = entries.value
    .filter((e) => ps.includes(joinPath(e.name)) && !e.is_dir)
    .reduce((s, e) => s + e.size, 0);
  if (!confirmBig(known, `${base}.tar`)) return;
  arcLoading.value = true;
  try {
    await runDownload(`${base}.tar`, { archivePaths: ps, archiveName: base });
  } finally {
    arcLoading.value = false;
  }
}

function onPackaged(taskId: string) {
  showPackage.value = false;
  selected.value = new Set();
  router.push({ name: "tasks", query: { focus: taskId } });
}

function startCreateDir() {
  creatingDir.value = true;
  newDirName.value = "";
}
function cancelCreateDir() {
  creatingDir.value = false;
  newDirName.value = "";
}
async function confirmCreateDir() {
  const name = newDirName.value.trim();
  if (!name) return;
  savingDir.value = true;
  error.value = "";
  try {
    await api.makeDir(path.value, name);
    cancelCreateDir();
    await load();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    savingDir.value = false;
  }
}

// 行内重命名
const renaming = ref<string | null>(null);
const renameValue = ref("");
const renameSaving = ref(false);
function startRename(entry: FsEntry) {
  renaming.value = entry.name;
  renameValue.value = entry.name;
}
function cancelRename() {
  renaming.value = null;
  renameValue.value = "";
}
async function confirmRename(entry: FsEntry) {
  const nn = renameValue.value.trim();
  if (!nn || nn === entry.name) {
    cancelRename();
    return;
  }
  renameSaving.value = true;
  error.value = "";
  try {
    await api.rename(joinPath(entry.name), nn);
    cancelRename();
    await load();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    renameSaving.value = false;
  }
}

// 删除文件/目录
const deleting = ref<string | null>(null);
async function removeEntry(entry: FsEntry) {
  const what = entry.is_dir ? "目录" : "文件";
  if (!window.confirm(`确定删除${what}「${entry.name}」？${entry.is_dir ? "目录及其全部内容将被删除，" : ""}操作不可恢复。`))
    return;
  deleting.value = entry.name;
  error.value = "";
  try {
    await api.deletePath(joinPath(entry.name));
    const full = joinPath(entry.name);
    if (selected.value.has(full)) {
      const next = new Set(selected.value);
      next.delete(full);
      selected.value = next;
    }
    await load();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    deleting.value = null;
  }
}

// 批量删除选中项（并发上限 4）
const batchDeleting = ref(false);
async function removeSelected() {
  const paths = Array.from(selected.value);
  if (!paths.length) return;
  if (
    !window.confirm(
      `确定删除选中的 ${paths.length} 项？目录将连同内容一并删除，操作不可恢复。`
    )
  )
    return;
  batchDeleting.value = true;
  error.value = "";
  const errs: string[] = [];
  let i = 0;
  const worker = async () => {
    while (i < paths.length) {
      const p = paths[i++];
      try {
        await api.deletePath(p);
      } catch (e) {
        errs.push(`${p.split("/").pop()}: ${errMsg(e)}`);
      }
    }
  };
  await Promise.all(Array.from({ length: 4 }, () => worker()));
  selected.value = new Set();
  await load();
  batchDeleting.value = false;
  if (errs.length) {
    error.value = `部分删除失败(${errs.length})：${errs.slice(0, 3).join("；")}${errs.length > 3 ? " …" : ""}`;
  }
}

function pickFile() {
  fileInput.value?.click();
}
async function onFilePicked(e: Event) {
  const input = e.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = ""; // 允许重复选择同名文件
  if (!file) return;
  uploading.value = true;
  error.value = "";
  // 初始化进度状态
  uploadName.value = file.name;
  uploadPercent.value = 0;
  uploadLoaded.value = 0;
  uploadTotal.value = file.size;
  uploadSpeed.value = 0;
  // 速率计算：基于相邻进度回调之间的字节增量与时间差，做轻度平滑
  let lastTime = performance.now();
  let lastLoaded = 0;
  try {
    await api.uploadFile(path.value, file, (loaded, total) => {
      uploadLoaded.value = loaded;
      uploadTotal.value = total || file.size;
      uploadPercent.value = uploadTotal.value
        ? Math.min(100, Math.round((loaded / uploadTotal.value) * 100))
        : 0;
      const now = performance.now();
      const dt = (now - lastTime) / 1000;
      if (dt >= 0.2) {
        const inst = (loaded - lastLoaded) / dt; // 字节/秒
        // 指数平滑，避免数字跳动
        uploadSpeed.value = uploadSpeed.value
          ? uploadSpeed.value * 0.6 + inst * 0.4
          : inst;
        lastTime = now;
        lastLoaded = loaded;
      }
    });
    uploadPercent.value = 100;
    await load();
  } catch (err) {
    error.value = errMsg(err);
  } finally {
    uploading.value = false;
    uploadSpeed.value = 0;
  }
}

function pickDir() {
  dirInput.value?.click();
}
// 上传整个目录：逐个文件上传并用 webkitRelativePath 保留层级，进度按总字节聚合
async function onDirPicked(e: Event) {
  const input = e.target as HTMLInputElement;
  const files = Array.from(input.files ?? []);
  input.value = "";
  if (!files.length) return;
  const totalBytes = files.reduce((s, f) => s + f.size, 0);
  uploading.value = true;
  error.value = "";
  uploadFileCount.value = files.length;
  uploadFileIdx.value = 0;
  uploadTotal.value = totalBytes;
  uploadLoaded.value = 0;
  uploadPercent.value = 0;
  uploadSpeed.value = 0;
  let doneBytes = 0; // 已完成文件累计字节
  let lastTime = performance.now();
  let lastLoaded = 0;
  try {
    for (let i = 0; i < files.length; i++) {
      const f = files[i];
      uploadFileIdx.value = i + 1;
      const rel = f.webkitRelativePath || f.name; // "目录名/子目录/文件"
      uploadName.value = rel;
      await api.uploadFile(
        path.value,
        f,
        (loaded) => {
          const overall = doneBytes + loaded;
          uploadLoaded.value = overall;
          uploadPercent.value = totalBytes
            ? Math.min(100, Math.round((overall / totalBytes) * 100))
            : 0;
          const now = performance.now();
          const dt = (now - lastTime) / 1000;
          if (dt >= 0.2) {
            const inst = (overall - lastLoaded) / dt;
            uploadSpeed.value = uploadSpeed.value
              ? uploadSpeed.value * 0.6 + inst * 0.4
              : inst;
            lastTime = now;
            lastLoaded = overall;
          }
        },
        rel
      );
      doneBytes += f.size;
    }
    uploadPercent.value = 100;
    await load();
  } catch (err) {
    error.value = errMsg(err);
  } finally {
    uploading.value = false;
    uploadSpeed.value = 0;
    uploadFileCount.value = 0;
    uploadFileIdx.value = 0;
  }
}

watch(
  () => props.initialPath,
  (p) => load(p)
);
onMounted(() => {
  load();
  if (!props.rootLock) loadFavorites();
});

defineExpose({ reload: () => load() });
</script>

<template>
  <div>
    <!-- 下载进度浮层(页面内 XHR 下载，实时显示进度与速率) -->
    <div
      v-if="downloading"
      class="fixed bottom-4 right-4 z-50 w-72 rounded-xl border border-slate-200 bg-white shadow-lg p-3"
    >
      <div class="flex items-center justify-between text-xs text-slate-600 mb-1.5">
        <span class="truncate max-w-[8rem] font-medium">{{ downloading }}</span>
        <div class="flex items-center gap-1.5">
          <span class="tabular-nums text-blue-600 font-semibold">
            {{
              dlPaused
                ? "已暂停"
                : downloadRate
                  ? (downloadRate / 1048576).toFixed(2) + " MB/s"
                  : "…"
            }}
          </span>
          <button
            v-if="dlControls?.resumable"
            class="p-1 rounded hover:bg-slate-100 text-slate-500"
            :title="dlPaused ? '继续' : '暂停'"
            @click="togglePause"
          >
            <Play v-if="dlPaused" :size="14" />
            <Pause v-else :size="14" />
          </button>
          <button
            class="p-1 rounded hover:bg-rose-50 text-slate-400 hover:text-rose-600"
            title="取消"
            @click="cancelDownload"
          >
            <X :size="14" />
          </button>
        </div>
      </div>
      <div class="h-1.5 rounded-full bg-slate-100 overflow-hidden">
        <div
          v-if="downloadPct"
          class="h-full transition-all"
          :class="dlPaused ? 'bg-slate-400' : 'bg-blue-500'"
          :style="{ width: downloadPct + '%' }"
        />
        <div
          v-else
          class="h-full w-1/3"
          :class="dlPaused ? 'bg-slate-300' : 'bg-blue-400 animate-pulse'"
        />
      </div>
      <div class="text-[11px] text-slate-400 mt-1 tabular-nums">
        <span v-if="downloadPct">{{ downloadPct }}%</span>
        <span v-else>已下载 {{ (downloadLoaded / 1048576).toFixed(1) }} MB</span>
        （页面内下载）
      </div>
    </div>

    <!-- 工具栏：面包屑 + 操作 -->
    <div class="flex items-center gap-2 mb-3 flex-wrap">
      <div class="flex items-center text-sm text-slate-600 flex-wrap">
        <button
          v-for="(c, i) in crumbs"
          :key="i"
          class="flex items-center"
          :class="c.path ? 'hover:text-blue-600' : 'text-slate-400 cursor-default'"
          :disabled="!c.path"
          @click="c.path && load(c.path)"
        >
          <ChevronRight v-if="i > 0" :size="14" class="text-slate-300" />
          <span class="px-1">{{ c.label }}</span>
        </button>
      </div>
      <button
        v-if="!rootLock"
        class="p-1 rounded hover:bg-amber-50"
        :title="isFav ? '取消收藏当前目录' : '收藏当前目录'"
        @click="toggleFav"
      >
        <Star :size="17" :class="isFav ? 'fill-amber-400 text-amber-400' : 'text-slate-400'" />
      </button>
      <div class="w-full md:w-auto md:ml-auto flex flex-wrap items-center gap-2">
        <button
          class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-slate-300 bg-white hover:bg-slate-50"
          title="勾选当前目录所有 .h3d 文件(再点取消)"
          @click="selectH3d"
        >
          <Check :size="15" /> 选择h3d
        </button>
        <button
          class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-slate-300 bg-white hover:bg-slate-50"
          title="勾选当前目录所有 d3plot 文件(再点取消)"
          @click="selectD3plot"
        >
          <Check :size="15" /> 选择d3plot
        </button>
        <button
          class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-slate-300 bg-white hover:bg-slate-50"
          title="勾选当前目录所有 mes* 文件(再点取消)"
          @click="selectMes"
        >
          <Check :size="15" /> 选择mes
        </button>
        <button
          v-if="selected.size"
          class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-slate-300 bg-white text-slate-600 hover:bg-slate-50"
          title="清除所有已勾选项"
          @click="clearSelection"
        >
          <X :size="15" /> 清除选择 ({{ selected.size }})
        </button>
        <button
          class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-slate-300 bg-white hover:bg-slate-50 disabled:opacity-60"
          :disabled="uploading"
          @click="pickFile"
        >
          <Loader2 v-if="uploading" :size="15" class="animate-spin" />
          <Upload v-else :size="15" /> 上传文件
        </button>
        <button
          class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-slate-300 bg-white hover:bg-slate-50 disabled:opacity-60"
          :disabled="uploading"
          title="上传整个目录（保留子目录层级）"
          @click="pickDir"
        >
          <Loader2 v-if="uploading" :size="15" class="animate-spin" />
          <FolderUp v-else :size="15" /> 上传目录
        </button>
        <button
          class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-slate-300 bg-white hover:bg-slate-50"
          @click="startCreateDir"
        >
          <FolderPlus :size="15" /> 新建目录
        </button>
        <button
          class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-slate-300 bg-white hover:bg-slate-50 disabled:opacity-60"
          :disabled="arcLoading"
          title="打包成 zip 直接下载到本地"
          @click="downloadArchive"
        >
          <Loader2 v-if="arcLoading" :size="15" class="animate-spin" />
          <Download v-else :size="15" />
          {{ selected.size ? `打包下载 (${selected.size})` : "打包下载" }}
        </button>
        <button
          v-if="selected.size"
          class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-rose-300 text-rose-600 bg-white hover:bg-rose-50 disabled:opacity-60"
          :disabled="batchDeleting"
          title="删除选中的文件/目录"
          @click="removeSelected"
        >
          <Loader2 v-if="batchDeleting" :size="15" class="animate-spin" />
          <Trash2 v-else :size="15" />
          批量删除 ({{ selected.size }})
        </button>
        <button
          class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
          :title="selected.size ? '打包选中的文件并上传网盘' : '打包当前目录的 d3plot 结果文件并上传网盘'"
          :disabled="!netdiskCount"
          @click="openNetdiskPackage"
        >
          <PackageIcon :size="15" /> {{ netdiskLabel }}
        </button>
        <button
          class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-emerald-500 text-emerald-600 bg-white hover:bg-emerald-50"
          title="用模板把当前目录的 K 文件提交为作业"
          @click="showSubmit = true"
        >
          <Rocket :size="15" /> 提交作业
        </button>
        <button
          class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-slate-300 bg-white hover:bg-slate-50"
          :disabled="loading"
          @click="load()"
        >
          <RefreshCw :size="15" :class="{ 'animate-spin': loading }" /> 刷新
        </button>
        <input
          ref="fileInput"
          type="file"
          class="hidden"
          @change="onFilePicked"
        />
        <input
          ref="dirInput"
          type="file"
          class="hidden"
          webkitdirectory
          multiple
          @change="onDirPicked"
        />
      </div>
    </div>

    <!-- 收藏的常用目录：点击跳转，× 取消收藏 -->
    <div v-if="!rootLock && favorites.length" class="flex items-center gap-1.5 mb-3 flex-wrap">
      <Star :size="14" class="fill-amber-400 text-amber-400 shrink-0" />
      <span
        v-for="f in favorites"
        :key="f"
        class="group flex items-center gap-1 pl-2 pr-1 py-0.5 rounded-full bg-amber-50 border border-amber-200 text-xs text-slate-700"
      >
        <button class="hover:text-blue-600" :title="f" @click="load(f)">{{ favName(f) }}</button>
        <button class="text-slate-400 hover:text-rose-500" title="取消收藏" @click="removeFav(f)">
          <X :size="12" />
        </button>
      </span>
    </div>

    <!-- 上传进度条 + 速率 -->
    <div
      v-if="uploading"
      class="flex items-center gap-3 mb-3 p-2.5 rounded-lg bg-blue-50 border border-blue-200"
    >
      <component
        :is="uploadFileCount ? FolderUp : Upload"
        :size="16"
        class="text-blue-600 shrink-0"
      />
      <span
        v-if="uploadFileCount"
        class="text-xs font-medium text-blue-700 tabular-nums shrink-0"
        >文件 {{ uploadFileIdx }}/{{ uploadFileCount }}</span
      >
      <span
        class="text-sm text-slate-700 truncate max-w-[14rem]"
        :title="uploadName"
        >{{ uploadName }}</span
      >
      <div class="flex-1 h-2 rounded-full bg-blue-100 overflow-hidden">
        <div
          class="h-full bg-blue-600 transition-all duration-150"
          :style="{ width: uploadPercent + '%' }"
        ></div>
      </div>
      <span
        class="text-xs font-medium text-blue-700 tabular-nums shrink-0 w-10 text-right"
        >{{ uploadPercent }}%</span
      >
      <span class="text-xs text-slate-500 tabular-nums shrink-0 w-44 text-right">
        {{ fmtBytes(uploadLoaded) }} / {{ fmtBytes(uploadTotal) }}
        <span v-if="uploadSpeed > 0">· {{ fmtBytes(uploadSpeed) }}/s</span>
      </span>
    </div>

    <!-- 新建目录内联输入 -->
    <div
      v-if="creatingDir"
      class="flex items-center gap-2 mb-3 p-2 rounded-lg bg-slate-50 border border-slate-200"
    >
      <FolderPlus :size="16" class="text-amber-500 ml-1" />
      <input
        v-model="newDirName"
        type="text"
        placeholder="新目录名称"
        class="flex-1 px-2.5 py-1.5 rounded-md border border-slate-300 outline-none text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
        @keyup.enter="confirmCreateDir"
        @keyup.esc="cancelCreateDir"
      />
      <button
        class="flex items-center gap-1 px-3 py-1.5 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-60"
        :disabled="savingDir || !newDirName.trim()"
        @click="confirmCreateDir"
      >
        <Loader2 v-if="savingDir" :size="14" class="animate-spin" />
        <Check v-else :size="14" /> 创建
      </button>
      <button
        class="flex items-center gap-1 px-2.5 py-1.5 text-sm rounded-md border border-slate-300 bg-white hover:bg-slate-50"
        @click="cancelCreateDir"
      >
        <X :size="14" />
      </button>
    </div>

    <p v-if="error" class="text-sm text-rose-600 mb-3">{{ error }}</p>

    <div class="bg-white rounded-xl border border-slate-200 overflow-hidden">
      <table class="w-full text-sm">
        <thead class="bg-slate-50 text-slate-500 text-left">
          <tr>
            <th class="px-3 py-2.5 w-9"></th>
            <th class="px-3 py-2.5 font-medium">名称</th>
            <th class="px-3 py-2.5 font-medium w-28">大小</th>
            <th class="px-3 py-2.5 font-medium w-44">修改时间</th>
            <th class="px-3 py-2.5 font-medium w-28">权限</th>
            <th class="px-3 py-2.5 font-medium w-28 text-right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="(entry, index) in entries"
            :key="entry.name"
            class="border-t border-slate-100 hover:bg-slate-50"
          >
            <td class="px-3 py-2 text-center">
              <input
                type="checkbox"
                :checked="isSelected(entry)"
                title="按住 Shift 点击可选中区间"
                @click="rowShift = $event.shiftKey"
                @change="onRowChange(entry, index)"
              />
            </td>
            <td class="px-3 py-2">
              <div v-if="renaming === entry.name" class="flex items-center gap-1">
                <input
                  v-model="renameValue"
                  class="px-2 py-1 text-sm border border-slate-300 rounded outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 w-64"
                  @keyup.enter="confirmRename(entry)"
                  @keyup.esc="cancelRename"
                />
                <button class="p-1 rounded hover:bg-slate-200 text-emerald-600 disabled:opacity-60" :disabled="renameSaving" title="确认" @click="confirmRename(entry)">
                  <Loader2 v-if="renameSaving" :size="15" class="animate-spin" /><Check v-else :size="15" />
                </button>
                <button class="p-1 rounded hover:bg-slate-200 text-slate-500" title="取消" @click="cancelRename">
                  <X :size="15" />
                </button>
              </div>
              <button
                v-else
                class="flex items-center gap-2 text-left"
                :class="entry.is_dir ? 'text-slate-800 hover:text-blue-600' : 'text-slate-700'"
                @click="enter(entry)"
              >
                <Folder v-if="entry.is_dir" :size="16" class="text-amber-500" />
                <Link2 v-else-if="entry.is_link" :size="16" class="text-sky-500" />
                <FileText v-else :size="16" class="text-slate-400" />
                {{ entry.name }}
              </button>
            </td>
            <td class="px-3 py-2 text-slate-500">
              {{ entry.is_dir ? "—" : fmtBytes(entry.size) }}
            </td>
            <td class="px-3 py-2 text-slate-500">{{ fmtTime(entry.mtime) }}</td>
            <td class="px-3 py-2 font-mono text-xs text-slate-400">
              {{ entry.mode }}
            </td>
            <td class="px-3 py-2">
              <div class="flex items-center justify-end gap-1">
                <button
                  class="p-1.5 rounded hover:bg-slate-200 text-slate-500"
                  title="重命名"
                  @click="startRename(entry)"
                >
                  <Pencil :size="15" />
                </button>
                <button
                  v-if="!entry.is_dir"
                  class="p-1.5 rounded hover:bg-slate-200 text-slate-500"
                  title="预览"
                  @click="previewPath = joinPath(entry.name)"
                >
                  <Eye :size="15" />
                </button>
                <button
                  v-if="!entry.is_dir"
                  class="p-1.5 rounded hover:bg-slate-200 text-slate-500"
                  title="下载"
                  :disabled="downloading === entry.name"
                  @click="download(entry)"
                >
                  <Loader2
                    v-if="downloading === entry.name"
                    :size="15"
                    class="animate-spin"
                  />
                  <Download v-else :size="15" />
                </button>
                <button
                  class="p-1.5 rounded hover:bg-rose-50 text-slate-500 hover:text-rose-600 disabled:opacity-60"
                  title="删除"
                  :disabled="deleting === entry.name"
                  @click="removeEntry(entry)"
                >
                  <Loader2 v-if="deleting === entry.name" :size="15" class="animate-spin" />
                  <Trash2 v-else :size="15" />
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>

      <div
        v-if="loading"
        class="py-12 flex items-center justify-center text-slate-400 gap-2"
      >
        <Loader2 :size="18" class="animate-spin" /> 加载中…
      </div>
      <div
        v-else-if="!entries.length"
        class="py-12 text-center text-slate-400"
      >
        空目录
      </div>
    </div>

    <PreviewModal
      v-if="previewPath"
      :path="previewPath"
      @close="previewPath = null"
    />
    <PackageDialog
      v-if="showPackage"
      :paths="pkgPaths"
      :name-hint="(nameHint || path.split('/').filter(Boolean).pop() || 'd3plot') + '_d3plot'"
      @close="showPackage = false"
      @done="onPackaged"
    />
    <SubmitJobModal
      v-if="showSubmit"
      :init-dir="path"
      :input-file="submitInput"
      @close="showSubmit = false"
      @submitted="showSubmit = false"
    />
  </div>
</template>
