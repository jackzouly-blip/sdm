/**
 * vektor3d 能力服务客户端（浏览器 → 用户桌面上的 vektor3d）。
 *
 * 为什么由浏览器发起：SDM 后端在 Linux 集群，vektor3d 装在用户 Windows 桌面上、
 * 只监听 localhost —— 集群**永远调不通它**。反过来桌面能访问集群。所以：
 *
 *   浏览器   ──调用──> localhost:23710   只发 URL + 票据，不发文件
 *   vektor3d ──GET───> SDM               自己来拉源文件
 *   vektor3d ──POST──> SDM               自己把 GLB 推回去
 *
 * 文件全程不经浏览器：座椅、白车身这类装配动辄几百 MB，中转一次就会把页面内存打爆。
 * 契约见 docs/vektor3d-geometry-capability-contract.md。
 */

const BASE_KEY = "vektor3d_base";
const PAIRING_KEY = "vektor3d_pairing_token";

/** vektor3d 能力服务默认端口（与 PLM 智能研发向导共用同一个服务） */
export const DEFAULT_BASE = "http://127.0.0.1:23710";

export function getBase(): string {
  return localStorage.getItem(BASE_KEY) || DEFAULT_BASE;
}
export function setBase(v: string | null): void {
  if (v && v.trim()) localStorage.setItem(BASE_KEY, v.trim().replace(/\/+$/, ""));
  else localStorage.removeItem(BASE_KEY);
}
/** 配对令牌：vektor3d 侧「设置 → 能力服务」若启用了令牌，这里要填同一个值 */
export function getPairingToken(): string {
  return localStorage.getItem(PAIRING_KEY) || "";
}
export function setPairingToken(v: string | null): void {
  if (v && v.trim()) localStorage.setItem(PAIRING_KEY, v.trim());
  else localStorage.removeItem(PAIRING_KEY);
}

export interface CapabilityInfo {
  id: string;
  version: string;
  name: string;
  kind: string;
  estimatedSeconds: number | null;
  ready: boolean;
  notReadyReason: string | null;
}

export interface HealthInfo {
  ok: boolean;
  appVersion: string;
  capabilityCount: number;
  readyCount: number;
  /** 本页面的 Origin 是否已被 vektor3d 授权 */
  authorized: boolean;
  /** 稳定错误码：disabled / origin_not_allowed / bad_token */
  authCode: string | null;
  authMessage: string | null;
  notReady: { id: string; reason: string | null }[];
}

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "canceled";

export interface JobProgress {
  step: string;
  detail: string;
  ts: number;
}

export interface JobDetail<T = unknown> {
  jobId: string;
  capabilityId: string;
  status: JobStatus;
  progress: JobProgress[];
  result: T | null;
  error: string | null;
  queuePosition: number | null;
}

/** 转换产物的元数据（geometry.convert 的 result） */
export interface ConvertResult {
  uploaded: boolean;
  format: string;
  bytes: number;
  triangleCount: number;
  partCount: number;
  instanceCount: number;
  assemblyDepth: number;
  identifiedNodeCount: number;
  boundingBox: { min: number[]; max: number[] } | null;
  unit: string;
  upAxis: string;
  warnings: string[];
}

export class Vektor3dError extends Error {
  constructor(message: string, readonly code: string = "", readonly status = 0) {
    super(message);
  }
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getPairingToken();
  let resp: Response;
  try {
    resp = await fetch(`${getBase()}${path}`, {
      ...init,
      headers: {
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...(token ? { "X-Vektor-Token": token } : {}),
        ...(init.headers as Record<string, string> | undefined),
      },
    });
  } catch (e) {
    // fetch 直接抛错 = 连不上：没装、没启动、端口不对，或被浏览器的私有网络策略拦下。
    // 这与「连上了但被拒绝」是完全不同的两件事，提示必须分开。
    throw new Vektor3dError(
      `连不上 vektor3d（${getBase()}）。请确认桌面端已启动，或在下方设置里改地址。`,
      "unreachable"
    );
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok || data?.success === false) {
    throw new Vektor3dError(
      data?.message || `vektor3d 返回 HTTP ${resp.status}`,
      data?.code || "",
      resp.status
    );
  }
  return data as T;
}

/** 探活。不鉴权——「拿不到响应」与「被拒绝」要给用户完全不同的提示。 */
export function health(): Promise<HealthInfo> {
  return call<HealthInfo>("/v1/health");
}

export async function capabilities(): Promise<CapabilityInfo[]> {
  const data = await call<{ capabilities: CapabilityInfo[] }>("/v1/capabilities");
  return data.capabilities ?? [];
}

export async function submitJob(
  capabilityId: string,
  input: Record<string, unknown>,
  idempotencyKey?: string
): Promise<string> {
  const data = await call<{ jobId: string }>("/v1/jobs", {
    method: "POST",
    body: JSON.stringify({ capabilityId, input, idempotencyKey }),
  });
  return data.jobId;
}

export function getJob<T>(jobId: string): Promise<JobDetail<T>> {
  return call<JobDetail<T>>(`/v1/jobs/${encodeURIComponent(jobId)}`);
}

export function cancelJob(jobId: string): Promise<{ canceled: boolean }> {
  return call<{ canceled: boolean }>(`/v1/jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST",
  });
}

/**
 * 提交并等到终态。
 *
 * 用轮询而不是 SSE：`GET /v1/jobs/{id}` 返回的 progress 是**全量数组**，
 * 轮询拿到的信息与订阅完全一样，而 EventSource 设不了 X-Vektor-Token 头
 * （配对令牌一启用就用不了）。转换本身是分钟级的，2 秒一次的延迟无关紧要。
 */
export async function runJob<T>(
  capabilityId: string,
  input: Record<string, unknown>,
  opts: {
    onProgress?: (p: JobProgress | null, job: JobDetail<T>) => void;
    intervalMs?: number;
    signal?: AbortSignal;
    idempotencyKey?: string;
  } = {}
): Promise<T> {
  const jobId = await submitJob(capabilityId, input, opts.idempotencyKey);
  const interval = opts.intervalMs ?? 2000;
  let seen = 0;
  for (;;) {
    if (opts.signal?.aborted) {
      await cancelJob(jobId).catch(() => undefined);
      throw new Vektor3dError("已取消", "canceled");
    }
    await new Promise((r) => setTimeout(r, interval));
    const job = await getJob<T>(jobId);
    if (job.progress.length > seen) {
      // 逐条上报新增的进度，而不是只报最后一条：两次轮询之间能力侧可能已经推进
      // 好几个阶段（网格化按 5% 一档回报，一个 2 秒窗口里就有十几条），只取末条
      // 会把中间阶段悄悄丢掉——而时间线的价值正在于"哪一步花了多久"。
      for (let i = seen; i < job.progress.length; i += 1) {
        opts.onProgress?.(job.progress[i], job);
      }
      seen = job.progress.length;
    } else if (job.status === "queued") {
      opts.onProgress?.(null, job);
    }
    if (job.status === "succeeded") return job.result as T;
    if (job.status === "failed") throw new Vektor3dError(job.error || "转换失败", "job_failed");
    if (job.status === "canceled") throw new Vektor3dError("转换已取消", "canceled");
  }
}

export const vektor3d = {
  getBase,
  setBase,
  getPairingToken,
  setPairingToken,
  health,
  capabilities,
  submitJob,
  getJob,
  cancelJob,
  runJob,
};
