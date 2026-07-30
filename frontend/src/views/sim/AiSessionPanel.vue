<script setup lang="ts">
/**
 * 项目内 AI 会话面板（契约：docs/vektor3d-ai-session-contract.md）。
 *
 * 分工：SDM 持消息流（主数据，跨设备可见）；推理在 vektor3d 侧（llm.chat 能力，
 * 凭项目级只读票据自行拉主数据进 workspace）。AI 只能"提案"——提案卡片经用户
 * 确认后，由本组件带**用户自己的凭据**调既有接口执行，留痕自动生效；本面板
 * 没有任何绕过留痕的写路径。
 *
 * vektor3d 不可用时降级：消息照常记录（它是主数据），AI 回复不可用并明说原因
 * ——与需求解析"探测-降级"同一模式。
 */
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { simApi, errMsg } from "@/api";
import { vektor3d, Vektor3dError } from "@/api/vektor3d";
import type { SimAiMessage, SimAiProposal, SimAiSession } from "@/api/types";
import {
  Check,
  Loader2,
  MessageSquarePlus,
  Send,
  Sparkles,
  Trash2,
  X,
} from "lucide-vue-next";

const props = defineProps<{ pid: string }>();
const emit = defineEmits<{ close: [] }>();

const sessions = ref<SimAiSession[]>([]);
const activeSid = ref<string>("");
const messages = ref<SimAiMessage[]>([]);
const error = ref("");
const loading = ref(false);

// ── vektor3d 探测（探测-降级，与需求解析同一模式）──────────────────────────
const chatReady = ref(false);
const chatUnavailableReason = ref("正在探测 vektor3d…");

async function probe() {
  try {
    await vektor3d.health();
    const caps = await vektor3d.capabilities();
    const chat = caps.find((c) => c.id === "llm.chat");
    if (!chat) {
      chatReady.value = false;
      chatUnavailableReason.value =
        "该 vektor3d 版本没有 llm.chat 能力（会话契约 4.1），消息会记录但 AI 无法回复";
      return;
    }
    chatReady.value = true;
    chatUnavailableReason.value = "";
  } catch {
    chatReady.value = false;
    chatUnavailableReason.value =
      "vektor3d 未连接：消息会记录，AI 回复不可用。启动桌面端 vektor3d 后重试";
  }
}

// ── 会话装载 ─────────────────────────────────────────────────────────────
const listEl = ref<HTMLElement | null>(null);

async function scrollToBottom() {
  await nextTick();
  listEl.value?.scrollTo({ top: listEl.value.scrollHeight });
}

async function loadSessions() {
  sessions.value = await simApi.listAiSessions(props.pid);
  if (!activeSid.value && sessions.value.length) activeSid.value = sessions.value[0].id;
}

async function loadMessages() {
  if (!activeSid.value) {
    messages.value = [];
    return;
  }
  const detail = await simApi.getAiSession(activeSid.value);
  messages.value = detail.messages;
  await scrollToBottom();
}

async function newSession() {
  const s = await simApi.createAiSession(props.pid);
  await loadSessions();
  activeSid.value = s.id;
}

async function removeSession() {
  if (!activeSid.value) return;
  if (!confirm("删除该会话及全部消息？已确认提案的留痕在目标对象上，不受影响。")) return;
  await simApi.deleteAiSession(activeSid.value);
  activeSid.value = "";
  await loadSessions();
  await loadMessages();
}

watch(activeSid, () => loadMessages().catch((e) => (error.value = errMsg(e))));

onMounted(async () => {
  loading.value = true;
  try {
    await Promise.all([probe(), loadSessions()]);
    await loadMessages();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
});

// ── 发送与推理 ───────────────────────────────────────────────────────────
const draft = ref("");
const sending = ref(false);
const progressText = ref("");

async function send() {
  const text = draft.value.trim();
  if (!text || sending.value) return;
  sending.value = true;
  error.value = "";
  try {
    if (!activeSid.value) await newSession();
    const sid = activeSid.value;
    const userMsg = await simApi.addAiMessage(sid, { role: "user", content: text });
    messages.value.push(userMsg);
    draft.value = "";
    await scrollToBottom();

    if (!chatReady.value) return; // 消息已落库（主数据），推理降级

    // 票据 + 消息历史交给 vektor3d；上下文由它凭票据自行按需拉取（推理自治）
    const ticket = await simApi.aiReadTicket(props.pid);
    progressText.value = "已提交推理…";
    const result = await vektor3d.runJob<{
      message: string;
      proposals?: SimAiProposal[];
      citations?: { text?: string; ref: string }[];
      workspaceRun?: string;
    }>(
      "llm.chat",
      {
        project: {
          id: props.pid,
          baseUrl: `${window.location.origin}/api`,
          authToken: ticket.token,
          contextUrl: ticket.contextUrl,
        },
        sessionId: sid,
        messages: messages.value
          .filter((m) => m.role !== "system")
          .slice(-40)
          .map((m) => ({ role: m.role, content: m.content })),
        allowedActions: [
          "requirement_item.update",
          "requirement_item.resolve_clarification",
          "quality_template.edit_content",
          "quality_template.derive",
        ],
      },
      { onProgress: (p) => (progressText.value = p ? `${p.step}${p.detail ? `：${p.detail}` : ""}` : "推理中…") }
    );

    // assistant 消息落库（服务端校验提案枚举与 reason，并置 pending）
    const saved = await simApi.addAiMessage(sid, {
      role: "assistant",
      content: result.message || "（无文字回复）",
      proposals: result.proposals as never,
      citations: result.citations,
      meta: result.workspaceRun ? { workspaceRun: result.workspaceRun } : undefined,
    });
    messages.value.push(saved);
    await scrollToBottom();
  } catch (e) {
    error.value = e instanceof Vektor3dError ? `推理失败：${e.message}` : errMsg(e);
  } finally {
    sending.value = false;
    progressText.value = "";
  }
}

// ── 提案确认（写闸：先按 action 调既有接口，成功后才记裁决）────────────────
const ACTION_LABEL: Record<string, string> = {
  "requirement_item.update": "修改需求条目",
  "requirement_item.resolve_clarification": "解除待澄清",
  "quality_template.edit_content": "修改质量卡模板",
  "quality_template.derive": "派生质量卡模板",
};
const deciding = ref<string>(""); // `${mid}:${index}`

function composeSource(p: SimAiProposal): string {
  const refs = (p.evidence ?? []).map((e) => e.ref).filter(Boolean);
  return `AI 会话提案（经确认）：${p.reason}${refs.length ? `（依据 ${refs.join("；")}）` : ""}`;
}

async function executeProposal(p: SimAiProposal): Promise<void> {
  const patch = (p.patch ?? {}) as Record<string, never>;
  switch (p.action) {
    case "requirement_item.update":
      await simApi.updateRequirementItem(p.targetId!, patch);
      return;
    case "requirement_item.resolve_clarification":
      await simApi.updateRequirementItem(p.targetId!, {
        ...patch,
        needs_clarification: false,
        clarification_hint: composeSource(p),
      });
      return;
    case "quality_template.edit_content": {
      const overrides = ((patch.overrides ?? []) as { target: string; new_value: string }[])
        .map((o) => ({ ...o, source: composeSource(p) }));
      await simApi.editQualityTemplateContent(p.targetId!, overrides);
      return;
    }
    case "quality_template.derive": {
      const body = patch as unknown as { new_id: string; name: string;
        overrides?: { target: string; new_value: string; source: string }[] };
      const overrides = (body.overrides ?? []).map((o) => ({ ...o, source: composeSource(p) }));
      await simApi.deriveQualityTemplate(p.targetId!, { ...body, overrides });
      return;
    }
    default:
      throw new Error(`未知的提案动作 ${p.action}`);
  }
}

async function decide(m: SimAiMessage, index: number, decision: "confirmed" | "rejected") {
  deciding.value = `${m.id}:${index}`;
  error.value = "";
  try {
    if (decision === "confirmed") await executeProposal(m.proposals![index]);
    const updated = await simApi.decideAiProposal(activeSid.value, m.id, index, decision);
    const i = messages.value.findIndex((x) => x.id === m.id);
    if (i >= 0) messages.value[i] = updated;
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    deciding.value = "";
  }
}

const activeSession = computed(() => sessions.value.find((s) => s.id === activeSid.value));
</script>

<template>
  <div class="fixed inset-y-0 right-0 z-30 w-full max-w-xl bg-white border-l border-slate-200 shadow-xl flex flex-col">
    <!-- 头部 -->
    <div class="flex items-center gap-2 px-4 py-3 border-b border-slate-100">
      <Sparkles :size="16" class="text-indigo-500" />
      <span class="font-semibold text-slate-800">AI 会话</span>
      <select
        v-if="sessions.length"
        v-model="activeSid"
        class="ml-2 border border-slate-200 rounded px-2 py-1 text-xs text-slate-600 max-w-[180px]"
      >
        <option v-for="s in sessions" :key="s.id" :value="s.id">{{ s.title }}</option>
      </select>
      <button
        class="p-1 rounded hover:bg-slate-100 text-slate-500"
        title="新会话"
        @click="newSession"
      ><MessageSquarePlus :size="15" /></button>
      <button
        v-if="activeSid"
        class="p-1 rounded hover:bg-rose-50 text-slate-400 hover:text-rose-600"
        title="删除会话"
        @click="removeSession"
      ><Trash2 :size="15" /></button>
      <button class="ml-auto p-1 rounded hover:bg-slate-100 text-slate-500" @click="emit('close')">
        <X :size="18" />
      </button>
    </div>

    <!-- 降级提示 -->
    <div v-if="!chatReady" class="px-4 py-2 bg-amber-50 text-amber-800 text-xs">
      {{ chatUnavailableReason }}
    </div>
    <div v-if="error" class="px-4 py-2 bg-rose-50 text-rose-700 text-xs">{{ error }}</div>

    <!-- 消息流 -->
    <div ref="listEl" class="flex-1 overflow-y-auto px-4 py-3 space-y-3">
      <div v-if="loading" class="flex items-center gap-2 text-slate-400 text-sm justify-center py-10">
        <Loader2 :size="16" class="animate-spin" /> 加载中…
      </div>
      <div
        v-else-if="!messages.length"
        class="text-center text-xs text-slate-400 py-10"
      >
        围绕本项目提问：澄清需求疑问、修改条目、调整质量卡……<br />
        AI 的每个修改动作都会以提案卡片出现，经你确认才会生效并留痕。
      </div>

      <div v-for="m in messages" :key="m.id">
        <!-- system 注记 -->
        <div v-if="m.role === 'system'" class="text-center text-xs text-slate-400">
          {{ m.content }}
        </div>

        <div v-else :class="m.role === 'user' ? 'flex justify-end' : 'flex justify-start'">
          <div
            class="max-w-[85%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap break-words"
            :class="m.role === 'user' ? 'bg-blue-600 text-white' : 'bg-slate-100 text-slate-800'"
          >
            <div>{{ m.content }}</div>

            <!-- 出处：答疑也要可追溯 -->
            <div v-if="m.citations?.length" class="mt-1.5 flex flex-wrap gap-1">
              <span
                v-for="(c, i) in m.citations"
                :key="i"
                class="px-1.5 py-0.5 rounded bg-white/70 text-slate-500 text-xs"
                :title="c.text"
              >{{ c.ref }}</span>
            </div>

            <!-- 提案卡片 -->
            <div v-if="m.proposals?.length" class="mt-2 space-y-2">
              <div
                v-for="(p, i) in m.proposals"
                :key="i"
                class="rounded-md border bg-white px-3 py-2 text-slate-700"
                :class="p.status === 'rejected' ? 'border-slate-200 opacity-60' : 'border-indigo-200'"
              >
                <div class="flex items-center gap-2 text-xs">
                  <span class="font-medium text-indigo-700">
                    {{ ACTION_LABEL[p.action] ?? p.action }}
                  </span>
                  <span v-if="p.targetId" class="font-mono text-slate-400">{{ p.targetId }}</span>
                  <span
                    v-if="p.status !== 'pending'"
                    class="ml-auto px-1.5 py-0.5 rounded text-xs"
                    :class="p.status === 'confirmed'
                      ? 'bg-emerald-50 text-emerald-700'
                      : 'bg-slate-100 text-slate-500'"
                  >
                    {{ p.status === "confirmed" ? `已确认 · ${p.decided_by}` : "已拒绝" }}
                  </span>
                </div>
                <pre
                  v-if="p.patch"
                  class="mt-1 text-xs bg-slate-50 rounded px-2 py-1 overflow-x-auto"
                >{{ JSON.stringify(p.patch, null, 1) }}</pre>
                <div class="mt-1 text-xs text-slate-500">依据：{{ p.reason }}</div>
                <div v-if="p.evidence?.length" class="mt-0.5 flex flex-wrap gap-1">
                  <span
                    v-for="(e, j) in p.evidence"
                    :key="j"
                    class="px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 text-xs"
                  >{{ e.ref }}</span>
                </div>
                <div v-if="p.status === 'pending'" class="mt-2 flex gap-2">
                  <button
                    class="inline-flex items-center gap-1 px-2 py-1 rounded bg-indigo-600 text-white text-xs hover:bg-indigo-700 disabled:opacity-50"
                    :disabled="deciding === `${m.id}:${i}`"
                    @click="decide(m, i, 'confirmed')"
                  >
                    <Loader2 v-if="deciding === `${m.id}:${i}`" :size="12" class="animate-spin" />
                    <Check v-else :size="12" /> 确认执行
                  </button>
                  <button
                    class="px-2 py-1 rounded text-xs text-slate-500 hover:bg-slate-100 disabled:opacity-50"
                    :disabled="deciding === `${m.id}:${i}`"
                    @click="decide(m, i, 'rejected')"
                  >拒绝</button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div v-if="progressText" class="flex items-center gap-2 text-xs text-slate-400">
        <Loader2 :size="13" class="animate-spin" /> {{ progressText }}
      </div>
    </div>

    <!-- 输入区 -->
    <div class="border-t border-slate-100 px-4 py-3">
      <div class="flex items-end gap-2">
        <textarea
          v-model="draft"
          rows="2"
          class="flex-1 border border-slate-200 rounded-md px-2.5 py-1.5 text-sm resize-none"
          :placeholder="activeSession ? '输入问题，Ctrl+Enter 发送' : '发送后将自动新建会话'"
          @keydown.ctrl.enter.prevent="send"
        />
        <button
          class="inline-flex items-center gap-1.5 px-3 py-2 rounded-md bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50"
          :disabled="sending || !draft.trim()"
          @click="send"
        >
          <Loader2 v-if="sending" :size="14" class="animate-spin" />
          <Send v-else :size="14" />
        </button>
      </div>
    </div>
  </div>
</template>
