<script setup lang="ts">
/**
 * 几何轻量化产物（glTF/GLB）的网页预览。
 *
 * 用 three.js 原生 GLTFLoader —— 这正是契约里要求 vektor3d 输出 glTF/GLB 而非
 * 私有 .3dix 的原因：标准格式让 SDM 不必引入任何对方的解析实现。
 * 契约见 docs/vektor3d-geometry-capability-contract.md。
 */
import { onBeforeUnmount, onMounted, ref, shallowRef } from "vue";
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { simApi } from "@/api";
import type { SimGeometry } from "@/api/types";
import { Loader2, X } from "lucide-vue-next";

const props = defineProps<{ geometry: SimGeometry }>();
const emit = defineEmits<{ (e: "close"): void }>();

const host = ref<HTMLDivElement | null>(null);
const loading = ref(true);
const error = ref("");
const stats = ref<{ tris: number; objects: number } | null>(null);

// three 对象用 shallowRef：它们内部状态庞大，深层响应式代理会拖垮渲染性能
const renderer = shallowRef<THREE.WebGLRenderer | null>(null);
const controls = shallowRef<OrbitControls | null>(null);
let frame = 0;

function disposeScene(scene: THREE.Scene) {
  scene.traverse((o) => {
    const m = o as THREE.Mesh;
    if (m.geometry) m.geometry.dispose();
    const mat = m.material as THREE.Material | THREE.Material[] | undefined;
    if (Array.isArray(mat)) mat.forEach((x) => x.dispose());
    else mat?.dispose();
  });
}

onMounted(async () => {
  const el = host.value;
  if (!el) return;

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0xf8fafc);
  const camera = new THREE.PerspectiveCamera(45, el.clientWidth / el.clientHeight, 0.1, 1e6);
  const r = new THREE.WebGLRenderer({ antialias: true });
  r.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  r.setSize(el.clientWidth, el.clientHeight);
  el.appendChild(r.domElement);
  renderer.value = r;

  scene.add(new THREE.AmbientLight(0xffffff, 0.7));
  const dir = new THREE.DirectionalLight(0xffffff, 0.8);
  dir.position.set(1, 1, 1);
  scene.add(dir);

  const ctl = new OrbitControls(camera, r.domElement);
  ctl.enableDamping = true;
  controls.value = ctl;

  try {
    const gltf = await new GLTFLoader().loadAsync(simApi.lightweightUrl(props.geometry.id));
    scene.add(gltf.scene);

    // 按包围盒自动取景：CAD 模型尺度差异极大（毫米级零件到米级总成），
    // 固定相机位置必然要么看不见要么穿模。
    const box = new THREE.Box3().setFromObject(gltf.scene);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const radius = Math.max(size.x, size.y, size.z) || 1;
    camera.position.set(center.x + radius, center.y + radius * 0.8, center.z + radius * 1.6);
    camera.near = radius / 1000;
    camera.far = radius * 100;
    camera.updateProjectionMatrix();
    ctl.target.copy(center);
    ctl.update();

    let tris = 0;
    let objects = 0;
    gltf.scene.traverse((o) => {
      const m = o as THREE.Mesh;
      if (m.isMesh && m.geometry) {
        objects += 1;
        const idx = m.geometry.getIndex();
        tris += (idx ? idx.count : m.geometry.getAttribute("position")?.count ?? 0) / 3;
      }
    });
    stats.value = { tris: Math.round(tris), objects };
  } catch (e) {
    error.value = `加载失败：${e instanceof Error ? e.message : String(e)}`;
  } finally {
    loading.value = false;
  }

  const tick = () => {
    frame = requestAnimationFrame(tick);
    ctl.update();
    r.render(scene, camera);
  };
  tick();

  const onResize = () => {
    if (!host.value) return;
    camera.aspect = host.value.clientWidth / host.value.clientHeight;
    camera.updateProjectionMatrix();
    r.setSize(host.value.clientWidth, host.value.clientHeight);
  };
  window.addEventListener("resize", onResize);

  onBeforeUnmount(() => {
    window.removeEventListener("resize", onResize);
    cancelAnimationFrame(frame);
    ctl.dispose();
    disposeScene(scene);
    r.dispose();
    r.domElement.remove();
  });
});
</script>

<template>
  <div
    class="fixed inset-0 z-40 bg-black/40 flex items-center justify-center p-4"
    @click.self="emit('close')"
  >
    <div class="bg-white rounded-lg shadow-xl w-full max-w-5xl h-[80vh] flex flex-col">
      <div class="flex items-center gap-2 px-4 py-2.5 border-b border-slate-200">
        <span class="font-medium text-slate-800 text-sm">
          {{ geometry.source_file?.name ?? "几何预览" }}
        </span>
        <span class="text-xs text-slate-400">v{{ geometry.version_no }}</span>
        <span v-if="stats" class="text-xs text-slate-500">
          {{ stats.objects }} 个网格 · {{ stats.tris.toLocaleString() }} 三角面
        </span>
        <button
          class="ml-auto p-1 rounded hover:bg-slate-100 text-slate-500"
          @click="emit('close')"
        >
          <X :size="18" />
        </button>
      </div>

      <div class="relative flex-1 min-h-0">
        <div ref="host" class="absolute inset-0"></div>
        <div
          v-if="loading"
          class="absolute inset-0 flex items-center justify-center gap-2 text-slate-500 text-sm bg-slate-50"
        >
          <Loader2 :size="18" class="animate-spin" /> 加载模型…
        </div>
        <div
          v-if="error"
          class="absolute inset-0 flex items-center justify-center text-rose-600 text-sm bg-slate-50 px-6 text-center"
        >
          {{ error }}
        </div>
      </div>
    </div>
  </div>
</template>
