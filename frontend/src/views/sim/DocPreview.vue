<script setup lang="ts">
/**
 * 需求文档在线预览。
 *
 * 用 @open-file-viewer（MIT，框架无关内核 + Vue 适配器），覆盖 pptx/docx/xlsx/pdf 等。
 * 两个刻意的处理：
 *
 * **① 懒加载。** 它的依赖树很重（mermaid、leaflet、ag-psd、hls.js…），静态引入会
 * 把主包撑大几 MB，而绝大多数人进项目页并不会点预览。这里用动态 import，
 * 只在真正打开预览时才拉这一坨。
 *
 * **② 不走 CDN。** 集群是内网，任何远程资源都取不到。这个库的依赖全是 npm 包、
 * 随构建打进 dist，因此可用；将来若换成需要联网取字体/worker 的组件，会直接白屏。
 */
import { onBeforeUnmount, onMounted, ref, shallowRef } from "vue";
import { Loader2, X } from "lucide-vue-next";

defineProps<{ url: string; fileName: string }>();
const emit = defineEmits<{ (e: "close"): void }>();

const loading = ref(true);
const error = ref("");
// 组件实例用 shallowRef：它内部状态庞大，深层响应式代理只会拖慢渲染
const Viewer = shallowRef<unknown>(null);
const plugins = shallowRef<unknown[]>([]);

// 弹窗打开期间锁住 body 滚动。只加 overflow-auto 还不够：鼠标移到弹窗以外
// （遮罩上）时滚轮仍会滚动背后的长列表，视觉上像是预览"跳"了一下。
onMounted(() => {
  const prev = document.body.style.overflow;
  document.body.style.overflow = "hidden";
  onBeforeUnmount(() => {
    document.body.style.overflow = prev;
  });
});

onMounted(async () => {
  try {
    // 插件必须显式注册——不传 plugins 的话组件能挂上但什么都渲染不出来。
    // 只注册需求文档用得到的三类，顺带把 mermaid/leaflet/hls 这些无关依赖挡在包外。
    // style.css 必须显式引入：库不会随 JS 自动注入样式，缺了它工具栏图标和
    // loading 圆环会以裸 HTML 原始尺寸撑满整页，看起来像文档多了一页乱码。
    const [vueMod, core] = await Promise.all([
      import("@open-file-viewer/vue"),
      import("@open-file-viewer/core"),
      import("@open-file-viewer/core/style.css"),
    ]);
    plugins.value = [
      core.officePlugin(),   // .pptx/.docx/.xlsx —— 技术协议基本都是这几种
      core.pdfPlugin(),
      core.textPlugin(),
      core.fallbackPlugin(), // 认不出的格式给一句人话 + 下载入口，而不是空白
    ];
    Viewer.value = vueMod.OpenFileViewer;
  } catch (e) {
    // 依赖没装或构建没打进来时如实说明，而不是留一个空白框让人以为文件坏了
    error.value = `预览组件加载失败：${e instanceof Error ? e.message : String(e)}`;
  } finally {
    loading.value = false;
  }
});
</script>

<template>
  <div
    class="fixed inset-0 z-40 bg-black/40 flex items-center justify-center p-4"
    @click.self="emit('close')"
  >
    <div class="bg-white rounded-lg shadow-xl w-full max-w-6xl h-[88vh] flex flex-col">
      <div class="flex items-center gap-2 px-4 py-2.5 border-b border-slate-200">
        <span class="font-medium text-slate-800 text-sm truncate">{{ fileName }}</span>
        <a
          :href="url"
          class="text-xs text-blue-600 hover:underline shrink-0"
          title="预览不支持的格式可下载后本地打开"
        >下载原件</a>
        <button class="ml-auto p-1 rounded hover:bg-slate-100 text-slate-500" @click="emit('close')">
          <X :size="18" />
        </button>
      </div>

      <!-- overflow-auto 是必需的：没有它，内容溢出后滚轮事件会一路冒泡到背后的
           页面上——表现就是"滚不动预览、反而把项目页滚走了"。 -->
      <div class="relative flex-1 min-h-0 overflow-auto bg-slate-50" @wheel.stop>
        <div v-if="loading" class="absolute inset-0 flex items-center justify-center gap-2 text-slate-500 text-sm">
          <Loader2 :size="18" class="animate-spin" /> 正在加载预览组件…
        </div>
        <div v-else-if="error" class="absolute inset-0 flex flex-col items-center justify-center gap-2 text-sm px-6 text-center">
          <span class="text-rose-600">{{ error }}</span>
          <a :href="url" class="text-blue-600 hover:underline">下载原件在本地打开</a>
        </div>
        <component
          v-else-if="Viewer"
          :is="Viewer"
          :file="url"
          :file-name="fileName"
          :plugins="plugins"
          :toolbar="true"
          locale="zh-CN"
          width="100%"
          height="100%"
        />
      </div>
    </div>
  </div>
</template>
