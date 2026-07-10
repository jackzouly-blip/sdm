import { http, getToken } from "./client";
export { errMsg } from "./client";
import type {
  D3plotFindResult,
  D3plotManifest,
  D3plotPrepareResult,
  ExtractRule,
  ExtractRuleInput,
  JobDetail,
  JobSummary,
  JobTemplate,
  TemplateKind,
  TrialSubmitResult,
  TrialOutput,
  UserPolicy,
  SubmitResult,
  ListResponse,
  LoginResponse,
  MeResponse,
  NetdiskPreview,
  PackageRequest,
  PreviewResponse,
  TaskSnapshot,
} from "./types";

export const api = {
  // --- 认证 ---
  async login(username: string, password: string): Promise<LoginResponse> {
    const { data } = await http.post<LoginResponse>("/auth/login", {
      username,
      password,
    });
    return data;
  },
  async me(): Promise<MeResponse> {
    const { data } = await http.get<MeResponse>("/auth/me");
    return data;
  },

  // 服务器当前全局 IPv6 地址（ISP 前缀变化时随之更新）；无则返回 null
  async systemIpv6(): Promise<string | null> {
    const { data } = await http.get<{ ipv6: string | null }>("/system/ipv6");
    return data.ipv6;
  },

  // --- 任务（PBS）---
  async listJobs(state?: "active" | "done"): Promise<JobSummary[]> {
    const { data } = await http.get<JobSummary[]>("/jobs", {
      params: state ? { state } : undefined,
    });
    return data;
  },
  async getJob(jobid: string): Promise<JobDetail> {
    const { data } = await http.get<JobDetail>(
      `/jobs/${encodeURIComponent(jobid)}`
    );
    return data;
  },
  // 终止运行/排队中的任务（qdel）
  async cancelJob(jobid: string): Promise<{ jobid: string; cancelled: boolean }> {
    const { data } = await http.post<{ jobid: string; cancelled: boolean }>(
      `/jobs/${encodeURIComponent(jobid)}/cancel`
    );
    return data;
  },
  // 撤回一个尚未提交到 PBS 的本地排队项（未占用 PBS 资源，无需 qdel）
  async cancelQueuedSubmission(queueId: number): Promise<{ id: number; cancelled: boolean }> {
    const { data } = await http.post<{ id: number; cancelled: boolean }>(
      `/jobs/queue/${queueId}/cancel`
    );
    return data;
  },
  // 删除一条“提交失败”的本地排队记录（仅 failed 状态可删）
  async deleteFailedSubmission(queueId: number): Promise<{ id: number; deleted: boolean }> {
    const { data } = await http.delete<{ id: number; deleted: boolean }>(
      `/jobs/queue/${queueId}`
    );
    return data;
  },
  // 预览将要上传到网盘的结果文件清单与总大小（不触发上传）
  async netdiskPreview(jobid: string): Promise<NetdiskPreview> {
    const { data } = await http.get<NetdiskPreview>(
      `/jobs/${encodeURIComponent(jobid)}/netdisk-preview`
    );
    return data;
  },
  // 将任务结果（h3d/d3plot/binout/d3hsp）上传百度网盘并生成分享链接
  async netdiskShare(jobid: string): Promise<{ task_id: string; jobid: string }> {
    const { data } = await http.post<{ task_id: string; jobid: string }>(
      `/jobs/${encodeURIComponent(jobid)}/netdisk-share`
    );
    return data;
  },

  // --- 统计（管理员）---
  // 按起止日期统计每个用户的 CPU 机时
  async cpuHoursStats(
    start: string,
    end: string
  ): Promise<{
    start: string;
    end: string;
    rows: { user: string; cpu_hours: number; job_count: number }[];
    total_cpu_hours: number;
  }> {
    const { data } = await http.get("/stats/cpu-hours", { params: { start, end } });
    return data;
  },
  // 任务清单（区间内每个作业的明细，含开始/结束时间、机时）；userFilter 非空只导该用户
  async jobsStatList(
    start: string,
    end: string,
    userFilter?: string
  ): Promise<{
    count: number;
    rows: {
      jobid: string;
      short_id: string;
      user: string;
      queue: string;
      name: string;
      cores: number;
      start_ts: number;
      end_ts: number | null;
      hours: number;
      cpu_hours: number;
      state: string;
      exit_status: string | null;
    }[];
  }> {
    const { data } = await http.get("/stats/jobs", {
      params: { start, end, ...(userFilter ? { user_filter: userFilter } : {}) },
    });
    return data;
  },

  // --- 文件浏览 ---
  async fsRoots(): Promise<string[]> {
    const { data } = await http.get<string[]>("/fs/roots");
    return data;
  },
  async listDir(path: string): Promise<ListResponse> {
    const { data } = await http.get<ListResponse>("/fs/list", {
      params: { path },
    });
    return data;
  },
  async makeDir(parent: string, name: string): Promise<{ path: string }> {
    const { data } = await http.post<{ path: string }>("/fs/mkdir", {
      parent,
      name,
    });
    return data;
  },
  async rename(path: string, newName: string): Promise<{ path: string }> {
    const { data } = await http.post<{ path: string }>("/fs/rename", {
      path,
      new_name: newName,
    });
    return data;
  },
  async deletePath(path: string): Promise<void> {
    await http.delete("/fs/delete", { params: { path } });
  },
  // 递归查找目录下指定扩展名文件（相对路径），供提交作业选输入文件
  async findFiles(dir: string, exts = "k,key"): Promise<string[]> {
    const { data } = await http.get<string[]>("/fs/find-files", {
      params: { dir, exts },
    });
    return data;
  },
  async uploadFile(
    parent: string,
    file: File,
    onProgress?: (loaded: number, total: number) => void,
    relpath?: string
  ): Promise<{ path: string }> {
    const form = new FormData();
    form.append("parent", parent);
    form.append("file", file);
    if (relpath) form.append("relpath", relpath); // 目录上传保留层级
    const { data } = await http.post<{ path: string }>("/fs/upload", form, {
      onUploadProgress: onProgress
        ? (e) => onProgress(e.loaded, e.total || file.size || 0)
        : undefined,
    });
    return data;
  },
  // 目录收藏（按登录账号）
  async listFavorites(): Promise<string[]> {
    const { data } = await http.get<string[]>("/fs/favorites");
    return data;
  },
  async addFavorite(path: string): Promise<void> {
    await http.post("/fs/favorites", { path });
  },
  async removeFavorite(path: string): Promise<void> {
    await http.delete("/fs/favorites", { params: { path } });
  },
  async preview(path: string): Promise<PreviewResponse> {
    const { data } = await http.get<PreviewResponse>("/fs/preview", {
      params: { path },
    });
    return data;
  },
  // 估算打包总大小与分卷数(供打包对话框预显)
  async archiveEstimate(
    paths: string[]
  ): Promise<{ total_bytes: number; volume_bytes: number; est_volumes: number }> {
    const { data } = await http.post("/fs/archive-estimate", { paths });
    return data as { total_bytes: number; volume_bytes: number; est_volumes: number };
  },
  // 打包下载到本地：选中文件/目录在服务器打成标准 ZIP 流式回传，浏览器保存。
  async downloadArchive(paths: string[], archive?: string): Promise<void> {
    const resp = await http.post(
      "/fs/download-archive",
      { paths, archive },
      { responseType: "blob" }
    );
    const blob = resp.data as Blob;
    const cd = resp.headers["content-disposition"] as string | undefined;
    const m = cd?.match(/filename="?([^"]+)"?/);
    const filename = m?.[1] ?? archive ?? "download.zip";
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  },
  // Office 文档转 PDF(服务器 LibreOffice)，返回 PDF Blob 用于内嵌预览。
  async officePdfBlob(path: string): Promise<Blob> {
    const resp = await http.get("/fs/office-pdf", {
      params: { path },
      responseType: "blob",
    });
    return resp.data as Blob;
  },
  // 取文件原始字节为 Blob（用于图片/PDF 预览），鉴权由 axios 处理。
  async previewBlob(path: string): Promise<Blob> {
    const resp = await http.get("/fs/download", {
      params: { path },
      responseType: "blob",
    });
    return resp.data as Blob;
  },
  // 下载：走页面内 XHR(与 3D 预览取 model.bin 完全同一条 axios 链路)，
  // 用 onDownloadProgress 实时回传速率/进度。下载完成后用 Blob 触发保存。
  // 注意：整文件先驻留浏览器内存(Blob)，仅适合中小文件；超大文件需另走流式落盘。
  async download(
    path: string,
    onProgress?: (p: { loaded: number; total: number; rate: number }) => void
  ): Promise<void> {
    const resp = await http.get("/fs/download", {
      params: { path },
      responseType: "blob",
      onDownloadProgress: (e) => {
        if (onProgress)
          onProgress({
            loaded: e.loaded,
            total: e.total ?? 0,
            rate: (e as { rate?: number }).rate ?? 0,
          });
      },
    });
    const blob = resp.data as Blob;
    const cd = String(
      (resp.headers as Record<string, unknown>)["content-disposition"] ?? ""
    );
    const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
    const filename = m
      ? decodeURIComponent(m[1])
      : path.split("/").filter(Boolean).pop() || "download";
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  },
  // 打包下载(tar)：服务器边打边传、不落临时包。走页面内 XHR(同预览的快链路)，
  // onDownloadProgress 实时回传速率。tar 总大小未知，故只报已下载字节与速率。
  // 同样会先驻留浏览器内存，超大打包需另走流式落盘方案。
  async downloadArchiveStream(
    paths: string[],
    name?: string,
    onProgress?: (p: { loaded: number; rate: number }) => void
  ): Promise<void> {
    const safe = name ?? "download";
    const resp = await http.get("/fs/download-archive-stream", {
      params: { paths, name: safe },
      paramsSerializer: { indexes: null }, // paths=a&paths=b（不带 [] 下标）
      responseType: "blob",
      onDownloadProgress: (e) => {
        if (onProgress)
          onProgress({
            loaded: e.loaded,
            rate: (e as { rate?: number }).rate ?? 0,
          });
      },
    });
    const blob = resp.data as Blob;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${safe}.tar`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  },

  // --- 打包上传 ---
  async createPackage(req: PackageRequest): Promise<{ task_id: string }> {
    const { data } = await http.post<{ task_id: string }>("/package", req);
    return data;
  },
  async listTasks(): Promise<TaskSnapshot[]> {
    const { data } = await http.get<TaskSnapshot[]>("/tasks");
    return data;
  },
  async getTask(taskId: string): Promise<TaskSnapshot> {
    const { data } = await http.get<TaskSnapshot>(
      `/tasks/${encodeURIComponent(taskId)}`
    );
    return data;
  },

  // --- 数据提取规则 ---
  async listRules(): Promise<ExtractRule[]> {
    const { data } = await http.get<ExtractRule[]>("/extract/rules");
    return data;
  },
  async createRule(input: ExtractRuleInput): Promise<ExtractRule> {
    const { data } = await http.post<ExtractRule>("/extract/rules", input);
    return data;
  },
  async updateRule(
    id: number,
    patch: Partial<ExtractRuleInput>
  ): Promise<ExtractRule> {
    const { data } = await http.put<ExtractRule>(
      `/extract/rules/${id}`,
      patch
    );
    return data;
  },
  async deleteRule(id: number): Promise<void> {
    await http.delete(`/extract/rules/${id}`);
  },
  async runExtract(jobid: string): Promise<{ jobid: string; dispatched: number }> {
    const { data } = await http.post<{ jobid: string; dispatched: number }>(
      `/extract/jobs/${encodeURIComponent(jobid)}/run`
    );
    return data;
  },
  // 清理任务工作目录下的 disk* / mes* / scr* 临时文件
  async cleanupJob(
    jobid: string
  ): Promise<{ deleted: string[]; count: number; freed_bytes: number; errors: string[] }> {
    const { data } = await http.post(
      `/jobs/${encodeURIComponent(jobid)}/cleanup`
    );
    return data as {
      deleted: string[];
      count: number;
      freed_bytes: number;
      errors: string[];
    };
  },

  // --- d3plot 网页可视化 ---
  async d3plotFind(dir: string): Promise<D3plotFindResult> {
    const { data } = await http.get<D3plotFindResult>("/d3plot/find", {
      params: { dir },
    });
    return data;
  },
  async d3plotPrepare(
    path: string,
    maxStates?: number,
    maxTris?: number
  ): Promise<D3plotPrepareResult> {
    const { data } = await http.post<D3plotPrepareResult>("/d3plot/prepare", {
      path,
      max_states: maxStates,
      max_tris: maxTris,
    });
    return data;
  },
  async d3plotManifest(key: string): Promise<D3plotManifest> {
    const { data } = await http.get<D3plotManifest>(
      `/d3plot/asset/${key}/model.json`
    );
    return data;
  },
  async d3plotBin(
    key: string,
    onProgress?: (loaded: number, total: number) => void
  ): Promise<ArrayBuffer> {
    const { data } = await http.get(`/d3plot/asset/${key}/model.bin`, {
      responseType: "arraybuffer",
      onDownloadProgress: onProgress
        ? (e) => onProgress(e.loaded, e.total || 0)
        : undefined,
    });
    return data as ArrayBuffer;
  },
  // 按需下载某个云图场文件（如 field_1.bin）
  async d3plotField(key: string, file: string): Promise<ArrayBuffer> {
    const { data } = await http.get(`/d3plot/asset/${key}/${file}`, {
      responseType: "arraybuffer",
    });
    return data as ArrayBuffer;
  },

  // --- 作业提交模板（管理员）+ 提交作业 ---
  async listTemplates(): Promise<JobTemplate[]> {
    const { data } = await http.get<JobTemplate[]>("/templates");
    return data;
  },
  async createTemplate(
    name: string,
    content: string,
    kind: TemplateKind = "pbs"
  ): Promise<JobTemplate> {
    const { data } = await http.post<JobTemplate>("/templates", { name, content, kind });
    return data;
  },
  async updateTemplate(
    id: number,
    name: string,
    content: string,
    kind: TemplateKind = "pbs"
  ): Promise<JobTemplate> {
    const { data } = await http.put<JobTemplate>(`/templates/${id}`, { name, content, kind });
    return data;
  },
  async deleteTemplate(id: number): Promise<void> {
    await http.delete(`/templates/${id}`);
  },

  // --- 用户提交策略（管理员）---
  async listUserPolicies(): Promise<UserPolicy[]> {
    const { data } = await http.get<UserPolicy[]>("/admin/user-policies");
    return data;
  },
  async upsertUserPolicy(
    user: string,
    payload: { max_concurrent: number | null; priority: number }
  ): Promise<UserPolicy> {
    const { data } = await http.put<UserPolicy>(
      `/admin/user-policies/${encodeURIComponent(user)}`,
      payload
    );
    return data;
  },
  async deleteUserPolicy(user: string): Promise<void> {
    await http.delete(`/admin/user-policies/${encodeURIComponent(user)}`);
  },

  async submitJob(payload: {
    name: string;
    cores: number;
    init_dir: string;
    input_file: string;
    queue?: string;
    template_id?: number;
    script?: string;
  }): Promise<SubmitResult> {
    const { data } = await http.post<SubmitResult>("/jobs/submit", payload);
    return data;
  },

  // --- 试算（管理节点直跑，不进 PBS）---
  async submitTrial(payload: {
    name: string;
    init_dir: string;
    input_file: string;
    template_id: number;
  }): Promise<TrialSubmitResult> {
    const { data } = await http.post<TrialSubmitResult>("/trials/submit", payload);
    return data;
  },
  // 增量拉取试算命令输出（带上次 offset）
  async trialOutput(id: number, offset = 0): Promise<TrialOutput> {
    const { data } = await http.get<TrialOutput>(
      `/trials/${id}/output`,
      { params: { offset } }
    );
    return data;
  },
  // 中断运行中的试算
  async cancelTrial(id: number): Promise<{ id: number; cancelled: boolean }> {
    const { data } = await http.post<{ id: number; cancelled: boolean }>(
      `/trials/${id}/cancel`
    );
    return data;
  },
};

// 任务进度轮询：每 intervalMs 拉取一次快照，终态(成功/失败/中断)自动停止。
// 返回停止函数（组件卸载或提前结束时调用）。用轮询替代 WebSocket，使 d3plot/打包
// 等核心功能不依赖 WS，简化外网部署（只需反代 /api）。
export function pollTask(
  taskId: string,
  onUpdate: (s: TaskSnapshot) => void,
  opts: { intervalMs?: number; onError?: (e: unknown) => void } = {}
): () => void {
  const interval = opts.intervalMs ?? 1500;
  const TERMINAL = ["success", "failed", "interrupted"];
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const stop = () => {
    stopped = true;
    if (timer) clearTimeout(timer);
  };
  const tick = async () => {
    if (stopped) return;
    try {
      const s = await api.getTask(taskId);
      if (stopped) return;
      onUpdate(s);
      if (TERMINAL.includes(s.status)) {
        stop();
        return;
      }
    } catch (e) {
      if (stopped) return;
      opts.onError?.(e);
      // 瞬时网络错误：不中断，继续下一轮重试
    }
    timer = setTimeout(tick, interval);
  };
  void tick();
  return stop;
}

// 在线 shell WebSocket：token 同样经 query 传递，仅管理员可连。
export function shellWs(): WebSocket {
  const token = getToken() ?? "";
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws/shell?token=${encodeURIComponent(
    token
  )}`;
  return new WebSocket(url);
}
