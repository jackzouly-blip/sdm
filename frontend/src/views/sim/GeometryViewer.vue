<script setup lang="ts">
/**
 * 几何轻量化产物（glTF/GLB）的网页预览。
 *
 * 用 three.js 原生 GLTFLoader —— 这正是契约里要求 vektor3d 输出 glTF/GLB 而非
 * 私有 .3dix 的原因：标准格式让 SDM 不必引入任何对方的解析实现。
 * 契约见 docs/vektor3d-geometry-capability-contract.md。
 *
 * 渲染口径对齐 vektor3d 的 3D 工作台（useViewer.ts）：同样的三灯布置、Phong
 * 高光、灰度归一化与折角边线。CAD 件与产品外观件不同——它多是单色金属，纯靠
 * 漫反射会糊成一团灰，**结构全靠边线和高光读出来**，所以这几项不是美化，
 * 而是可读性。
 *
 * 边线是**加载后算的**（EdgesGeometry 按折角阈值），不来自 glTF：
 * 工作台也是这么做的。这意味着任何一个合规 GLB 都能有边线，
 * 不必要求 vektor3d 在产物里额外塞线段。
 */
import { computed, onBeforeUnmount, onMounted, ref, shallowRef } from "vue";
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { simApi } from "@/api";
import type { SimGeometry } from "@/api/types";
import { Loader2, Spline, X } from "lucide-vue-next";

// ── 与 vektor3d 3D 工作台一致的渲染常量（useViewer.ts）──
/** 折角超过它才画边线：太小则圆角面上爬满碎线，太大则棱线丢失 */
const EDGE_CREASE_ANGLE_DEG = 35;
/** 纯灰零件的明暗兜底色：CAD 默认色多是 180/180/180，原样渲染就是一片死灰 */
const GRAYSCALE_DELTA = 4;
const LIGHT_COLOR_THRESHOLD = 250;
const DARK_COLOR_THRESHOLD = 8;
const LIGHT_FALLBACK: [number, number, number] = [236, 240, 246];
const DARK_FALLBACK: [number, number, number] = [76, 82, 92];
/** 边线上限：EdgesGeometry 要遍历全部三角面，整车装配上会卡死主线程 */
const EDGE_TRIANGLE_BUDGET = 1_500_000;

function normalizeBodyColor(c: THREE.Color): THREE.Color {
  const r = c.r * 255, g = c.g * 255, b = c.b * 255;
  const maxv = Math.max(r, g, b);
  const minv = Math.min(r, g, b);
  if (maxv - minv > GRAYSCALE_DELTA) return c;         // 有彩度，保留原色
  if (minv >= LIGHT_COLOR_THRESHOLD) return new THREE.Color(...LIGHT_FALLBACK.map((v) => v / 255) as [number, number, number]);
  if (maxv <= DARK_COLOR_THRESHOLD) return new THREE.Color(...DARK_FALLBACK.map((v) => v / 255) as [number, number, number]);
  return c;
}

/**
 * src / title 可选：网格预览复用同一个查看器（vektor3d 回传的网格 GLB 带真实
 * 单元边线，渲染要求与几何 GLB 完全一样，没有理由再写一个）。不传就按几何版本
 * 的轻量化产物加载，保持既有调用方零改动。
 */
const props = defineProps<{ geometry: SimGeometry; src?: string; title?: string }>();
const emit = defineEmits<{ (e: "close"): void }>();

const host = ref<HTMLDivElement | null>(null);
const loading = ref(true);
const error = ref("");
/**
 * 零件导航。**直接从 GLB 的 node.extras 生成**——气囊预览把每个识别出的件
 * 单独成 node 并带上 kind/bbox，所以这里不需要另开接口或加库字段。
 *
 * 它的价值不只是"看得方便"：预览列出的件与「生成网格」将要建的那些片一一
 * 对应，认少了、认错了在这里一眼看得出来，不必先花几十秒建完再发现。
 */
interface NavPart {
  name: string;
  kind: string;
  /** 归并键（如"安装位3"）。同一安装位上的固定带与扎带实物叠在一起，列一处 */
  group?: string;
  segments: number;
  obj: THREE.Object3D;
  box: THREE.Box3;
}
const navParts = shallowRef<NavPart[]>([]);
const activePart = ref<string | null>(null);
const KIND_LABEL: Record<string, string> = {
  single: "单层区(囊袋支撑层)", nangdai: "囊袋", chamber: "腔体", diffuser: "导流袋",
  fix: "固定带", tie: "扎带(仿真忽略)", named: "点名件",
  carrier: "固定件", tether: "拉带", band: "缝线带", other: "其它",
};
/** 认件口径（可能多于画出来的：窄缝线带的边界曲线常被邻近大区吸走） */
const identified = ref<Record<string, number>>({});

function collectNav(root: THREE.Object3D) {
  const out: NavPart[] = [];
  root.traverse((o) => {
    const ex = o.userData as Record<string, unknown> | undefined;
    if (!ex || ex.source !== "airbag-flat" || !ex.partId) return;
    const bb = ex.bbox as number[] | undefined;
    const box = bb && bb.length === 6
      ? new THREE.Box3(new THREE.Vector3(bb[0], bb[1], bb[2]),
                       new THREE.Vector3(bb[3], bb[4], bb[5]))
      : new THREE.Box3().setFromObject(o);
    out.push({
      name: String(ex.partId), kind: String(ex.kind ?? "other"),
      group: ex.group ? String(ex.group) : undefined,
      segments: Number(ex.segments ?? 0), obj: o, box,
    });
  });
  navParts.value = out;
}

/** 点导航：把相机对准该件，并把其它件压暗（不隐藏——要看它在整图里的位置） */
function focusPart(p: NavPart | null) {
  const cam = cameraRef.value;
  const ctl = controls.value;
  activePart.value = p ? p.name : null;
  for (const q of navParts.value) {
    // 压暗而不隐藏：要能看出这个件在整张图里的位置
    const dim = !!p && q.name !== p.name;
    q.obj.traverse((o) => {
      const mat = (o as THREE.Mesh).material;
      const list = Array.isArray(mat) ? mat : mat ? [mat] : [];
      for (const mt of list) {
        mt.transparent = dim;
        mt.opacity = dim ? 0.12 : 1.0;
        mt.depthWrite = !dim;
        mt.needsUpdate = true;
      }
    });
  }
  if (!p || !cam || !ctl) return;
  const size = p.box.getSize(new THREE.Vector3());
  const center = p.box.getCenter(new THREE.Vector3());
  // 件可能是一条极扁的窄带，用最大维定视距；下限避免贴到脸上
  const radius = Math.max(size.x, size.y, size.z, 20);
  cam.position.set(center.x, center.y - radius * 0.15, center.z + radius * 2.2);
  cam.updateProjectionMatrix();
  ctl.target.copy(center);
  ctl.update();
}

/** 分组后的导航列表：主件在前，同类聚在一起；带 group 的按归并键成组 */
const navGroups = computed(() => {
  const order = ["single", "nangdai", "chamber", "diffuser", "fix", "tie",
                 "named", "carrier", "tether", "band", "other"];
  // 归并键优先于类别：固定带3 与扎带3 是同一安装位上叠着的两件，列一处才好看。
  // 键由 GLB 的 extras.group 给死，不从件名里猜序号。
  const by = new Map<string, NavPart[]>();
  for (const p of navParts.value) {
    const key = p.group ?? p.kind;
    if (!by.has(key)) by.set(key, []);
    by.get(key)!.push(p);
  }
  // order 只定次序，**不当白名单**：不认识的键追加在后面。早先按 order 过滤，
  // Python 侧把类名从 nangdai/carrier/band 改成 single/fix/tie 之后，15 个件
  // (单层区 + 固定带 7 + 扎带 7)渲染出来了却整个从导航里消失，只剩 5 件可点。
  const rank = (k: string) => {
    const parts = by.get(k)!;
    // 归并组按其中排得最靠前的那件定位；同名组之间再按键排，安装位才按序
    const i = Math.min(...parts.map((p) => {
      const j = order.indexOf(p.kind);
      return j < 0 ? order.length : j;
    }));
    return i;
  };
  return [...by.keys()]
    .sort((a, b) => rank(a) - rank(b) || a.localeCompare(b, "zh"))
    .map((k) => ({
      kind: k,
      label: KIND_LABEL[k] ?? k,
      parts: by.get(k)!,
      /** 认出来但没画出来的差额，如实标出 */
      hidden: Math.max(0, (identified.value[k] ?? 0) - by.get(k)!.length),
    }));
});

const stats = ref<{
  tris: number;
  /** 线段数。平面展开图这类纯线框模型三角面为 0，读数要落在这里 */
  segs: number;
  objects: number;
  /** 装配层级深度；1 表示被拍平了 */
  depth: number;
  /** 带稳定零件标识的 node 数 */
  withPartId: number;
  /** 去重后的 mesh 数；远小于 objects 说明相同零件复用生效 */
  uniqueMeshes: number;
} | null>(null);

// three 对象用 shallowRef：它们内部状态庞大，深层响应式代理会拖垮渲染性能
/** 三角面超预算时不画边线——如实标出来,而不是让人以为模型本来就没棱线 */
const edgesSkipped = ref(false);
const renderer = shallowRef<THREE.WebGLRenderer | null>(null);
const controls = shallowRef<OrbitControls | null>(null);
// 相机原本是 onMounted 里的局部变量；导航要聚焦到某个件，得能在外面拿到它
const cameraRef = shallowRef<THREE.PerspectiveCamera | null>(null);
let frame = 0;

function makeGradientBackground(top: number, bottom: number): THREE.CanvasTexture {
  const canvas = document.createElement("canvas");
  canvas.width = 2;
  canvas.height = 256;
  const ctx = canvas.getContext("2d")!;
  const g = ctx.createLinearGradient(0, 0, 0, canvas.height);
  g.addColorStop(0, `#${top.toString(16).padStart(6, "0")}`);
  g.addColorStop(1, `#${bottom.toString(16).padStart(6, "0")}`);
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

/**
 * 把 GLTFLoader 建出来的场景改成工作台的观感：Phong 高光材质 + 折角边线。
 *
 * 为什么要换材质:glTF 规范用 PBR(MeshStandardMaterial),而 PBR 的金属/粗糙度
 * 在**没有环境贴图**的场景里几乎不产生高光——CAD 单色件因此糊成一坨。工作台用
 * MeshPhongMaterial 配一点冷色高光,正是为了让曲面起伏读得出来。
 *
 * polygonOffset 是给边线让位的:面与线同深度会 z-fighting,边线会一段段闪。
 */
function applyWorkbenchLook(root: THREE.Object3D): { edges: number; skipped: boolean } {
  let triangles = 0;
  const meshes: THREE.Mesh[] = [];
  root.traverse((o) => {
    const m = o as THREE.Mesh;
    if (!m.isMesh || !m.geometry) return;
    meshes.push(m);
    const idx = m.geometry.getIndex();
    triangles += (idx ? idx.count : m.geometry.getAttribute("position")?.count ?? 0) / 3;
  });

  for (const mesh of meshes) {
    const src = Array.isArray(mesh.material) ? mesh.material[0] : mesh.material;
    const base = (src as THREE.MeshStandardMaterial)?.color ?? new THREE.Color(0xb4b4b4);
    mesh.material = new THREE.MeshPhongMaterial({
      color: normalizeBodyColor(base.clone()),
      specular: new THREE.Color(0x444e5a),
      shininess: 100,
      side: THREE.DoubleSide,
      polygonOffset: true,
      polygonOffsetFactor: 0.5,
      polygonOffsetUnits: 1,
    });
    (src as THREE.Material)?.dispose?.();
  }

  // 边线开销与三角面数成正比,整车装配上会把主线程钉死几十秒 —— 超预算就不画,
  // 并如实告诉用户,而不是让他对着转不动的页面猜。
  if (triangles > EDGE_TRIANGLE_BUDGET) return { edges: 0, skipped: true };

  let edges = 0;
  for (const mesh of meshes) {
    const eg = new THREE.EdgesGeometry(mesh.geometry, EDGE_CREASE_ANGLE_DEG);
    if ((eg.getAttribute("position")?.count ?? 0) < 2) {
      eg.dispose();
      continue;
    }
    const line = new THREE.LineSegments(
      eg,
      new THREE.LineBasicMaterial({ color: 0x1a1a1a, transparent: true, opacity: 0.85, depthWrite: false })
    );
    line.renderOrder = 2;
    // 挂在 mesh 下:实例各自带自己的世界变换,挂到场景根会让复用的零件错位
    mesh.add(line);
    edges += 1;
  }
  return { edges, skipped: false };
}

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
  // 渐变背景而非纯白：纯白底上浅色零件的轮廓会被"洗掉"，深浅过渡才看得出体积
  scene.background = makeGradientBackground(0xf8fafc, 0xe7edf5);
  const camera = new THREE.PerspectiveCamera(45, el.clientWidth / el.clientHeight, 0.1, 1e6);
  cameraRef.value = camera;
  const r = new THREE.WebGLRenderer({ antialias: true });
  r.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  r.setSize(el.clientWidth, el.clientHeight);
  r.outputColorSpace = THREE.LinearSRGBColorSpace;  // 与工作台同口径，否则整体偏亮发白
  el.appendChild(r.domElement);
  renderer.value = r;

  // 三灯布置照抄工作台：主光 + 反向补光 + 半球光。
  // 强度乘 π 是 three r155+ 的光照单位换算——沿用旧数值会明显偏暗，
  // 截图里那种"灰扑扑一坨"正是这么来的。
  const sunlight = new THREE.DirectionalLight(0xffffff, 0.5 * Math.PI);
  sunlight.position.set(1, 1, -1);
  scene.add(sunlight);
  const bottomlight = new THREE.DirectionalLight(0xffffff, 0.5 * Math.PI);
  bottomlight.position.set(-1, -1, 1);
  scene.add(bottomlight);
  const hemiLight = new THREE.HemisphereLight(0xffffff, 0xffffff, 0.5 * Math.PI);
  hemiLight.position.set(-1, 1, 0);
  scene.add(hemiLight);

  const ctl = new OrbitControls(camera, r.domElement);
  ctl.enableDamping = true;
  controls.value = ctl;

  try {
    const gltf = await new GLTFLoader().loadAsync(
      props.src || simApi.lightweightUrl(props.geometry.id)
    );
    scene.add(gltf.scene);
    const look = applyWorkbenchLook(gltf.scene);
    edgesSkipped.value = look.skipped;

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

    collectNav(gltf.scene);

    // 统计同时充当契约验收读数：装配层级、零件标识、mesh 复用这三条要求
    // （见 docs/vektor3d-geometry-capability-contract.md 的装配小节）
    // 是否被满足，在这里一眼可见，不必等用久了才发现标识是随机生成的。
    let tris = 0;
    let segs = 0;
    let objects = 0;
    let depth = 0;
    let withPartId = 0;
    const meshIds = new Set<number>();
    gltf.scene.traverse((o) => {
      let d = 0;
      for (let p = o.parent; p; p = p.parent) d += 1;
      depth = Math.max(depth, d);
      if (o.userData?.partId || o.userData?.instancePath) withPartId += 1;
      const m = o as THREE.Mesh;
      if (m.isMesh && m.geometry) {
        objects += 1;
        // 同一 geometry 被多个 node 引用 => 复用生效，几何只存一份
        if (!meshIds.has(m.geometry.id)) {
          meshIds.add(m.geometry.id);
          const idx = m.geometry.getIndex();
          tris += (idx ? idx.count : m.geometry.getAttribute("position")?.count ?? 0) / 3;
        }
        return;
      }
      // 线框（气囊平面展开图的预览是 LINES）。GLTFLoader 对 mode=1 建的是
      // LineSegments，不是 Mesh —— 只数 isMesh 会显示"0 个对象"，看着像加载失败。
      const l = o as unknown as THREE.LineSegments;
      if (l.isLineSegments && l.geometry) {
        objects += 1;
        if (!meshIds.has(l.geometry.id)) {
          meshIds.add(l.geometry.id);
          const idx = l.geometry.getIndex();
          segs += (idx ? idx.count : l.geometry.getAttribute("position")?.count ?? 0) / 2;
        }
      }
    });
    stats.value = {
      tris: Math.round(tris),
      segs: Math.round(segs),
      objects,
      depth,
      withPartId,
      uniqueMeshes: meshIds.size,
    };
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
          {{ title ?? geometry.source_file?.name ?? "几何预览" }}
        </span>
        <span v-if="!title" class="text-xs text-slate-400">v{{ geometry.version_no }}</span>
        <span v-if="stats" class="text-xs text-slate-500">
          {{ stats.objects }} 个零件 ·
          <template v-if="stats.tris">{{ stats.tris.toLocaleString() }} 三角面</template>
          <template v-else-if="stats.segs">{{ stats.segs.toLocaleString() }} 线段（线框）</template>
          <template v-else>空</template>
          <template v-if="stats.uniqueMeshes < stats.objects">
            （复用后 {{ stats.uniqueMeshes }} 份几何）
          </template>
        </span>
        <span
          v-if="edgesSkipped"
          class="text-xs px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 inline-flex items-center gap-0.5"
          title="三角面过多，绘制折角边线会长时间卡住页面，已跳过。旋转查看仍不受影响。"
        >
          <Spline :size="11" /> 边线已跳过
        </span>
        <!-- 契约验收：层级被拍平、或零件缺标识，都会让后续的零件级操作做不了 -->
        <span
          v-if="stats && stats.depth <= 1 && stats.objects > 1"
          class="text-xs px-1.5 py-0.5 rounded bg-amber-50 text-amber-700"
          title="装配层级被拍平成一层，无法按子系统操作。见几何能力契约的装配小节。"
          >层级已拍平</span
        >
        <span
          v-if="stats && stats.objects > 1 && stats.withPartId === 0"
          class="text-xs px-1.5 py-0.5 rounded bg-amber-50 text-amber-700"
          title="零件没有稳定标识（node.extras.partId），无法按零件赋材料或圈定范围。见几何能力契约的装配小节。"
          >零件无标识</span
        >
        <button
          class="ml-auto p-1 rounded hover:bg-slate-100 text-slate-500"
          @click="emit('close')"
        >
          <X :size="18" />
        </button>
      </div>

      <div class="relative flex-1 min-h-0 flex">
        <!-- 零件导航：只有气囊平面图预览才有（node.extras.source=airbag-flat） -->
        <aside
          v-if="navParts.length"
          class="w-48 shrink-0 overflow-auto border-r border-slate-200 bg-slate-50/70 text-xs"
        >
          <button
            class="w-full px-3 py-1.5 text-left border-b border-slate-200 hover:bg-white"
            :class="activePart === null ? 'bg-white font-medium text-slate-800' : 'text-slate-500'"
            @click="focusPart(null)"
          >
            全部（{{ navParts.length }} 件）
          </button>
          <div v-for="g in navGroups" :key="g.kind" class="border-b border-slate-200 last:border-0">
            <div class="px-3 py-1 text-[11px] text-slate-400 flex items-center gap-1">
              {{ g.label }} · {{ g.parts.length }}
              <span
                v-if="g.hidden"
                class="text-amber-600"
                :title="`另有 ${g.hidden} 个已识别但预览里没画出来——窄件的边界曲线常被邻近大区吸走，建网格时仍会建`"
              >+{{ g.hidden }} 未绘出</span>
            </div>
            <button
              v-for="p in g.parts"
              :key="p.name"
              class="w-full px-3 py-1 text-left hover:bg-white flex items-center justify-between gap-2"
              :class="activePart === p.name ? 'bg-white text-slate-900 font-medium' : 'text-slate-600'"
              @click="focusPart(activePart === p.name ? null : p)"
            >
              <span class="truncate">{{ p.name }}</span>
              <span class="text-[10px] text-slate-400 shrink-0">{{ p.segments }}</span>
            </button>
          </div>
        </aside>

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
  </div>
</template>
