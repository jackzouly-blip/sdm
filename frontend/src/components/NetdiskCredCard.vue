<script setup lang="ts">
/**
 * 平台网盘凭据配置（仅管理员可见）。
 *
 * 这不是每个用户各自的凭据——普通用户只需粘分享链接+提取码，永远不接触
 * 这里的东西。BDUSS/STOKEN 是**平台自己那个百度账号**的网页 cookie，
 * 转存(share/transfer)没有开放接口，只能靠它，全平台配一次。
 *
 * 存库而非 env：它会过期、需定期轮换，改 env 得 ssh 上生产 + 重启服务。
 */
import { ref, computed } from "vue";
import { api, errMsg } from "@/api";
import type { NetdiskCredStatus } from "@/api/types";
import { fmtTime } from "@/lib/format";
import {
  KeyRound,
  Loader2,
  Check,
  AlertTriangle,
  Trash2,
  ChevronDown,
  ChevronRight,
  ExternalLink,
} from "lucide-vue-next";

const props = defineProps<{ cred: NetdiskCredStatus }>();
const emit = defineEmits<{ changed: [] }>();

const open = ref(!props.cred.configured); // 未配置时默认展开，省一次点击
const bduss = ref("");
const stoken = ref("");
const saving = ref(false);
const testing = ref(false);
const error = ref("");
const notice = ref("");

const badge = computed(() => {
  const c = props.cred;
  if (!c.configured) return { text: "未配置", cls: "bg-slate-100 text-slate-500" };
  if (c.state === "auth_failed")
    return { text: "已失效，需更换", cls: "bg-rose-100 text-rose-700" };
  if (c.state === "ok") return { text: "正常", cls: "bg-emerald-100 text-emerald-700" };
  return { text: "已配置，未验证", cls: "bg-amber-100 text-amber-700" };
});

async function save() {
  if (!bduss.value.trim()) {
    error.value = "BDUSS 不能为空";
    return;
  }
  error.value = "";
  notice.value = "";
  saving.value = true;
  try {
    await api.setNetdiskCredentials({
      bduss: bduss.value.trim(),
      stoken: stoken.value.trim(),
    });
    bduss.value = "";
    stoken.value = "";
    notice.value = "已保存，立即生效。建议点「测试连接」确认一次。";
    emit("changed");
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    saving.value = false;
  }
}

async function test() {
  error.value = "";
  notice.value = "";
  testing.value = true;
  try {
    const r = await api.testNetdiskCredentials();
    if (r.ok) notice.value = r.account ? `凭据有效（账号：${r.account}）` : r.detail;
    else error.value = r.detail;
    emit("changed");
  } catch (e) {
    error.value = errMsg(e);
  } finally {
    testing.value = false;
  }
}

async function clear() {
  if (!confirm("确定清除平台网盘凭据？清除后所有用户的同步都会停止。")) return;
  error.value = "";
  notice.value = "";
  try {
    await api.clearNetdiskCredentials();
    notice.value = "已清除。";
    emit("changed");
  } catch (e) {
    error.value = errMsg(e);
  }
}
</script>

<template>
  <div
    class="mb-4 bg-white rounded-xl border"
    :class="cred.state === 'auth_failed' ? 'border-rose-300' : 'border-slate-200'"
  >
    <button
      class="w-full flex items-center gap-2 px-4 py-3 text-left hover:bg-slate-50 rounded-xl"
      @click="open = !open"
    >
      <component :is="open ? ChevronDown : ChevronRight" :size="15" class="text-slate-400" />
      <KeyRound :size="16" class="text-slate-500" />
      <span class="text-sm font-medium text-slate-700">平台网盘凭据</span>
      <span class="px-2 py-0.5 rounded-full text-xs" :class="badge.cls">
        {{ badge.text }}
      </span>
      <AlertTriangle
        v-if="cred.state === 'auth_failed'"
        :size="15"
        class="text-rose-500"
      />
      <span class="ml-auto text-xs text-slate-400">
        <template v-if="cred.source === 'env'">来自部署文件（改动需重启）</template>
        <template v-else-if="cred.configured">
          {{ cred.updated_by }} 更新于 {{ fmtTime(cred.updated_at) }}
        </template>
        <template v-else>仅管理员可见</template>
      </span>
    </button>

    <div v-if="open" class="px-4 pb-4 border-t border-slate-100 pt-3">
      <p class="text-xs text-slate-500 bg-slate-50 border border-slate-100 rounded-md px-3 py-2 mb-3">
        这是<span class="font-medium text-slate-600">平台账号</span>的网盘登录凭据，全平台配一次；普通用户不需要、也接触不到它。
        <br />
        取值：用平台的百度账号登录
        <a
          href="https://pan.baidu.com"
          target="_blank"
          rel="noopener noreferrer"
          class="text-blue-600 hover:underline inline-flex items-center gap-0.5"
          >pan.baidu.com<ExternalLink :size="11"
        /></a>
        → 按 F12 打开开发者工具 → Application → Cookies → 复制 <code>BDUSS</code> 与
        <code>STOKEN</code> 两项的值。
        <br />
        <span class="text-amber-700">
          凭据会过期。失效时本卡片会标红，所有用户的同步都会停，届时按同样方法换一次即可（无需重启服务）。
        </span>
      </p>

      <div v-if="cred.configured" class="text-xs text-slate-500 mb-3 flex flex-wrap gap-x-4 gap-y-1">
        <span v-if="cred.account">账号：{{ cred.account }}</span>
        <span>STOKEN：{{ cred.has_stoken ? "已配" : "未配（转存可能失败）" }}</span>
        <span v-if="cred.last_checked_at">
          最后验证：{{ fmtTime(cred.last_checked_at) }}
        </span>
      </div>

      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <label class="block">
          <span class="text-sm text-slate-600">BDUSS</span>
          <input
            v-model="bduss"
            type="password"
            autocomplete="off"
            :placeholder="cred.configured ? '留空则不修改' : '粘贴 BDUSS 的值'"
            class="mt-1 w-full px-3 py-2 rounded-md border border-slate-300 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none text-sm font-mono"
          />
        </label>
        <label class="block">
          <span class="text-sm text-slate-600">STOKEN</span>
          <input
            v-model="stoken"
            type="password"
            autocomplete="off"
            :placeholder="cred.configured ? '留空则不修改' : '粘贴 STOKEN 的值'"
            class="mt-1 w-full px-3 py-2 rounded-md border border-slate-300 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none text-sm font-mono"
          />
        </label>
      </div>

      <p v-if="error" class="mt-2 text-sm text-rose-600">{{ error }}</p>
      <p v-else-if="notice" class="mt-2 text-sm text-emerald-600">{{ notice }}</p>

      <div class="mt-3 flex items-center gap-2">
        <button
          class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-60"
          :disabled="saving || !bduss.trim()"
          @click="save"
        >
          <Loader2 v-if="saving" :size="15" class="animate-spin" />
          保存
        </button>
        <button
          class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-slate-300 bg-white hover:bg-slate-50 disabled:opacity-60"
          :disabled="testing || !cred.configured"
          title="不需要分享链接，直接验证 cookie 是否还有效"
          @click="test"
        >
          <Loader2 v-if="testing" :size="15" class="animate-spin" />
          <Check v-else :size="15" />
          测试连接
        </button>
        <button
          v-if="cred.source === 'db'"
          class="ml-auto flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-rose-300 text-rose-600 bg-white hover:bg-rose-50"
          @click="clear"
        >
          <Trash2 :size="15" /> 清除
        </button>
      </div>
    </div>
  </div>
</template>
