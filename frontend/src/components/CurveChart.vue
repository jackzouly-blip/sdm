<script setup lang="ts">
/**
 * 可交互曲线图表（自绘 SVG，无第三方依赖）。
 * 支持多条曲线、坐标轴/网格、悬停读数十字线+提示、点击/拖动定位当前帧。
 * 为后续曲线交互（导入/分析/积分/参考系修正等）预留组件边界。
 */
import { computed, ref } from "vue";

const props = withDefaults(
  defineProps<{
    series: { name: string; color: string; data: number[] }[];
    cursor: number; // 当前帧索引
    xs?: number[] | null; // x 轴值（如时间），缺省用帧索引
    width?: number;
    height?: number;
    yLabel?: string;
  }>(),
  { width: 520, height: 240, xs: null, yLabel: "" }
);
const emit = defineEmits<{ (e: "scrub", index: number): void }>();

const M = { l: 54, r: 16, t: 12, b: 28 };
const pw = computed(() => props.width - M.l - M.r);
const ph = computed(() => props.height - M.t - M.b);
const n = computed(() => Math.max(1, ...props.series.map((s) => s.data.length)));
const allVals = computed(() => props.series.flatMap((s) => s.data));
const ymin = computed(() => (allVals.value.length ? Math.min(...allVals.value) : 0));
const ymax = computed(() => (allVals.value.length ? Math.max(...allVals.value) : 1));

function xAt(i: number) { return M.l + (n.value > 1 ? i / (n.value - 1) : 0) * pw.value; }
function yAt(v: number) { const rg = ymax.value - ymin.value || 1; return M.t + (1 - (v - ymin.value) / rg) * ph.value; }
function poly(data: number[]) { return data.map((v, i) => `${xAt(i).toFixed(1)},${yAt(v).toFixed(1)}`).join(" "); }

const hover = ref<number | null>(null);
let dragging = false;
function idxFromEvent(e: PointerEvent) {
  const svg = e.currentTarget as SVGElement;
  const r = svg.getBoundingClientRect();
  const x = (e.clientX - r.left) * (props.width / r.width);
  const f = (x - M.l) / pw.value;
  return Math.max(0, Math.min(n.value - 1, Math.round(f * (n.value - 1))));
}
function onDown(e: PointerEvent) { dragging = true; emit("scrub", idxFromEvent(e)); }
function onMove(e: PointerEvent) { hover.value = idxFromEvent(e); if (dragging) emit("scrub", hover.value); }
function onUp() { dragging = false; }
function onLeave() { hover.value = null; dragging = false; }

function fmt(v: number) { const x = Math.abs(v); if (x !== 0 && (x < 0.01 || x >= 1e5)) return v.toExponential(2); return (+v.toFixed(x < 1 ? 3 : 2)).toString(); }
const yTicks = computed(() => { const a = ymin.value, b = ymax.value, k = 4; return Array.from({ length: k + 1 }, (_, i) => a + ((b - a) * i) / k); });
const xTicks = computed(() => { const k = Math.min(6, Math.max(1, n.value - 1)); return Array.from({ length: k + 1 }, (_, i) => Math.round(((n.value - 1) * i) / k)); });
function xlab(i: number) { return props.xs ? fmt(props.xs[i]) : String(i); }
const tipX = computed(() => (hover.value === null ? 0 : Math.min(xAt(hover.value) + 8, props.width - 96)));
</script>

<template>
  <svg
    :viewBox="`0 0 ${width} ${height}`" :width="width" :height="height"
    style="touch-action: none; cursor: crosshair; display: block; max-width: 100%"
    @pointerdown="onDown" @pointermove="onMove" @pointerup="onUp" @pointerleave="onLeave"
  >
    <!-- 网格 + y 轴刻度 -->
    <g v-for="(t, i) in yTicks" :key="'y' + i">
      <line :x1="M.l" :x2="width - M.r" :y1="yAt(t)" :y2="yAt(t)" stroke="#313244" stroke-width="1" />
      <text :x="M.l - 6" :y="yAt(t) + 3" text-anchor="end" font-size="10" fill="#a6adc8">{{ fmt(t) }}</text>
    </g>
    <!-- x 轴刻度 -->
    <g v-for="(ti, i) in xTicks" :key="'x' + i">
      <text :x="xAt(ti)" :y="height - 9" text-anchor="middle" font-size="10" fill="#a6adc8">{{ xlab(ti) }}</text>
    </g>
    <!-- 曲线 -->
    <polyline v-for="(s, i) in series" :key="'s' + i" :points="poly(s.data)" fill="none" :stroke="s.color" stroke-width="1.8" />
    <!-- 当前帧游标 -->
    <line :x1="xAt(cursor)" :x2="xAt(cursor)" :y1="M.t" :y2="height - M.b" stroke="#f59e0b" stroke-width="1.3" />
    <!-- 悬停十字线 + 提示 -->
    <template v-if="hover !== null">
      <line :x1="xAt(hover)" :x2="xAt(hover)" :y1="M.t" :y2="height - M.b" stroke="#cdd6f4" stroke-dasharray="3 3" stroke-width="1" />
      <circle v-for="(s, i) in series" :key="'c' + i" :cx="xAt(hover)" :cy="yAt(s.data[hover])" r="3" :fill="s.color" />
      <g :transform="`translate(${tipX},${M.t + 6})`">
        <rect width="90" :height="16 + series.length * 13" rx="4" fill="#11111b" opacity="0.92" />
        <text x="6" y="12" font-size="10" fill="#a6adc8">{{ xs ? "t=" : "帧 " }}{{ xlab(hover) }}</text>
        <text v-for="(s, i) in series" :key="'tt' + i" x="6" :y="25 + i * 13" font-size="10" :fill="s.color">{{ fmt(s.data[hover]) }}</text>
      </g>
    </template>
  </svg>
</template>
