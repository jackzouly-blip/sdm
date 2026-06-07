<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { shellWs } from "@/api";
import { useAuthStore } from "@/stores/auth";

const auth = useAuthStore();
const termEl = ref<HTMLDivElement | null>(null);
const status = ref<"connecting" | "open" | "closed">("connecting");
const statusMsg = ref("");

let term: Terminal | null = null;
let fit: FitAddon | null = null;
let ws: WebSocket | null = null;
const enc = new TextEncoder();

function sendResize() {
  if (!term || ws?.readyState !== WebSocket.OPEN) return;
  ws.send(
    JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows })
  );
}

function onWindowResize() {
  try {
    fit?.fit();
  } catch {
    /* 容器未就绪时忽略 */
  }
}

onMounted(async () => {
  // 直接进入本页时 me 可能尚未加载，先确保拿到用户信息再判断权限
  if (!auth.me) {
    try {
      await auth.refreshMe();
    } catch {
      /* 拦截器会处理登录态失效 */
    }
  }
  if (!auth.me?.is_admin) {
    status.value = "closed";
    statusMsg.value = "需要管理员权限才能使用在线终端";
    return;
  }
  if (!termEl.value) return;

  term = new Terminal({
    fontSize: 13,
    fontFamily: 'Menlo, Monaco, "Courier New", monospace',
    cursorBlink: true,
    theme: { background: "#1e1e2e", foreground: "#cdd6f4" },
  });
  fit = new FitAddon();
  term.loadAddon(fit);
  term.open(termEl.value);
  fit.fit();

  ws = shellWs();
  ws.binaryType = "arraybuffer";

  ws.onopen = () => {
    status.value = "open";
    fit?.fit();
    sendResize();
    term?.focus();
  };
  ws.onmessage = (e: MessageEvent) => {
    if (e.data instanceof ArrayBuffer) {
      term?.write(new Uint8Array(e.data));
    } else if (typeof e.data === "string") {
      term?.write(e.data);
    }
  };
  ws.onclose = (e: CloseEvent) => {
    status.value = "closed";
    if (e.code === 4403) statusMsg.value = "无权限：仅管理员可用";
    else if (e.code === 4401) statusMsg.value = "登录态失效，请重新登录";
    else statusMsg.value = "会话已结束";
    term?.writeln("\r\n\x1b[33m[连接已关闭]\x1b[0m");
  };
  ws.onerror = () => {
    statusMsg.value = "连接异常";
  };

  // 键盘输入 -> 二进制帧
  term.onData((d) => {
    if (ws?.readyState === WebSocket.OPEN) ws.send(enc.encode(d));
  });
  // 终端尺寸变化 -> 通知后端调整 PTY
  term.onResize(() => sendResize());

  window.addEventListener("resize", onWindowResize);
});

onBeforeUnmount(() => {
  window.removeEventListener("resize", onWindowResize);
  try {
    ws?.close();
  } catch {
    /* ignore */
  }
  term?.dispose();
});
</script>

<template>
  <div class="flex flex-col h-full">
    <div class="flex items-center gap-3 mb-3">
      <h1 class="text-lg font-semibold text-slate-800">在线终端</h1>
      <span
        class="text-xs px-2 py-0.5 rounded-full"
        :class="{
          'bg-amber-50 text-amber-600': status === 'connecting',
          'bg-emerald-50 text-emerald-600': status === 'open',
          'bg-slate-100 text-slate-500': status === 'closed',
        }"
      >
        {{
          status === "open"
            ? "已连接"
            : status === "connecting"
            ? "连接中…"
            : "已断开"
        }}
      </span>
      <span v-if="statusMsg" class="text-xs text-slate-500">{{ statusMsg }}</span>
      <span class="ml-auto text-xs text-slate-400"
        >以 {{ auth.me?.username }} 身份运行 · 仅管理员可用</span
      >
    </div>
    <div
      ref="termEl"
      class="flex-1 min-h-0 rounded-xl overflow-hidden border border-slate-800 bg-[#1e1e2e] p-2"
    ></div>
  </div>
</template>
