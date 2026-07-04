<script setup lang="ts">
import { ref, onMounted } from "vue";
import { api, errMsg } from "@/api";
import type { UserPolicy } from "@/api/types";
import { fmtTime } from "@/lib/format";
import { Loader2, Plus, Save, Trash2, X, Users } from "lucide-vue-next";

const list = ref<UserPolicy[]>([]);
const loading = ref(false);
const error = ref("");
const saving = ref<string | null>(null); // 正在保存的用户名
const removing = ref<string | null>(null);

// 新增表单缓冲。注意：<input type="number"> 的 v-model 在 Vue 中会把值强制转成
// number（空则为空串），故这些缓冲实际可能是 number | string，比较/校验前统一归一。
const adding = ref(false);
const draftUser = ref("");
const draftMax = ref<string | number>(""); // 空=不限并发
const draftPriority = ref<string | number>(0);

// 把 number 输入的 v-model 值归一成去空白字符串（空/undefined→""）
function normStr(v: unknown): string {
  return v === "" || v == null ? "" : String(v).trim();
}

async function load() {
  loading.value = true;
  error.value = "";
  try {
    list.value = await api.listUserPolicies();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    loading.value = false;
  }
}
onMounted(load);

// 校验并归一化：max_concurrent 空=不限(null)，否则须为正整数；priority 为整数(默认0)
function parseForm(
  maxRaw: unknown,
  prioRaw: unknown
): { max_concurrent: number | null; priority: number } | string {
  let max: number | null = null;
  const mt = normStr(maxRaw);
  if (mt !== "") {
    const m = Number(mt);
    if (!Number.isInteger(m) || m < 1) return "并发上限需为正整数，或留空表示不限";
    max = m;
  }
  const pt = normStr(prioRaw);
  const p = pt === "" ? 0 : Number(pt);
  if (!Number.isInteger(p)) return "优先级需为整数";
  return { max_concurrent: max, priority: p };
}

function startAdd() {
  adding.value = true;
  draftUser.value = "";
  draftMax.value = "";
  draftPriority.value = 0;
  error.value = "";
}

async function saveNew() {
  const u = draftUser.value.trim();
  if (!u) {
    error.value = "请填写用户名";
    return;
  }
  const parsed = parseForm(draftMax.value, draftPriority.value);
  if (typeof parsed === "string") {
    error.value = parsed;
    return;
  }
  saving.value = u;
  error.value = "";
  try {
    await api.upsertUserPolicy(u, parsed);
    adding.value = false;
    await load();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    saving.value = null;
  }
}

// 表格内直接编辑：把某行的输入回写。number 输入的 v-model 可能写入 number，
// 故缓冲值类型放宽为 string | number，比较/校验统一经 normStr 归一。
const editBuf = ref<Record<string, { max: string | number; prio: string | number }>>({});

function bufFor(p: UserPolicy) {
  if (!editBuf.value[p.user]) {
    editBuf.value[p.user] = {
      max: p.max_concurrent == null ? "" : String(p.max_concurrent),
      prio: String(p.priority),
    };
  }
  return editBuf.value[p.user];
}

async function saveRow(p: UserPolicy) {
  const buf = bufFor(p);
  const parsed = parseForm(buf.max, buf.prio);
  if (typeof parsed === "string") {
    error.value = parsed;
    return;
  }
  saving.value = p.user;
  error.value = "";
  try {
    await api.upsertUserPolicy(p.user, parsed);
    delete editBuf.value[p.user];
    await load();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    saving.value = null;
  }
}

async function remove(p: UserPolicy) {
  if (!window.confirm(`删除「${p.user}」的策略？删除后该用户恢复为不限并发。`)) return;
  removing.value = p.user;
  error.value = "";
  try {
    await api.deleteUserPolicy(p.user);
    await load();
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    removing.value = null;
  }
}

// 某行相对已保存值是否有改动（决定"保存"按钮是否高亮可用）
function dirty(p: UserPolicy): boolean {
  const buf = editBuf.value[p.user];
  if (!buf) return false;
  const savedMax = p.max_concurrent == null ? "" : String(p.max_concurrent);
  return normStr(buf.max) !== savedMax || normStr(buf.prio) !== String(p.priority);
}
</script>

<template>
  <div class="w-full">
    <div class="flex items-center gap-3 mb-2">
      <h1 class="text-lg font-semibold text-slate-800">用户提交策略</h1>
      <span class="text-xs text-slate-400">控制各用户可并行运行的任务数与抢占优先级（管理员维护）</span>
      <button
        class="ml-auto flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-700"
        @click="startAdd"
      >
        <Plus :size="15" /> 新增策略
      </button>
    </div>

    <!-- 说明卡片 -->
    <div class="mb-4 text-xs text-slate-500 bg-slate-50 border border-slate-200 rounded-lg px-4 py-3 leading-relaxed">
      <div><span class="font-medium text-slate-600">并发上限</span>：该用户最多同时运行/排队 (Q+R) 的任务数；超出的提交会在门户本地排队，有名额时自动进入 PBS。留空 = 不限。</div>
      <div><span class="font-medium text-slate-600">优先级</span>：数字越大越优先抢占空出来的名额/核数。未配置策略的用户默认不限并发、优先级 0。</div>
    </div>

    <p v-if="error" class="text-sm text-rose-600 mb-3">{{ error }}</p>

    <div class="bg-white rounded-xl border border-slate-200 overflow-x-auto">
      <table class="w-full text-sm min-w-[640px]">
        <thead class="bg-slate-50 text-slate-500 text-left">
          <tr>
            <th class="px-4 py-2.5 font-medium">用户名</th>
            <th class="px-4 py-2.5 font-medium">并发上限</th>
            <th class="px-4 py-2.5 font-medium">优先级</th>
            <th class="px-4 py-2.5 font-medium">更新时间</th>
            <th class="px-4 py-2.5 font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          <!-- 新增行 -->
          <tr v-if="adding" class="border-t border-slate-100 bg-blue-50/40">
            <td class="px-4 py-2">
              <input
                v-model="draftUser"
                placeholder="系统用户名，如 user07"
                class="w-full px-2 py-1 border border-slate-300 rounded-md"
              />
            </td>
            <td class="px-4 py-2">
              <input
                v-model="draftMax"
                type="number"
                min="1"
                placeholder="留空=不限"
                class="w-28 px-2 py-1 border border-slate-300 rounded-md"
              />
            </td>
            <td class="px-4 py-2">
              <input
                v-model="draftPriority"
                type="number"
                class="w-20 px-2 py-1 border border-slate-300 rounded-md"
              />
            </td>
            <td class="px-4 py-2 text-slate-300">—</td>
            <td class="px-4 py-2">
              <div class="flex items-center gap-1.5">
                <button
                  class="inline-flex items-center gap-1 px-2.5 py-1 text-xs rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-60"
                  :disabled="saving === draftUser.trim()"
                  @click="saveNew"
                >
                  <Loader2 v-if="saving === draftUser.trim()" :size="13" class="animate-spin" />
                  <Save v-else :size="13" /> 保存
                </button>
                <button
                  class="inline-flex items-center gap-1 px-2.5 py-1 text-xs rounded-md border border-slate-300 hover:bg-slate-50"
                  @click="adding = false"
                >
                  <X :size="13" /> 取消
                </button>
              </div>
            </td>
          </tr>

          <!-- 已有策略行（可就地编辑） -->
          <tr
            v-for="p in list"
            :key="p.user"
            class="border-t border-slate-100 hover:bg-slate-50/60"
          >
            <td class="px-4 py-2.5 font-mono text-slate-700">{{ p.user }}</td>
            <td class="px-4 py-2">
              <input
                v-model="bufFor(p).max"
                type="number"
                min="1"
                placeholder="不限"
                class="w-28 px-2 py-1 border border-slate-300 rounded-md"
              />
            </td>
            <td class="px-4 py-2">
              <input
                v-model="bufFor(p).prio"
                type="number"
                class="w-20 px-2 py-1 border border-slate-300 rounded-md"
              />
            </td>
            <td class="px-4 py-2.5 text-slate-500">{{ fmtTime(p.updated_at) }}</td>
            <td class="px-4 py-2">
              <div class="flex items-center gap-1.5">
                <button
                  class="inline-flex items-center gap-1 px-2.5 py-1 text-xs rounded-md border disabled:opacity-50"
                  :class="dirty(p)
                    ? 'border-blue-300 text-blue-700 hover:bg-blue-50'
                    : 'border-slate-200 text-slate-400 cursor-default'"
                  :disabled="!dirty(p) || saving === p.user"
                  @click="saveRow(p)"
                >
                  <Loader2 v-if="saving === p.user" :size="13" class="animate-spin" />
                  <Save v-else :size="13" /> 保存
                </button>
                <button
                  class="inline-flex items-center gap-1 px-2.5 py-1 text-xs rounded-md border border-rose-200 text-rose-600 hover:bg-rose-50 disabled:opacity-60"
                  :disabled="removing === p.user"
                  @click="remove(p)"
                >
                  <Loader2 v-if="removing === p.user" :size="13" class="animate-spin" />
                  <Trash2 v-else :size="13" /> 删除
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-if="loading" class="py-12 flex items-center justify-center text-slate-400 gap-2">
        <Loader2 :size="18" class="animate-spin" /> 加载中…
      </div>
      <div
        v-else-if="!list.length && !adding"
        class="py-12 flex flex-col items-center justify-center text-slate-400 gap-2"
      >
        <Users :size="28" /> 暂无用户策略，所有用户不限并发
      </div>
    </div>
  </div>
</template>
