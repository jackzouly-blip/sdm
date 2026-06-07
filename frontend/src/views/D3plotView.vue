<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, reactive, computed } from "vue";
import { useRoute, useRouter } from "vue-router";
import * as THREE from "three";
import { TrackballControls } from "three/examples/jsm/controls/TrackballControls.js";
import { api, errMsg, pollTask } from "@/api";
import type { D3plotField, D3plotManifest } from "@/api/types";
import { ArrowLeft, Loader2, Play, Pause, SkipBack, SkipForward } from "lucide-vue-next";
import { fmtBytes } from "@/lib/format";
import CurveChart from "@/components/CurveChart.vue";

const route = useRoute();
const router = useRouter();
const canvas = ref<HTMLCanvasElement | null>(null);

const phase = ref<"choose" | "prep" | "parsing" | "loading" | "ready" | "error">("choose");
// 读取帧数选择：0=全部；越多越流畅但数据越大
const FRAME_OPTS = [
  { label: "全部", v: 0 }, { label: "60 帧", v: 60 }, { label: "40 帧", v: 40 },
  { label: "30 帧", v: 30 }, { label: "20 帧", v: 20 }, { label: "10 帧", v: 10 },
];
const maxStates = ref(40);
// 精度模式：preview=减面(快)，full=不减面/原始节点(准,大)
const precision = ref<"preview" | "full">("preview");
const statusMsg = ref("准备中…");
const progress = ref(0);            // 阶段一：服务器解析(数据分析)进度 %
const dlLoaded = ref(0);            // 阶段二：已下载字节
const dlTotal = ref(0);             // 下载总字节(gzip 分块时可能为 0)
const modelBytes = ref(0);          // model.bin 未压缩大小(估算下载总量用)
// 下载进度百分比：有 Content-Length 用真实值；gzip 无总量时按未压缩×0.7 估算
const dlPct = computed(() => {
  if (dlTotal.value > 0) return Math.round((dlLoaded.value / dlTotal.value) * 100);
  const est = modelBytes.value * 0.7;
  return est > 0 ? Math.min(99, Math.round((dlLoaded.value / est) * 100)) : 0;
});
const dlText = computed(() =>
  dlTotal.value > 0
    ? `${fmtBytes(dlLoaded.value)} / ${fmtBytes(dlTotal.value)}`
    : fmtBytes(dlLoaded.value)
);
const meta = ref("");
const playing = ref(true);
const frame = ref(0);
const nStates = ref(1);
const timeLabel = ref("");
const mode = ref<"part" | "region" | "field">("region");
const speed = ref(1);
const wire = ref(true);
// 剖切
const secOn = ref(false);
const secAxis = ref<"x" | "y" | "z">("z");
const secOffset = ref(0);
const secRotA = ref(0);
const secRotB = ref(0);
const secFlip = ref(false);
const secRange = ref(1);
const SEC_AXES = [
  { label: "XY", ax: "z" as const },
  { label: "XZ", ax: "y" as const },
  { label: "YZ", ax: "x" as const },
];
const fields = ref<D3plotField[]>([]);
const curField = ref(0);
const fieldBusy = ref(false); // 按需下载云图场中
const legend = reactive({ min: "0", mid: "", max: "", realMin: "", realMax: "", clamped: false });
const scale = ref(1);
const regions = ref<{ name: string; count: number; color: string; visible: boolean }[]>([]);
const picked = ref<{ part: number; title: string; region: string; field: string; value: string } | null>(null);
const refOn = ref(false);
const refTitle = ref("");
const refPart = ref<number | null>(null);
const savedFrames = ref<{ name: string; part: number }[]>([]);
const frameName = ref("");
// 测量：支持同时多条 —— 两点测距 / 单点位移
type Meas = {
  id: number; kind: "dist" | "disp"; name: string; color: string;
  v1: number; v2: number;            // dist: 两点；disp: 单点用 v1(v2=v1)
  data: number[]; min: number; max: number;  // 主标量(距离 / 合位移)随帧
  cx: number[]; cy: number[]; cz: number[];  // 仅 disp：XYZ 分量
  refTitle: string | null;           // 仅 disp：相对的固定零件(null=绝对位移)
};
const measType = ref<"dist" | "disp">("dist"); // 当前拾取类型
const measuring = ref(false);          // 正在拾取测量点
const measureA = ref<number | null>(null); // 测距已拾取的第一个点(顶点索引)
const measures = ref<Meas[]>([]);      // 已建立的测量列表(可多条)
let measSeq = 0;                       // 测量 id / 命名序号
const measLabels = ref<{ id: number; x: number; y: number; text: string; color: string }[]>([]); // 3D 内随动标签
const times = ref<number[] | null>(null);  // 各 state 的真实时间(若有)，作曲线 x 轴
const measureSeries = computed(() => measures.value.map((m) => ({ name: m.name, color: m.color, data: m.data })));
// 多测量配色(循环复用)
const MEAS_PALETTE = ["#22d3ee", "#f472b6", "#a3e635", "#fbbf24", "#60a5fa", "#fb923c", "#c084fc", "#34d399", "#e879f9", "#2dd4bf"];

function clearPick() {
  picked.value = null;
  if (V?.marker) V.marker.visible = false;
}

// 非响应式渲染状态
let V: any = null;
let stopPoll: (() => void) | null = null;
let raf = 0;

// 零件类别：有下划线取首段(假人 01HF_HEAD_…→01HF 整车假人一类、AY5-T_…→AY5-T)；
// 纯数字编号取前 4 位作系统大类(座椅 66000885/66001012→6600xxxx 并为一类)；否则用原名。
function groupOf(title: string): string {
  const t = (title || "").trim();
  if (t.includes("_")) return t.split("_")[0] || "OTHER";
  const m = /^(\d{4})\d*$/.exec(t);
  if (m) return m[1] + "xxxx";
  return t || "OTHER";
}
// 固定分类色板(Tableau 风格，~20 色)，按组循环复用，避免色类过多
const PALETTE_HEX = [
  "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f", "#edc948",
  "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac", "#86bcb6", "#d37295",
  "#a0cbe8", "#ffbe7d", "#8cd17d", "#b6992d", "#499894", "#fabfd2",
];
const PALETTE_RGB = PALETTE_HEX.map((h) => [
  parseInt(h.slice(1, 3), 16) / 255,
  parseInt(h.slice(3, 5), 16) / 255,
  parseInt(h.slice(5, 7), 16) / 255,
]);
function hsv(h: number, s: number, v: number) {
  const i = Math.floor(h * 6), f = h * 6 - i, p = v * (1 - s), q = v * (1 - f * s), t = v * (1 - (1 - f) * s);
  return [[v, t, p], [q, v, p], [p, v, t], [p, q, v], [t, p, v], [v, p, q]][i % 6];
}
function jet(t: number) {
  t = Math.min(1, Math.max(0, t));
  const s = [[0.23, 0.30, 0.75], [0.36, 0.84, 0.75], [0.98, 0.97, 0.44], [0.96, 0.62, 0.04], [0.84, 0.15, 0.24]];
  const x = t * (s.length - 1), i = Math.floor(x), f = x - i, a = s[i], b = s[Math.min(i + 1, s.length - 1)];
  return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f];
}
function fmt(v: number) {
  const x = Math.abs(v);
  if (x !== 0 && (x < 0.01 || x >= 1e5)) return v.toExponential(2);
  return (+v.toFixed(x < 1 ? 3 : 1)).toString();
}
// 4x4 对称矩阵最大特征值对应的特征向量（循环 Jacobi）
function jacobi4Max(Ain: number[][]): number[] {
  const a = Ain.map((r) => r.slice());
  const v = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]];
  for (let sweep = 0; sweep < 100; sweep++) {
    let off = 0;
    for (let i = 0; i < 4; i++) for (let j = i + 1; j < 4; j++) off += a[i][j] * a[i][j];
    if (off < 1e-18) break;
    for (let p = 0; p < 4; p++) for (let q = p + 1; q < 4; q++) {
      if (Math.abs(a[p][q]) < 1e-20) continue;
      const theta = (a[q][q] - a[p][p]) / (2 * a[p][q]);
      const t = Math.sign(theta || 1) / (Math.abs(theta) + Math.sqrt(theta * theta + 1));
      const c = 1 / Math.sqrt(t * t + 1), s = t * c, tau = s / (1 + c), apq = a[p][q];
      a[p][p] -= t * apq; a[q][q] += t * apq; a[p][q] = 0; a[q][p] = 0;
      for (let i = 0; i < 4; i++) if (i !== p && i !== q) {
        const aip = a[i][p], aiq = a[i][q];
        a[i][p] = aip - s * (aiq + tau * aip); a[p][i] = a[i][p];
        a[i][q] = aiq + s * (aip - tau * aiq); a[q][i] = a[i][q];
      }
      for (let i = 0; i < 4; i++) {
        const vip = v[i][p], viq = v[i][q];
        v[i][p] = vip - s * (viq + tau * vip);
        v[i][q] = viq + s * (vip - tau * viq);
      }
    }
  }
  let mi = 0; for (let i = 1; i < 4; i++) if (a[i][i] > a[mi][mi]) mi = i;
  return [v[0][mi], v[1][mi], v[2][mi], v[3][mi]];
}
// 最优刚体旋转(Horn)：S 为 Σ(P0-c0)(Ps-cs)^T 的 9 个分量，返回把 P0 对到 Ps 的四元数
function bestFitQuat(Sxx: number, Sxy: number, Sxz: number, Syx: number, Syy: number, Syz: number, Szx: number, Szy: number, Szz: number): THREE.Quaternion {
  const N = [
    [Sxx + Syy + Szz, Syz - Szy, Szx - Sxz, Sxy - Syx],
    [Syz - Szy, Sxx - Syy - Szz, Sxy + Syx, Szx + Sxz],
    [Szx - Sxz, Sxy + Syx, -Sxx + Syy - Szz, Syz + Szy],
    [Sxy - Syx, Szx + Sxz, Syz + Szy, -Sxx - Syy + Szz],
  ];
  const e = jacobi4Max(N); // [w,x,y,z]
  return new THREE.Quaternion(e[1], e[2], e[3], e[0]).normalize();
}

async function start() {
  const path = route.query.path as string | undefined;
  if (!path) { phase.value = "error"; statusMsg.value = "缺少 d3plot 路径"; return; }
  try {
    phase.value = "parsing"; progress.value = 0; statusMsg.value = "检查缓存…";
    // 高精度=不减面(max_tris=0)；预览=用服务端默认预算(传 undefined)
    const maxTris = precision.value === "full" ? 0 : undefined;
    const prep = await api.d3plotPrepare(path, maxStates.value, maxTris);
    if (prep.ready) { await load(prep.key); return; }
    // 解析中：轮询任务进度
    phase.value = "parsing"; statusMsg.value = "服务器解析 d3plot 中…";
    await new Promise<void>((resolve, reject) => {
      stopPoll = pollTask(prep.task_id!, (s) => {
        progress.value = Math.round(s.progress || 0);
        if (s.phase) statusMsg.value = s.phase;
        if (s.status === "success") { stopPoll?.(); resolve(); }
        else if (s.status === "failed" || s.status === "interrupted") { stopPoll?.(); reject(new Error(s.error || "解析失败")); }
      });
    });
    await load(prep.key);
  } catch (e) {
    phase.value = "error"; statusMsg.value = errMsg(e);
  }
}

async function load(key: string) {
  phase.value = "loading"; statusMsg.value = "下载模型数据…";
  const man: D3plotManifest = await api.d3plotManifest(key);
  modelBytes.value = man.bytes || 0;
  dlLoaded.value = 0; dlTotal.value = 0;
  const buf = await api.d3plotBin(key, (loaded, total) => {
    dlLoaded.value = loaded; dlTotal.value = total;
  });
  statusMsg.value = "构建三维场景…";
  // 让进度条先渲染到 100% 再做同步建模(建模会阻塞主线程)
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  build(man, buf, key);
  phase.value = "ready"; statusMsg.value = "";
}

function build(man: D3plotManifest, buf: ArrayBuffer, key: string) {
  const U = man.n_verts, T = man.n_tris, S = man.n_states, B = man.n_beam_verts, L = man.n_beam_lines, P = man.n_parts;
  nStates.value = S;
  meta.value = `${U.toLocaleString()}点 / ${T.toLocaleString()}面 / ${L}梁 / ${P}部件 / ${S}帧 · ${man.decimated ? '预览(减面)' : '高精度'}`;
  fields.value = man.fields;
  times.value = man.times && man.times.length ? man.times : null;

  // 布局：u32/f32 在前(4 字节对齐，零拷贝视图)，u16(triPart/bPart)在末尾
  let o = 0;
  const triIdx = new Uint32Array(buf, o, T * 3); o += T * 3 * 4;
  const coords = new Float32Array(buf, o, S * U * 3); o += S * U * 3 * 4;
  const bcoords = new Float32Array(buf, o, S * B * 3); o += S * B * 3 * 4;
  const bIdx = new Uint32Array(buf, o, L * 2); o += L * 2 * 4;
  const triPart = new Uint16Array(buf, o, T); o += T * 2;
  const bPart = new Uint16Array(buf, o, L); o += L * 2;
  // 云图场不再随 model.bin 下发：变形幅值(无 file)用 coords 现算；vonMises/塑性应变按需下载缓存。
  const fieldArr: (Float32Array | undefined)[] = man.fields.map(() => undefined);
  {
    const def = new Float32Array(S * U);
    for (let s = 0; s < S; s++) {
      const so = s * U * 3;
      for (let i = 0; i < U; i++) {
        const dx = coords[so + 3 * i] - coords[3 * i];
        const dy = coords[so + 3 * i + 1] - coords[3 * i + 1];
        const dz = coords[so + 3 * i + 2] - coords[3 * i + 2];
        def[s * U + i] = Math.sqrt(dx * dx + dy * dy + dz * dz);
      }
    }
    const di = man.fields.findIndex((f) => !f.file); // 变形幅值
    if (di >= 0) fieldArr[di] = def;
  }
  async function ensureField(i: number): Promise<boolean> {
    if (fieldArr[i]) return true;
    const file = man.fields[i]?.file;
    if (!file) return false;
    const b = await api.d3plotField(key, file);
    fieldArr[i] = new Float32Array(b);
    return true;
  }

  const partCol = new Float32Array(P * 3);
  for (let i = 0; i < P; i++) { const c = hsv((i * 0.61803) % 1, 0.55, 0.95); partCol[3 * i] = c[0]; partCol[3 * i + 1] = c[1]; partCol[3 * i + 2] = c[2]; }

  const renderer = new THREE.WebGLRenderer({ canvas: canvas.value!, antialias: true });
  const W = canvas.value!.clientWidth || window.innerWidth;
  const H = canvas.value!.clientHeight || window.innerHeight;
  renderer.setSize(W, H, false); renderer.setPixelRatio(Math.min(2, devicePixelRatio));
  renderer.localClippingEnabled = false;
  const clipPlane = new THREE.Plane(new THREE.Vector3(0, 0, 1), 1e9);  // 初始不裁剪
  const scene = new THREE.Scene(); scene.background = new THREE.Color(0x11111b);
  const camera = new THREE.PerspectiveCamera(50, W / H, 0.1, 1e7);
  // 轨迹球：任意方向自由旋转（无极点限制），贴合 LS-PrePost 等后处理习惯
  const controls = new TrackballControls(camera, renderer.domElement);
  controls.rotateSpeed = 3.5;
  controls.zoomSpeed = 1.3;
  controls.panSpeed = 0.8;
  controls.staticMoving = false;
  controls.dynamicDampingFactor = 0.15;
  // 灯光布局对齐 Vektor3D(半球光 + 上/下方向光，强度 0.5π，r155+ 物理光照约定)
  // 主光(强,定形)+ 补光(弱,去死黑)+ 半球光(天空亮/地面暗，给上下明暗梯度，避免平板"一片亮")
  const sun = new THREE.DirectionalLight(0xffffff, 1.5); sun.position.set(0.6, 1, 0.8); scene.add(sun);
  const fill = new THREE.DirectionalLight(0xffffff, 0.55); fill.position.set(-0.8, -0.4, -1); scene.add(fill);
  const hemi = new THREE.HemisphereLight(0xdde6f2, 0x3a3a44, 1.15); hemi.position.set(0, 1, 0); scene.add(hemi);

  const geom = new THREE.BufferGeometry();
  const pos = new Float32Array(U * 3), col = new Float32Array(U * 3);
  geom.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  geom.setAttribute("color", new THREE.BufferAttribute(col, 3));
  geom.setIndex(new THREE.BufferAttribute(new Uint32Array(triIdx), 1));
  const mesh = new THREE.Mesh(geom, new THREE.MeshPhongMaterial({
    vertexColors: true, side: THREE.DoubleSide, shininess: 6,
    polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
    clippingPlanes: [clipPlane],
  }));
  scene.add(mesh);
  // 网格线叠加层（共用同一几何，自动随变形与部件显隐）
  const wireMesh = new THREE.Mesh(geom, new THREE.MeshBasicMaterial({
    color: 0x1b1b28, wireframe: true, transparent: true, opacity: 0.35,
    clippingPlanes: [clipPlane],
  }));
  wireMesh.visible = wire.value; scene.add(wireMesh);

  const bpos = new Float32Array(B * 3), bcol = new Float32Array(B * 3);
  const bgeom = new THREE.BufferGeometry();
  bgeom.setAttribute("position", new THREE.BufferAttribute(bpos, 3));
  bgeom.setAttribute("color", new THREE.BufferAttribute(bcol, 3));
  bgeom.setIndex(new THREE.BufferAttribute(new Uint32Array(bIdx), 1));
  scene.add(new THREE.LineSegments(bgeom, new THREE.LineBasicMaterial({ vertexColors: true, clippingPlanes: [clipPlane] })));
  for (let l = 0; l < L; l++) { const p = bPart[l]; for (const v of [bIdx[2 * l], bIdx[2 * l + 1]]) { bcol[3 * v] = partCol[3 * p]; bcol[3 * v + 1] = partCol[3 * p + 1]; bcol[3 * v + 2] = partCol[3 * p + 2]; } }
  bgeom.attributes.color.needsUpdate = true;

  const partVertCol = new Float32Array(U * 3);
  for (let t = 0; t < T; t++) { const p = triPart[t]; for (let k = 0; k < 3; k++) { const v = triIdx[3 * t + k]; partVertCol[3 * v] = partCol[3 * p]; partVertCol[3 * v + 1] = partCol[3 * p + 1]; partVertCol[3 * v + 2] = partCol[3 * p + 2]; } }

  // 按类别(region)着色：同一类别一种颜色（同类零件名归并，假人按身体部位等）
  // 统计各类别零件数，按数量降序排序 → 大件优先拿到色板靠前的鲜明色，长尾小件循环复用
  const grpCount = new Map<string, number>();
  man.parts.forEach((p) => { const g = groupOf(p.title); grpCount.set(g, (grpCount.get(g) || 0) + 1); });
  const regionNames = [...grpCount.keys()].sort((a, b) => (grpCount.get(b)! - grpCount.get(a)!) || a.localeCompare(b));
  const regionIdx = new Map(regionNames.map((r, i) => [r, i]));
  const regionCol = new Float32Array(regionNames.length * 3);
  for (let i = 0; i < regionNames.length; i++) { const c = PALETTE_RGB[i % PALETTE_RGB.length]; regionCol[3 * i] = c[0]; regionCol[3 * i + 1] = c[1]; regionCol[3 * i + 2] = c[2]; }
  const partRegionVertCol = new Float32Array(U * 3);
  for (let t = 0; t < T; t++) {
    const ri = regionIdx.get(groupOf(man.parts[triPart[t]]?.title || "")) ?? 0;
    for (let k = 0; k < 3; k++) { const v = triIdx[3 * t + k]; partRegionVertCol[3 * v] = regionCol[3 * ri]; partRegionVertCol[3 * v + 1] = regionCol[3 * ri + 1]; partRegionVertCol[3 * v + 2] = regionCol[3 * ri + 2]; }
  }

  const c0 = coords.subarray(0, U * 3), bc0 = bcoords.subarray(0, B * 3);
  const visible = new Uint8Array(P).fill(1);
  const visTriMap = new Uint32Array(T);  // 第 k 个可见三角 -> 原始三角号（供拾取反查部件）

  function rebuildIndex() {
    const out = new Uint32Array(T * 3); let n = 0, k = 0;
    for (let t = 0; t < T; t++) if (visible[triPart[t]]) { visTriMap[k++] = t; out[n++] = triIdx[3 * t]; out[n++] = triIdx[3 * t + 1]; out[n++] = triIdx[3 * t + 2]; }
    geom.setIndex(new THREE.BufferAttribute(out.subarray(0, n), 1));
    const bo = new Uint32Array(L * 2); let m = 0;
    for (let l = 0; l < L; l++) if (visible[bPart[l]]) { bo[m++] = bIdx[2 * l]; bo[m++] = bIdx[2 * l + 1]; }
    bgeom.setIndex(new THREE.BufferAttribute(bo.subarray(0, m), 1));
  }
  function setColors() {
    const F = fieldArr[curField.value];
    // 部件着色=Vektor3D 风格高光(立体感); 云图着色=平光无高光(避免高光斑扭曲颜色判读)
    const showingField = mode.value === "field" && !!F;
    const phong = mesh.material as THREE.MeshPhongMaterial;
    phong.shininess = showingField ? 5 : 100;
    phong.specular.setHex(showingField ? 0x000000 : 0x444e5a);
    if (mode.value === "field" && F) {
      const f0 = man.fields[curField.value];
      const mn = f0.lo ?? f0.min, mx = f0.hi ?? f0.max, rg = (mx - mn) || 1;  // 分位色阶
      const fo = frame.value * U;
      for (let i = 0; i < U; i++) { const v = F[fo + i]; let c; if (v !== v) c = [0.45, 0.45, 0.5]; else c = jet(Math.min(1, Math.max(0, (v - mn) / rg))); col[3 * i] = c[0]; col[3 * i + 1] = c[1]; col[3 * i + 2] = c[2]; }
    } else if (mode.value === "region") {
      col.set(partRegionVertCol);  // 同类一色
    } else {
      col.set(partVertCol);  // 每零件一色(场未加载也回退到此)
    }
    geom.attributes.color.needsUpdate = true;
  }
  // 多测量可视：每条测量一组(两端点球 + 连线)，按 id 管理；位置在 setFrame 中按当前帧更新
  const measVis = new Map<number, { a: THREE.Mesh; b: THREE.Mesh; line: THREE.Line; geom: THREE.BufferGeometry }>();
  let measGroup: THREE.Group | null = null;   // 延迟到首次需要时创建
  let measTmp: THREE.Mesh | null = null;      // 测距拾取第 1 点的临时标记
  // 参考系：固定某部件 —— 逐 state 求其最优刚体变换的逆，作用到全场
  let refA: THREE.Matrix3[] = [], refB: THREE.Vector3[] = [];
  function computeRefFrame(refPart: number) {
    const set = new Set<number>();
    for (let t = 0; t < T; t++) if (triPart[t] === refPart) { set.add(triIdx[3 * t]); set.add(triIdx[3 * t + 1]); set.add(triIdx[3 * t + 2]); }
    const verts = [...set]; refA = []; refB = [];
    if (verts.length < 1) return false;
    let c0x = 0, c0y = 0, c0z = 0;
    for (const vi of verts) { c0x += coords[3 * vi]; c0y += coords[3 * vi + 1]; c0z += coords[3 * vi + 2]; }
    const nV = verts.length; c0x /= nV; c0y /= nV; c0z /= nV;
    const C0 = new THREE.Vector3(c0x, c0y, c0z);
    for (let s = 0; s < S; s++) {
      const off = s * U * 3;
      let csx = 0, csy = 0, csz = 0;
      for (const vi of verts) { csx += coords[off + 3 * vi]; csy += coords[off + 3 * vi + 1]; csz += coords[off + 3 * vi + 2]; }
      csx /= nV; csy /= nV; csz /= nV;
      let Sxx = 0, Sxy = 0, Sxz = 0, Syx = 0, Syy = 0, Syz = 0, Szx = 0, Szy = 0, Szz = 0;
      for (const vi of verts) {
        const ax = coords[3 * vi] - c0x, ay = coords[3 * vi + 1] - c0y, az = coords[3 * vi + 2] - c0z;
        const bx = coords[off + 3 * vi] - csx, by = coords[off + 3 * vi + 1] - csy, bz = coords[off + 3 * vi + 2] - csz;
        Sxx += ax * bx; Sxy += ax * by; Sxz += ax * bz;
        Syx += ay * bx; Syy += ay * by; Syz += ay * bz;
        Szx += az * bx; Szy += az * by; Szz += az * bz;
      }
      const q = bestFitQuat(Sxx, Sxy, Sxz, Syx, Syy, Syz, Szx, Szy, Szz);
      const Rt = new THREE.Matrix3().setFromMatrix4(new THREE.Matrix4().makeRotationFromQuaternion(q)).transpose();
      const b = C0.clone().sub(new THREE.Vector3(csx, csy, csz).applyMatrix3(Rt));
      refA.push(Rt); refB.push(b);
    }
    return true;
  }

  // 两点距离：逐 state 计算 v1、v2 间欧氏距离(刚体不变量，与参考系无关)
  function distData(v1: number, v2: number) {
    const data = new Array<number>(S); let mn = Infinity, mx = -Infinity;
    for (let s = 0; s < S; s++) {
      const fo = s * U * 3;
      const dx = coords[fo + 3 * v1] - coords[fo + 3 * v2];
      const dy = coords[fo + 3 * v1 + 1] - coords[fo + 3 * v2 + 1];
      const dz = coords[fo + 3 * v1 + 2] - coords[fo + 3 * v2 + 2];
      const d = Math.sqrt(dx * dx + dy * dy + dz * dz);
      data[s] = d; if (d < mn) mn = d; if (d > mx) mx = d;
    }
    return { data, min: mn, max: mx };
  }
  // 单点位移：逐 state 计算顶点 v 相对其初始位置的位移；
  // 若启用参考系(固定某零件)，先把坐标变换到该零件随动坐标系，得到"相对固定件的位移"，否则为绝对位移。
  function dispData(v: number) {
    const useRef = refOn.value && refA.length === S;
    const data = new Array<number>(S), cx = new Array<number>(S), cy = new Array<number>(S), cz = new Array<number>(S);
    let p0x = 0, p0y = 0, p0z = 0, mn = Infinity, mx = -Infinity;
    for (let s = 0; s < S; s++) {
      const fo = s * U * 3;
      let x = coords[fo + 3 * v], y = coords[fo + 3 * v + 1], z = coords[fo + 3 * v + 2];
      if (useRef) {
        const e = refA[s].elements, b = refB[s];
        const tx = e[0] * x + e[3] * y + e[6] * z + b.x, ty = e[1] * x + e[4] * y + e[7] * z + b.y, tz = e[2] * x + e[5] * y + e[8] * z + b.z;
        x = tx; y = ty; z = tz;
      }
      if (s === 0) { p0x = x; p0y = y; p0z = z; }
      const ux = x - p0x, uy = y - p0y, uz = z - p0z;
      cx[s] = ux; cy[s] = uy; cz[s] = uz;
      const d = Math.sqrt(ux * ux + uy * uy + uz * uz);
      data[s] = d; if (d < mn) mn = d; if (d > mx) mx = d;
    }
    return { data, cx, cy, cz, min: mn, max: mx, refTitle: useRef ? refTitle.value : null };
  }
  // 新增一条测量并同步可视对象
  function addMeasure(kind: "dist" | "disp", v1: number, v2: number) {
    const id = ++measSeq;
    const color = MEAS_PALETTE[(id - 1) % MEAS_PALETTE.length];
    const nSame = measures.value.filter((m) => m.kind === kind).length + 1;
    const name = (kind === "dist" ? "距离" : "位移") + nSame;
    let m: Meas;
    if (kind === "dist") {
      const d = distData(v1, v2);
      m = { id, kind, name, color, v1, v2, data: d.data, min: d.min, max: d.max, cx: [], cy: [], cz: [], refTitle: null };
    } else {
      const d = dispData(v1);
      m = { id, kind, name, color, v1, v2: v1, data: d.data, min: d.min, max: d.max, cx: d.cx, cy: d.cy, cz: d.cz, refTitle: d.refTitle };
    }
    measures.value = [...measures.value, m];
    syncMeasVis();
  }
  // 参考系变更后，重算所有位移测量(基准随之更新)
  function recomputeDisp() {
    let changed = false;
    for (const m of measures.value) {
      if (m.kind !== "disp") continue;
      const d = dispData(m.v1);
      m.data = d.data; m.cx = d.cx; m.cy = d.cy; m.cz = d.cz; m.min = d.min; m.max = d.max; m.refTitle = d.refTitle;
      changed = true;
    }
    if (changed) measures.value = [...measures.value];  // 触发曲线刷新
  }
  // 同步可视对象与 measures 列表(增/删)
  function syncMeasVis() {
    if (!measGroup) { measGroup = new THREE.Group(); scene.add(measGroup); }
    for (const [id, o] of measVis) {
      if (!measures.value.find((m) => m.id === id)) {
        measGroup.remove(o.a, o.b, o.line);
        o.a.geometry.dispose(); o.b.geometry.dispose(); o.geom.dispose();
        (o.a.material as THREE.Material).dispose(); (o.line.material as THREE.Material).dispose();
        measVis.delete(id);
      }
    }
    const r = (geom.boundingSphere?.radius || 1) * 0.014;
    for (const m of measures.value) {
      if (measVis.has(m.id)) continue;
      const col = new THREE.Color(m.color);
      const mat = new THREE.MeshBasicMaterial({ color: col });
      const a = new THREE.Mesh(new THREE.SphereGeometry(r, 12, 12), mat);
      const b = new THREE.Mesh(new THREE.SphereGeometry(r, 12, 12), mat);
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(6), 3));
      const line = new THREE.Line(g, new THREE.LineBasicMaterial({ color: col }));
      measGroup.add(a, b, line);
      measVis.set(m.id, { a, b, line, geom: g });
    }
  }

  function setFrame(s: number) {
    frame.value = s; const fo = s * U * 3, sc = scale.value;
    const useRef = refOn.value && refA.length === S;
    if (useRef) {
      const e = refA[s].elements, b = refB[s];
      for (let i = 0; i < U; i++) {
        const x = coords[fo + 3 * i], y = coords[fo + 3 * i + 1], z = coords[fo + 3 * i + 2];
        const ex = e[0] * x + e[3] * y + e[6] * z + b.x, ey = e[1] * x + e[4] * y + e[7] * z + b.y, ez = e[2] * x + e[5] * y + e[8] * z + b.z;
        pos[3 * i] = c0[3 * i] + sc * (ex - c0[3 * i]); pos[3 * i + 1] = c0[3 * i + 1] + sc * (ey - c0[3 * i + 1]); pos[3 * i + 2] = c0[3 * i + 2] + sc * (ez - c0[3 * i + 2]);
      }
    } else {
      for (let i = 0; i < U * 3; i++) pos[i] = c0[i] + sc * (coords[fo + i] - c0[i]);
    }
    geom.attributes.position.needsUpdate = true;
    if (B) {
      const bfo = s * B * 3;
      if (useRef) {
        const e = refA[s].elements, b = refB[s];
        for (let i = 0; i < B; i++) {
          const x = bcoords[bfo + 3 * i], y = bcoords[bfo + 3 * i + 1], z = bcoords[bfo + 3 * i + 2];
          const ex = e[0] * x + e[3] * y + e[6] * z + b.x, ey = e[1] * x + e[4] * y + e[7] * z + b.y, ez = e[2] * x + e[5] * y + e[8] * z + b.z;
          bpos[3 * i] = bc0[3 * i] + sc * (ex - bc0[3 * i]); bpos[3 * i + 1] = bc0[3 * i + 1] + sc * (ey - bc0[3 * i + 1]); bpos[3 * i + 2] = bc0[3 * i + 2] + sc * (ez - bc0[3 * i + 2]);
        }
      } else {
        for (let i = 0; i < B * 3; i++) bpos[i] = bc0[i] + sc * (bcoords[bfo + i] - bc0[i]);
      }
      bgeom.attributes.position.needsUpdate = true;
    }
    // 多测量标记/连线（位置随当前帧）
    for (const m of measures.value) {
      const o = measVis.get(m.id);
      if (!o) continue;
      const i1 = m.v1;
      o.a.position.set(pos[3 * i1], pos[3 * i1 + 1], pos[3 * i1 + 2]); o.a.visible = true;
      if (m.kind === "dist") {
        const i2 = m.v2;
        o.b.position.set(pos[3 * i2], pos[3 * i2 + 1], pos[3 * i2 + 2]);
      } else {
        o.b.position.set(c0[3 * i1], c0[3 * i1 + 1], c0[3 * i1 + 2]);  // 位移：另一端为初始锚点
      }
      o.b.visible = true;
      const lp = o.geom.attributes.position.array as Float32Array;
      lp[0] = o.a.position.x; lp[1] = o.a.position.y; lp[2] = o.a.position.z;
      lp[3] = o.b.position.x; lp[4] = o.b.position.y; lp[5] = o.b.position.z;
      o.geom.attributes.position.needsUpdate = true; o.line.visible = true;
    }
    // 测距拾取中的第 1 点临时标记
    if (measTmp) {
      if (measType.value === "dist" && measuring.value && measureA.value !== null) {
        const v = measureA.value;
        measTmp.position.set(pos[3 * v], pos[3 * v + 1], pos[3 * v + 2]); measTmp.visible = true;
      } else measTmp.visible = false;
    }
    setColors();
    timeLabel.value = man.times && man.times.length ? ` · t=${fmt(man.times[s])}` : "";
  }

  setFrame(0); rebuildIndex(); geom.computeVertexNormals(); geom.computeBoundingSphere();
  const bs = geom.boundingSphere!;

  // 标准视图方位（模型 Z 向上）
  // 汽车碰撞坐标系：X 纵向(前后)、Y 横向(左右)、Z 垂向(上下)
  const VIEWS: Record<string, { dir: [number, number, number]; up: [number, number, number] }> = {
    iso: { dir: [1, -1, 0.8], up: [0, 0, 1] },
    front: { dir: [1, 0, 0], up: [0, 0, 1] },
    back: { dir: [-1, 0, 0], up: [0, 0, 1] },
    left: { dir: [0, 1, 0], up: [0, 0, 1] },
    right: { dir: [0, -1, 0], up: [0, 0, 1] },
    top: { dir: [0, 0, 1], up: [1, 0, 0] },
    bottom: { dir: [0, 0, -1], up: [1, 0, 0] },
  };
  function applyView(name: string) {
    const v = VIEWS[name]; if (!v) return;
    const d = new THREE.Vector3(...v.dir).normalize().multiplyScalar(bs.radius * 2.0);
    camera.up.set(...v.up);
    camera.position.copy(bs.center).add(d);
    controls.target.copy(bs.center); controls.update();
  }
  applyView("iso");

  // 拾取：点选三角 -> 反查部件 + 当前场数值，红点标记
  const raycaster = new THREE.Raycaster();
  const marker = new THREE.Mesh(
    new THREE.SphereGeometry(bs.radius * 0.012, 16, 16),
    new THREE.MeshBasicMaterial({ color: 0xff3b3b })
  );
  marker.visible = false; scene.add(marker);
  // 测距拾取第 1 点的临时标记（最终测量的可视对象在 syncMeasVis 中按 id 动态创建）
  measTmp = new THREE.Mesh(new THREE.SphereGeometry(bs.radius * 0.014, 16, 16), new THREE.MeshBasicMaterial({ color: 0xffffff }));
  measTmp.visible = false; scene.add(measTmp);

  function pickAt(cx: number, cy: number) {
    const rect = renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((cx - rect.left) / rect.width) * 2 - 1, -((cy - rect.top) / rect.height) * 2 + 1);
    raycaster.setFromCamera(ndc, camera);
    const hits = raycaster.intersectObject(mesh, false);
    if (!hits.length || hits[0].faceIndex == null || !hits[0].face) { if (!measuring.value) clearPick(); return; }
    const h = hits[0];
    const part = triPart[visTriMap[h.faceIndex!]];
    const pt = h.point;
    const verts = [h.face!.a, h.face!.b, h.face!.c];
    let best = verts[0], bd = Infinity;
    for (const v of verts) { const dx = pos[3 * v] - pt.x, dy = pos[3 * v + 1] - pt.y, dz = pos[3 * v + 2] - pt.z; const d = dx * dx + dy * dy + dz * dz; if (d < bd) { bd = d; best = v; } }

    if (measuring.value) {
      if (measType.value === "disp") {  // 单点位移：一次点选即建立一条测量
        addMeasure("disp", best, best);
        measuring.value = false; measureA.value = null;
      } else if (measureA.value === null) {
        measureA.value = best;          // 测距第 1 点，等待第 2 点
      } else {
        addMeasure("dist", measureA.value, best);
        measuring.value = false; measureA.value = null;
      }
      setFrame(frame.value);
      return;
    }

    const F = fieldArr[curField.value], fo = frame.value * U;
    const val = F ? F[fo + best] : NaN;
    marker.position.copy(pt); marker.visible = true;
    picked.value = { part, title: man.parts[part].title, region: man.parts[part].region, field: man.fields[curField.value]?.name || "", value: val !== val ? "无数据" : fmt(val) };
  }
  let downX = 0, downY = 0;
  renderer.domElement.addEventListener("pointerdown", (e) => { downX = e.clientX; downY = e.clientY; });
  renderer.domElement.addEventListener("pointerup", (e) => { if (Math.hypot(e.clientX - downX, e.clientY - downY) < 5) pickAt(e.clientX, e.clientY); });

  // 剖切平面：方向(XY/XZ/YZ) + 平移(沿法向) + 旋转(绕面内两轴) + 定位点
  const clipCenter = bs.center.clone();
  secRange.value = bs.radius;
  const planeHelper = new THREE.PlaneHelper(clipPlane, bs.radius * 2.2, 0x4488ff);
  planeHelper.visible = false; scene.add(planeHelper);

  function applySection() {
    const ax = secAxis.value;
    const base = ax === "x" ? new THREE.Vector3(1, 0, 0) : ax === "y" ? new THREE.Vector3(0, 1, 0) : new THREE.Vector3(0, 0, 1);
    const a1 = ax === "x" ? new THREE.Vector3(0, 1, 0) : ax === "y" ? new THREE.Vector3(0, 0, 1) : new THREE.Vector3(1, 0, 0);
    const a2 = ax === "x" ? new THREE.Vector3(0, 0, 1) : ax === "y" ? new THREE.Vector3(1, 0, 0) : new THREE.Vector3(0, 1, 0);
    const q = new THREE.Quaternion().setFromAxisAngle(a1, (secRotA.value * Math.PI) / 180)
      .multiply(new THREE.Quaternion().setFromAxisAngle(a2, (secRotB.value * Math.PI) / 180));
    const n = base.applyQuaternion(q).normalize();
    if (secFlip.value) n.negate();
    const p = clipCenter.clone().addScaledVector(n, secOffset.value);
    clipPlane.normal.copy(n);
    clipPlane.constant = -n.dot(p);
    renderer.localClippingEnabled = secOn.value;
    planeHelper.visible = secOn.value;
  }

  // 区域分组
  const reg: Record<string, number[]> = {};
  man.parts.forEach((p, i) => { const g = groupOf(p.title); (reg[g] = reg[g] || []).push(i); });
  regions.value = Object.keys(reg).sort().map((name) => {
    const ri = regionIdx.get(name) ?? 0;  // 树色板与"按类别"3D 着色一致
    return { name, count: reg[name].length, color: `rgb(${regionCol[3 * ri] * 255 | 0},${regionCol[3 * ri + 1] * 255 | 0},${regionCol[3 * ri + 2] * 255 | 0})`, visible: true };
  });

  function resize() {
    const w = canvas.value!.clientWidth || window.innerWidth;
    const h = canvas.value!.clientHeight || window.innerHeight;
    camera.aspect = w / h; camera.updateProjectionMatrix(); renderer.setSize(w, h, false);
    controls.handleResize();
  }
  addEventListener("resize", resize);
  // 布局稳定后再校正一次尺寸（避免挂载瞬间 clientWidth 为 0）
  requestAnimationFrame(resize);

  const _midV = new THREE.Vector3();
  function loop(t: number) {
    raf = requestAnimationFrame(loop);
    if (playing.value && t - V.last > V.interval) { setFrame((frame.value + 1) % S); V.last = t; }
    controls.update(); renderer.render(scene, camera);
    // 3D 内测量标签：把每条测量的代表点(两端中点)投影到屏幕，随帧/旋转刷新
    if (measures.value.length) {
      const el = renderer.domElement;
      const labels: { id: number; x: number; y: number; text: string; color: string }[] = [];
      for (const m of measures.value) {
        const o = measVis.get(m.id);
        if (!o || !o.a.visible) continue;
        _midV.copy(o.a.position).add(o.b.position).multiplyScalar(0.5).project(camera);
        if (_midV.z >= 1) continue;
        labels.push({ id: m.id, x: (_midV.x * 0.5 + 0.5) * el.clientWidth, y: (-_midV.y * 0.5 + 0.5) * el.clientHeight, text: fmt(m.data[frame.value] ?? 0), color: m.color });
      }
      measLabels.value = labels;
    } else if (measLabels.value.length) {
      measLabels.value = [];
    }
  }
  V = { setFrame, setColors, rebuildIndex, visible, reg, partVertCol, renderer, controls, resize, marker, applyView, wireMesh, applySection, clipCenter, computeRefFrame, recomputeDisp, syncMeasVis, last: 0, interval: 1000,
        async setField(i: number) {
          curField.value = i; const f = man.fields[i];
          const lo = f.lo ?? f.min, hi = f.hi ?? f.max;  // 分位色阶
          legend.min = fmt(lo); legend.mid = fmt((lo + hi) / 2); legend.max = fmt(hi);
          legend.realMin = fmt(f.min); legend.realMax = fmt(f.max);
          legend.clamped = f.min < lo - 1e-6 || f.max > hi + 1e-6;  // 真实范围超出色阶=有被钳制的异常值
          if (!fieldArr[i] && f.file) {  // 云图场按需下载
            fieldBusy.value = true;
            try { await ensureField(i); } catch { /* 下载失败则保持上一场色 */ } finally { fieldBusy.value = false; }
          }
          setColors();
        } };
  raf = requestAnimationFrame(loop);
  if (man.fields.length) V.setField(0);
}

function togglePlay() { playing.value = !playing.value; }
// 逐帧步进(暂停后单步上一帧/下一帧，循环)
function stepFrame(d: number) {
  playing.value = false;
  if (!V) return;
  const n = nStates.value;
  V.setFrame(((frame.value + d) % n + n) % n);
}
function onSlider(e: Event) { playing.value = false; V?.setFrame(+(e.target as HTMLInputElement).value); }
function onScale(e: Event) { scale.value = +(e.target as HTMLInputElement).value; V?.setFrame(frame.value); }
function setMode(m: "part" | "region" | "field") { mode.value = m; V?.setColors(); }
function setView(name: string) { V?.applyView(name); }
function toggleWire() { wire.value = !wire.value; if (V) V.wireMesh.visible = wire.value; }
// 剖切
function toggleSection() { secOn.value = !secOn.value; V?.applySection(); }
function setSecAxis(ax: "x" | "y" | "z") { secAxis.value = ax; if (!secOn.value) secOn.value = true; V?.applySection(); }
function onSecOffset(e: Event) { secOffset.value = +(e.target as HTMLInputElement).value; V?.applySection(); }
function onSecRotA(e: Event) { secRotA.value = +(e.target as HTMLInputElement).value; V?.applySection(); }
function onSecRotB(e: Event) { secRotB.value = +(e.target as HTMLInputElement).value; V?.applySection(); }
function flipSection() { secFlip.value = !secFlip.value; V?.applySection(); }
function sectionHere() {
  if (!V) return;
  V.clipCenter.copy(V.marker.position);  // 用拾取点作为剖切面定位点
  secOffset.value = 0; secOn.value = true;
  V.applySection();
}
// 参考系：固定拾取到的部件
function fixToPart() {
  if (!V || !picked.value) return;
  if (V.computeRefFrame(picked.value.part)) { refOn.value = true; refTitle.value = picked.value.title; refPart.value = picked.value.part; }
  clearPick();
  V.recomputeDisp();  // 参考系变更后，位移基准随之更新
  V.setFrame(frame.value);
}
function clearRef() { refOn.value = false; refPart.value = null; if (V) V.recomputeDisp(); V?.setFrame(frame.value); }
// 参考系命名方案
function saveFrame() {
  if (refPart.value === null) return;
  savedFrames.value.push({ name: frameName.value.trim() || refTitle.value, part: refPart.value });
  frameName.value = "";
}
function activateFrame(f: { name: string; part: number }) {
  if (!V) return;
  if (V.computeRefFrame(f.part)) { refOn.value = true; refPart.value = f.part; refTitle.value = f.name; V.recomputeDisp(); V.setFrame(frame.value); }
}
function deleteFrame(i: number) { savedFrames.value.splice(i, 1); }
// 测量：两点测距 / 单点位移（可同时多条）
function beginMeasure(t: "dist" | "disp") {
  if (measuring.value && measType.value === t) { measuring.value = false; measureA.value = null; if (V) V.setFrame(frame.value); return; }
  measType.value = t;
  measuring.value = true;
  measureA.value = null;
  if (V) V.setFrame(frame.value);
}
function removeMeasure(id: number) {
  measures.value = measures.value.filter((m) => m.id !== id);
  if (V) { V.syncMeasVis(); V.setFrame(frame.value); }
}
function clearMeasure() {
  measures.value = [];
  measuring.value = false; measureA.value = null;
  if (V) { V.syncMeasVis(); V.setFrame(frame.value); }
}
// 导出所有测量曲线为 CSV（按帧逐行；距离 1 列、位移含合位移与 XYZ 分量 4 列）
function exportCsv() {
  if (!measures.value.length) return;
  const n = measures.value[0].data.length;
  const cols: { head: string; get: (s: number) => number }[] = [];
  for (const m of measures.value) {
    if (m.kind === "dist") cols.push({ head: `${m.name}(距离)`, get: (s) => m.data[s] });
    else {
      cols.push({ head: `${m.name}(合位移)`, get: (s) => m.data[s] });
      cols.push({ head: `${m.name}_X`, get: (s) => m.cx[s] });
      cols.push({ head: `${m.name}_Y`, get: (s) => m.cy[s] });
      cols.push({ head: `${m.name}_Z`, get: (s) => m.cz[s] });
    }
  }
  const hasT = !!(times.value && times.value.length);
  const rows = [["帧", ...(hasT ? ["时间"] : []), ...cols.map((c) => c.head)].join(",")];
  for (let s = 0; s < n; s++) {
    rows.push([String(s), ...(hasT ? [String(times.value![s])] : []), ...cols.map((c) => { const v = c.get(s); return Number.isFinite(v) ? v.toFixed(4) : ""; })].join(","));
  }
  const blob = new Blob(["﻿" + rows.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a"); a.href = url; a.download = "d3plot_测量曲线.csv"; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function onScrub(i: number) { playing.value = false; V?.setFrame(i); }
function onSpeed(e: Event) { speed.value = +(e.target as HTMLSelectElement).value; if (V) V.interval = 1000 / speed.value; }
const SPEEDS = [0.25, 0.5, 1, 2, 4];
const VIEW_BTNS: { k: string; label: string }[] = [
  { k: "iso", label: "等轴" }, { k: "front", label: "前" }, { k: "back", label: "后" },
  { k: "left", label: "左" }, { k: "right", label: "右" }, { k: "top", label: "上" }, { k: "bottom", label: "下" },
];
function onField(e: Event) { V?.setField(+(e.target as HTMLSelectElement).value); }
function toggleRegion(r: { name: string; visible: boolean }) {
  r.visible = !r.visible;
  V.reg[r.name].forEach((i: number) => (V.visible[i] = r.visible ? 1 : 0));
  V.rebuildIndex();
}
function setAll(v: boolean) { regions.value.forEach((r) => (r.visible = v)); V.visible.fill(v ? 1 : 0); V.rebuildIndex(); }
// 结构树勾选状态随逐部件可见性同步（区域内全可见才勾上）
function syncRegions() { if (!V) return; for (const r of regions.value) r.visible = V.reg[r.name].every((i: number) => V.visible[i] === 1); }
function hidePicked() { if (!V || !picked.value) return; V.visible[picked.value.part] = 0; V.rebuildIndex(); syncRegions(); clearPick(); }
function isolatePicked() { if (!V || !picked.value) return; V.visible.fill(0); V.visible[picked.value.part] = 1; V.rebuildIndex(); syncRegions(); }
function showAll() { setAll(true); clearPick(); }

onMounted(() => {
  // 先让用户选择读取帧数，再开始加载
  if (!route.query.path) { phase.value = "error"; statusMsg.value = "缺少 d3plot 路径"; return; }
  phase.value = "choose";
});
onBeforeUnmount(() => {
  cancelAnimationFrame(raf); stopPoll?.();
  if (V) { removeEventListener("resize", V.resize); V.renderer.dispose(); V.controls.dispose(); }
});
</script>

<template>
  <div class="fixed inset-0 bg-[#11111b] text-slate-200">
    <canvas ref="canvas" class="block w-full h-full"></canvas>

    <!-- 3D 内测量标签：每条测量一个，跟随中点，随帧/旋转刷新 -->
    <div v-for="lb in measLabels" :key="lb.id"
      class="pointer-events-none absolute z-20 -translate-x-1/2 -translate-y-1/2 rounded px-1.5 py-0.5 text-xs font-semibold text-slate-900 shadow ring-1 ring-white/40 whitespace-nowrap"
      :style="{ left: lb.x + 'px', top: lb.y + 'px', background: lb.color }">
      {{ lb.text }}
    </div>

    <!-- 顶部控制 -->
    <div v-if="phase === 'ready'" class="absolute top-3 left-3 bg-[#1e1e2e]/90 rounded-xl p-3 w-[340px] text-xs space-y-2">
      <div class="flex items-center gap-2">
        <button class="p-1 rounded hover:bg-slate-700" @click="router.back()"><ArrowLeft :size="16" /></button>
        <b class="text-blue-400">d3plot 仿真查看</b>
        <span class="text-slate-400 truncate">· {{ meta }}</span>
      </div>
      <div v-if="refOn" class="text-amber-300 bg-amber-500/10 rounded px-2 py-1 space-y-1">
        <div class="flex items-center gap-2">
          <span class="whitespace-nowrap">已固定</span>
          <span class="truncate">{{ refTitle }}</span>
          <button class="ml-auto px-1.5 rounded bg-slate-700 text-slate-200" @click="clearRef">取消</button>
        </div>
        <div class="flex items-center gap-1">
          <input v-model="frameName" placeholder="方案名" class="flex-1 bg-slate-800 rounded px-1.5 py-0.5 text-slate-200 outline-none" />
          <button class="px-1.5 rounded bg-amber-500 text-[#11111b]" @click="saveFrame">保存方案</button>
        </div>
      </div>
      <div v-if="savedFrames.length" class="flex items-center gap-1 flex-wrap text-slate-300">
        <span class="text-slate-400">方案</span>
        <span v-for="(f, i) in savedFrames" :key="i" class="flex items-center gap-1 bg-slate-700 rounded px-1.5 py-0.5">
          <button class="hover:text-blue-300" @click="activateFrame(f)">{{ f.name }}</button>
          <button class="text-slate-400 hover:text-rose-400" @click="deleteFrame(i)">×</button>
        </span>
      </div>
      <div class="flex items-center gap-2">
        <button class="p-1 rounded bg-slate-700 hover:bg-slate-600" title="上一帧" @click="stepFrame(-1)">
          <SkipBack :size="14" />
        </button>
        <button class="p-1 rounded bg-blue-500 text-[#11111b]" @click="togglePlay">
          <Pause v-if="playing" :size="14" /><Play v-else :size="14" />
        </button>
        <button class="p-1 rounded bg-slate-700 hover:bg-slate-600" title="下一帧" @click="stepFrame(1)">
          <SkipForward :size="14" />
        </button>
        <input type="range" min="0" :max="nStates - 1" :value="frame" class="flex-1" @input="onSlider" />
        <span class="tabular-nums whitespace-nowrap">{{ frame }}/{{ nStates - 1 }}{{ timeLabel }}</span>
        <select class="bg-slate-700 rounded px-1 py-0.5" :value="speed" @change="onSpeed" title="播放速度">
          <option v-for="s in SPEEDS" :key="s" :value="s">{{ s }}×</option>
        </select>
      </div>
      <div class="flex items-center gap-2">
        <span class="text-slate-400">着色</span>
        <button :class="['px-2 py-0.5 rounded', mode==='region'?'bg-blue-500 text-[#11111b] font-semibold':'bg-slate-700']" @click="setMode('region')" title="同一类零件用同一颜色">按类别</button>
        <button :class="['px-2 py-0.5 rounded', mode==='part'?'bg-blue-500 text-[#11111b] font-semibold':'bg-slate-700']" @click="setMode('part')" title="每个零件一种颜色">按部件</button>
        <button :class="['px-2 py-0.5 rounded', mode==='field'?'bg-blue-500 text-[#11111b] font-semibold':'bg-slate-700']" @click="setMode('field')">按场</button>
        <select v-if="mode==='field'" class="bg-slate-700 rounded px-1 py-0.5" @change="onField">
          <option v-for="(f,i) in fields" :key="i" :value="i">{{ f.name }}</option>
        </select>
        <span v-if="fieldBusy" class="text-amber-400 whitespace-nowrap">云图加载中…</span>
        <button
          :class="['ml-auto px-2 py-0.5 rounded', wire ? 'bg-blue-500 text-[#11111b] font-semibold' : 'bg-slate-700']"
          @click="toggleWire"
        >网格</button>
        <button
          :class="['px-2 py-0.5 rounded', measuring && measType==='dist' ? 'bg-cyan-400 text-[#11111b] font-semibold' : 'bg-slate-700']"
          @click="beginMeasure('dist')"
          title="两点之间的距离随时间变化（可加多条；与参考系无关）"
        >测距</button>
        <button
          :class="['px-2 py-0.5 rounded', measuring && measType==='disp' ? 'bg-cyan-400 text-[#11111b] font-semibold' : 'bg-slate-700']"
          @click="beginMeasure('disp')"
          title="单点位移（可加多条）：未固定零件=绝对位移；已固定零件=相对该零件的位移"
        >测位移</button>
      </div>
      <div v-if="mode==='field'">
        <div class="h-2 rounded" style="background:linear-gradient(90deg,#3b4cc0,#5cd6c0,#f9f871,#f59e0b,#d7263d)"></div>
        <div class="flex justify-between tabular-nums mt-0.5"><span>{{ legend.min }}</span><span>{{ legend.mid }}</span><span>{{ legend.max }}</span></div>
        <div v-if="legend.clamped" class="text-slate-500 mt-0.5">色阶取 1%~99% 分位 · 真实 {{ legend.realMin }} ~ {{ legend.realMax }}</div>
      </div>
      <div class="flex items-center gap-2">
        <span class="text-slate-400 whitespace-nowrap">变形放大 ×{{ scale }}</span>
        <input type="range" min="1" max="20" step="1" :value="scale" class="flex-1" @input="onScale" />
      </div>
      <div class="flex items-center gap-1">
        <span class="text-slate-400">视图</span>
        <button
          v-for="v in VIEW_BTNS"
          :key="v.k"
          class="px-1.5 py-0.5 rounded bg-slate-700 hover:bg-slate-600"
          @click="setView(v.k)"
        >{{ v.label }}</button>
      </div>
      <div class="flex items-center gap-1 flex-wrap">
        <span class="text-slate-400">剖切</span>
        <button :class="['px-2 py-0.5 rounded', secOn ? 'bg-blue-500 text-[#11111b] font-semibold' : 'bg-slate-700']" @click="toggleSection">{{ secOn ? "开" : "关" }}</button>
        <template v-if="secOn">
          <button v-for="a in SEC_AXES" :key="a.ax" :class="['px-1.5 py-0.5 rounded', secAxis === a.ax ? 'bg-blue-500 text-[#11111b] font-semibold' : 'bg-slate-700']" @click="setSecAxis(a.ax)">{{ a.label }}</button>
          <button :class="['px-1.5 py-0.5 rounded', secFlip ? 'bg-blue-500 text-[#11111b]' : 'bg-slate-700']" @click="flipSection">翻转</button>
        </template>
      </div>
      <div v-if="secOn" class="flex items-center gap-2">
        <span class="text-slate-400 whitespace-nowrap">平移</span>
        <input type="range" :min="-secRange" :max="secRange" :step="secRange / 200" :value="secOffset" class="flex-1" @input="onSecOffset" />
      </div>
      <div v-if="secOn" class="flex items-center gap-2">
        <span class="text-slate-400 whitespace-nowrap">旋转</span>
        <input type="range" min="-90" max="90" step="1" :value="secRotA" class="flex-1" @input="onSecRotA" title="绕轴1" />
        <input type="range" min="-90" max="90" step="1" :value="secRotB" class="flex-1" @input="onSecRotB" title="绕轴2" />
      </div>
    </div>

    <!-- 部件区域 -->
    <div v-if="phase === 'ready'" class="absolute top-3 right-3 bg-[#1e1e2e]/90 rounded-xl p-2 w-[220px] max-h-[92vh] flex flex-col text-xs">
      <div class="flex items-center gap-1.5">
        <b class="text-blue-400">部件区域</b>
        <button class="ml-auto px-1.5 py-0.5 rounded bg-slate-700" @click="setAll(true)">全显</button>
        <button class="px-1.5 py-0.5 rounded bg-slate-700" @click="setAll(false)">全隐</button>
      </div>
      <div class="overflow-auto mt-1.5">
        <label v-for="r in regions" :key="r.name" class="flex items-center gap-1.5 px-1 py-0.5 rounded hover:bg-slate-700 cursor-pointer">
          <input type="checkbox" :checked="r.visible" @change="toggleRegion(r)" />
          <span class="w-3 h-3 rounded-sm shrink-0" :style="{ background: r.color }"></span>
          <span>{{ r.name }} ({{ r.count }})</span>
        </label>
      </div>
    </div>

    <!-- 拾取结果 -->
    <div
      v-if="phase === 'ready' && picked"
      class="absolute bottom-3 left-3 bg-[#1e1e2e]/92 rounded-xl p-3 w-[320px] text-xs space-y-1"
    >
      <div class="flex items-center gap-2">
        <b class="text-rose-400">拾取</b>
        <button class="ml-auto px-1.5 rounded hover:bg-slate-700" @click="clearPick">✕</button>
      </div>
      <div class="break-all"><span class="text-slate-400">部件</span> {{ picked.title }}</div>
      <div><span class="text-slate-400">区域</span> {{ picked.region }}</div>
      <div><span class="text-slate-400">{{ picked.field }}</span> <span class="tabular-nums">{{ picked.value }}</span></div>
      <div class="flex gap-1.5 pt-1.5">
        <button class="px-2 py-1 rounded bg-rose-500 text-[#11111b] font-medium" @click="hidePicked">隐藏该部件</button>
        <button class="px-2 py-1 rounded bg-slate-700 hover:bg-slate-600" @click="isolatePicked">只看它</button>
        <button class="px-2 py-1 rounded bg-slate-700 hover:bg-slate-600" @click="showAll">显示全部</button>
      </div>
      <div class="flex gap-1.5 mt-1">
        <button class="flex-1 px-2 py-1 rounded bg-sky-600 hover:bg-sky-500 text-white" @click="sectionHere">在此处剖切</button>
        <button class="flex-1 px-2 py-1 rounded bg-amber-500 hover:bg-amber-400 text-[#11111b] font-medium" @click="fixToPart">固定此件</button>
      </div>
    </div>

    <!-- 测量面板：多条测距/位移 + 时间曲线（可交互图表区） -->
    <div
      v-if="phase === 'ready' && (measures.length || measuring)"
      class="absolute bottom-3 right-3 bg-[#1e1e2e]/95 rounded-xl p-3 w-[560px] text-xs shadow-xl"
    >
      <div class="flex items-center gap-2 mb-1">
        <b class="text-cyan-300">测量 · 时间曲线</b>
        <span v-if="measures.length" class="text-slate-400">共 {{ measures.length }} 条</span>
        <span class="ml-auto flex items-center gap-1">
          <button v-if="measures.length" class="px-2 py-0.5 rounded bg-emerald-600 hover:bg-emerald-500 text-white" @click="exportCsv">导出CSV</button>
          <button v-if="measures.length" class="px-2 py-0.5 rounded bg-slate-700 hover:bg-slate-600" @click="clearMeasure">清空</button>
        </span>
      </div>
      <div v-if="measuring" class="text-cyan-300 mb-1">
        <template v-if="measType === 'disp'">测位移：在零件上点选要测量的点（已固定零件则测相对位移）</template>
        <template v-else>测距：{{ measureA === null ? "① 点选第 1 个点" : "② 再点选第 2 个点" }}</template>
      </div>
      <!-- 测量条目列表 -->
      <div v-if="measures.length" class="max-h-[120px] overflow-auto mb-1 space-y-0.5">
        <div v-for="m in measures" :key="m.id" class="flex items-center gap-1.5 px-1 py-0.5 rounded hover:bg-slate-700/60">
          <span class="w-2.5 h-2.5 rounded-full shrink-0" :style="{ background: m.color }"></span>
          <span class="font-medium shrink-0" :style="{ color: m.color }">{{ m.name }}</span>
          <span class="text-slate-500 shrink-0">{{ m.kind === 'dist' ? '距离' : (m.refTitle ? '相对位移' : '绝对位移') }}</span>
          <span v-if="m.kind==='disp' && m.refTitle" class="text-amber-300 truncate" :title="m.refTitle">·{{ m.refTitle }}</span>
          <span class="ml-auto tabular-nums text-slate-300 shrink-0">当前 <b :style="{ color: m.color }">{{ (m.data[frame] ?? 0).toFixed(2) }}</b></span>
          <span class="tabular-nums text-slate-500 shrink-0">峰 {{ m.max.toFixed(2) }}</span>
          <button class="text-slate-400 hover:text-rose-400 px-1 shrink-0" title="删除该测量" @click="removeMeasure(m.id)">✕</button>
        </div>
      </div>
      <CurveChart
        v-if="measures.length"
        :series="measureSeries"
        :cursor="frame"
        :xs="times"
        :width="536"
        :height="200"
        @scrub="onScrub"
      />
      <div v-if="measures.length" class="text-center text-slate-500 mt-0.5">{{ times ? "时间" : "帧" }} · 点击/拖动曲线定位</div>
    </div>

    <!-- 帧数选择态 -->
    <div v-if="phase === 'choose'" class="absolute inset-0 flex items-center justify-center">
      <div class="bg-[#1e1e2e] rounded-xl p-6 w-[380px] text-sm space-y-4 shadow-xl">
        <div class="text-slate-200 font-medium">加载选项</div>

        <!-- 精度模式 -->
        <div class="space-y-1.5">
          <div class="text-xs text-slate-400">精度</div>
          <div class="grid grid-cols-2 gap-2">
            <button
              :class="['px-2 py-1.5 rounded text-center', precision === 'preview' ? 'bg-blue-500 text-[#11111b] font-semibold' : 'bg-slate-700 text-slate-200 hover:bg-slate-600']"
              @click="precision = 'preview'"
            >预览(快)</button>
            <button
              :class="['px-2 py-1.5 rounded text-center', precision === 'full' ? 'bg-blue-500 text-[#11111b] font-semibold' : 'bg-slate-700 text-slate-200 hover:bg-slate-600']"
              @click="precision = 'full'"
            >高精度(准)</button>
          </div>
          <div class="text-xs text-slate-500 leading-relaxed">
            <template v-if="precision === 'preview'">减面后快速可视化检查;测距/峰值应力为近似值。</template>
            <template v-else>不减面、原始节点与场值,精确测距/真实峰值;模型大、加载慢,建议有线。</template>
          </div>
        </div>

        <!-- 帧数 -->
        <div class="space-y-1.5">
          <div class="text-xs text-slate-400">读取帧数</div>
          <div class="grid grid-cols-3 gap-2">
            <button
              v-for="o in FRAME_OPTS" :key="o.v"
              :class="['px-2 py-1.5 rounded text-center', maxStates === o.v ? 'bg-blue-500 text-[#11111b] font-semibold' : 'bg-slate-700 text-slate-200 hover:bg-slate-600']"
              @click="maxStates = o.v"
            >{{ o.label }}</button>
          </div>
          <div class="text-xs text-slate-500">帧数越多越流畅但数据越大;长仿真会全程均匀抽样。</div>
        </div>

        <div v-if="precision === 'full' && maxStates === 0" class="text-xs text-amber-400 bg-amber-500/10 rounded px-2 py-1.5 leading-relaxed">
          ⚠ 高精度 + 全部帧 数据量可能极大(整车可达数百 MB~GB),慢链路会很慢,建议有线访问或先减少帧数。
        </div>

        <div class="flex gap-2 pt-1">
          <button class="flex-1 px-3 py-2 rounded bg-blue-500 text-[#11111b] font-semibold" @click="start">开始加载</button>
          <button class="px-3 py-2 rounded bg-slate-700 text-slate-200" @click="router.back()">返回</button>
        </div>
      </div>
    </div>

    <!-- 加载/解析/错误态 -->
    <div v-if="phase !== 'ready' && phase !== 'choose'" class="absolute inset-0 flex flex-col items-center justify-center gap-4 text-sm">
      <Loader2 v-if="phase !== 'error'" :size="28" class="animate-spin text-blue-400" />
      <div :class="phase === 'error' ? 'text-rose-400' : 'text-slate-300'">{{ statusMsg }}</div>

      <!-- 两段式进度：① 数据分析(服务器解析)  ② 下载模型 -->
      <div v-if="phase === 'parsing' || phase === 'loading'" class="w-72 space-y-3">
        <div>
          <div class="flex justify-between text-xs mb-1">
            <span class="text-slate-300">① 数据分析</span>
            <span class="tabular-nums text-slate-400">{{ phase === 'parsing' ? progress + '%' : '完成' }}</span>
          </div>
          <div class="h-2 bg-slate-700 rounded-full overflow-hidden">
            <div class="h-full bg-blue-500 transition-all" :style="{ width: (phase === 'parsing' ? progress : 100) + '%' }"></div>
          </div>
        </div>
        <div :class="phase === 'parsing' ? 'opacity-40' : ''">
          <div class="flex justify-between text-xs mb-1">
            <span class="text-slate-300">② 下载模型</span>
            <span class="tabular-nums text-slate-400">{{ phase === 'loading' ? dlText : '等待中' }}</span>
          </div>
          <div class="h-2 bg-slate-700 rounded-full overflow-hidden">
            <div class="h-full bg-emerald-500 transition-all" :style="{ width: (phase === 'loading' ? dlPct : 0) + '%' }"></div>
          </div>
        </div>
      </div>

      <button v-if="phase === 'error'" class="px-3 py-1.5 rounded bg-slate-700" @click="router.back()">返回</button>
    </div>
  </div>
</template>
