import { http, getToken } from "./client";
export { errMsg } from "./client";
import type {
  SimTemplateRelease,
  SimProjectTemplateRef,
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
  MoveResult,
  NetdiskPreview,
  NetdiskShare,
  NetdiskShareFile,
  NetdiskShareInput,
  NetdiskSyncStatus,
  NetdiskCredStatus,
  NetdiskCredTestResult,
  ShareBatch,
  SharePreview,
  NodesResponse,
  PackageRequest,
  DagDoc,
  NodeRun,
  NodeTypeDef,
  PipelineDef,
  PipelineRun,
  PreviewResponse,
  SimJob,
  SimProject,
  SimProjectInput,
  SimGeometry,
  SimConvertTicket,
  SimMesh,
  SimMeshTicket,
  SimQualityCard,
  SimMaterial,
  SimMaterialDetail,
  SimMaterialImportReport,
  SimMaterialTemplate,
  SimMaterialTemplateDetail,
  SimControlTemplate,
  SimTemplateCheck,
  SimTemplateParseTicket,
  SimAirbagTicket,
  SimQualityCardDetail,
  SimQualityTemplate,
  SimRequirementDoc,
  SimRequirementItem,
  SimExtractSummary,
  SimAnalyzeTicket,
  SimAiSession,
  SimAiMessage,
  SimAiProposal,
  SimResult,
  SimSubject,
  SimTarget,
  SimTemplate,
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

  // --- 计算节点监控（管理员）---
  // 采集各计算节点状态（pbsnodes -a）
  async listNodes(): Promise<NodesResponse> {
    const { data } = await http.get<NodesResponse>("/nodes");
    return data;
  },
  // ssh 到指定节点重启 pbs 服务（pbs_mom / trqauthd）
  async restartNodeServices(
    name: string
  ): Promise<{ node: string; restarted: boolean; services: string[] }> {
    const { data } = await http.post<{
      node: string;
      restarted: boolean;
      services: string[];
    }>(`/nodes/${encodeURIComponent(name)}/restart-services`);
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
  /** 把一批文件/目录移动到 dst_dir。跨文件系统时返回 task_id，需配合 pollTask。 */
  async movePaths(paths: string[], dstDir: string): Promise<MoveResult> {
    const { data } = await http.post<MoveResult>("/fs/move", {
      paths,
      dst_dir: dstDir,
    });
    return data;
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

  // --- 网盘数据管理（入站：客户分享链接 → 集群）---
  async netdiskSyncStatus(): Promise<NetdiskSyncStatus> {
    const { data } = await http.get<NetdiskSyncStatus>("/netdisk/status");
    return data;
  },
  /** 配置平台账号的网页 cookie（仅管理员）。存库，改完立即生效、无需重启。 */
  async setNetdiskCredentials(body: {
    bduss: string;
    stoken: string;
  }): Promise<NetdiskCredStatus> {
    const { data } = await http.put<NetdiskCredStatus>("/netdisk/credentials", body);
    return data;
  },
  async clearNetdiskCredentials(): Promise<NetdiskCredStatus> {
    const { data } = await http.delete<NetdiskCredStatus>("/netdisk/credentials");
    return data;
  },
  /** 当场验证凭据是否有效（不需要分享链接）。 */
  async testNetdiskCredentials(): Promise<NetdiskCredTestResult> {
    const { data } = await http.post<NetdiskCredTestResult>(
      "/netdisk/credentials/test"
    );
    return data;
  },
  /** 落库前先验一次链接与提取码，并列出该层目录供挑子目录。 */
  async previewShare(body: {
    share_url: string;
    pwd: string;
    sub_dir?: string;
  }): Promise<SharePreview> {
    const { data } = await http.post<SharePreview>("/netdisk/preview", body);
    return data;
  },
  async listShares(): Promise<NetdiskShare[]> {
    const { data } = await http.get<NetdiskShare[]>("/netdisk/shares");
    return data;
  },
  async createShare(input: NetdiskShareInput): Promise<NetdiskShare> {
    const { data } = await http.post<NetdiskShare>("/netdisk/shares", input);
    return data;
  },
  async updateShare(
    id: number,
    patch: Partial<NetdiskShareInput>
  ): Promise<NetdiskShare> {
    const { data } = await http.patch<NetdiskShare>(`/netdisk/shares/${id}`, patch);
    return data;
  },
  async deleteShare(id: number): Promise<void> {
    await http.delete(`/netdisk/shares/${id}`);
  },
  /** 手动触发同步；返回 task_id，配合 pollTask 拿进度（与打包/d3plot 同构）。 */
  async syncShare(id: number): Promise<{ task_id: string }> {
    const { data } = await http.post<{ task_id: string }>(
      `/netdisk/shares/${id}/sync`
    );
    return data;
  },
  /**
   * 重新拉取单个文件并覆盖本地，返回 task_id。
   *
   * 比整源同步快——只列该文件所在的一层目录。客户在网盘上换了新版本时
   * fs_id 会变，后端按文件名重新定位，所以这个调用对"更新"和"补一份"都适用。
   */
  async resyncShareFile(
    shareId: number,
    fsId: string
  ): Promise<{ task_id: string }> {
    const { data } = await http.post<{ task_id: string }>(
      `/netdisk/shares/${shareId}/files/${encodeURIComponent(fsId)}/resync`
    );
    return data;
  },
  async listShareFiles(id: number, state?: string): Promise<NetdiskShareFile[]> {
    const { data } = await http.get<NetdiskShareFile[]>(
      `/netdisk/shares/${id}/files`,
      { params: state ? { state } : undefined }
    );
    return data;
  },
  async listShareBatches(id: number): Promise<ShareBatch[]> {
    const { data } = await http.get<ShareBatch[]>(`/netdisk/shares/${id}/batches`);
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

/**
 * 模板解析票据 → vektor3d 能直接用的入参。
 *
 * 与 meshTicket 同法把相对路径拼成绝对 URL：票据要交给**桌面上的 vektor3d**，
 * 它和浏览器不在同一个进程里，相对路径对它没有意义。
 */
async function templateParseTicket(
  seg: "material-templates" | "control-templates",
  tid: string,
  kind: "material" | "control"
): Promise<SimTemplateParseTicket> {
  const { data } = await http.post<{
    tid: string;
    kind: string;
    token: string;
    expires_in: number;
    source_name: string;
    source_path_suffix: string;
    unit_system: string;
    version: string;
    expected_sha256: string;
  }>(`/sim/${seg}/${tid}/parse-ticket`);
  return {
    tid: data.tid,
    kind,
    token: data.token,
    expiresIn: data.expires_in,
    sourceName: data.source_name,
    sourceUrl: `${window.location.origin}/api${data.source_path_suffix}`,
    unitSystem: data.unit_system,
    version: data.version,
    expectedSha256: data.expected_sha256 || "",
  };
}

/**
 * SDM 仿真设计接口。
 *
 * 单独成组而非平铺进 api：仿真是一个独立的一级 APP，接口数量会持续增长，
 * 与算力管理的接口混在一起会很快失去可读性。
 */
export const simApi = {
  // --- 项目 ---
  async listProjects(status?: string): Promise<SimProject[]> {
    const { data } = await http.get<SimProject[]>("/sim/projects", {
      params: status ? { status_filter: status } : undefined,
    });
    return data;
  },
  async createProject(body: SimProjectInput): Promise<SimProject> {
    const { data } = await http.post<SimProject>("/sim/projects", body);
    return data;
  },
  async getProject(pid: string): Promise<SimProject> {
    const { data } = await http.get<SimProject>(`/sim/projects/${pid}`);
    return data;
  },
  async updateProject(pid: string, body: Partial<SimProjectInput>): Promise<SimProject> {
    const { data } = await http.patch<SimProject>(`/sim/projects/${pid}`, body);
    return data;
  },
  async deleteProject(pid: string): Promise<void> {
    await http.delete(`/sim/projects/${pid}`);
  },

  // --- 分析对象 ---
  async listTargets(pid: string): Promise<SimTarget[]> {
    const { data } = await http.get<SimTarget[]>(`/sim/projects/${pid}/targets`);
    return data;
  },
  async createTarget(
    pid: string,
    body: { name: string; target_type?: string; source_ref?: Record<string, unknown> }
  ): Promise<SimTarget> {
    const { data } = await http.post<SimTarget>(`/sim/projects/${pid}/targets`, body);
    return data;
  },
  async deleteTarget(tid: string): Promise<void> {
    await http.delete(`/sim/targets/${tid}`);
  },

  // --- 几何版本 ---
  async listGeometries(tid: string): Promise<SimGeometry[]> {
    const { data } = await http.get<SimGeometry[]>(`/sim/targets/${tid}/geometries`);
    return data;
  },
  /** 删除几何版本。有工况绑定其网格时后端拒删（409），错误信息里点名工况 */
  async deleteGeometry(gid: string): Promise<void> {
    await http.delete(`/sim/geometries/${gid}`);
  },
  /** 上传 CAD 数模，建立一个几何版本。onProgress 用于大文件进度显示。 */
  async uploadGeometry(
    tid: string,
    file: File,
    onProgress?: (pct: number) => void
  ): Promise<SimGeometry> {
    const form = new FormData();
    form.append("file", file);
    const { data } = await http.post<SimGeometry>(
      `/sim/targets/${tid}/geometries/upload`,
      form,
      {
        onUploadProgress: (e) => {
          if (onProgress && e.total) onProgress(Math.round((e.loaded / e.total) * 100));
        },
      }
    );
    return data;
  },
  /**
   * 从集群已有路径建几何版本。
   *
   * 求解器 deck 只能走这条路：主控 .key 会牵出几百 MB 的 include 树、散在集群
   * 目录里，浏览器传不上来，而它本就在集群上。deck 会自动转 GLB 供网页渲染。
   */
  async addGeometryFromPath(
    tid: string,
    path: string,
    opts?: { convert?: boolean; max_triangles?: number }
  ): Promise<SimGeometry & { convert_task_id: string | null }> {
    const { data } = await http.post<SimGeometry & { convert_task_id: string | null }>(
      `/sim/targets/${tid}/geometries/from-path`,
      { path, ...opts }
    );
    return data;
  },
  /** 轻量化产物的下载地址；token 经 query 传递，供 three.js GLTFLoader 直接加载。 */
  lightweightUrl(gid: string): string {
    return `/api/sim/geometries/${gid}/lightweight?token=${encodeURIComponent(
      getToken() ?? ""
    )}`;
  },
  sourceDownloadUrl(gid: string): string {
    return `/api/sim/geometries/${gid}/download?token=${encodeURIComponent(
      getToken() ?? ""
    )}`;
  },
  /**
   * 取一次几何转换的受限票据，并拼出 vektor3d 要用的两个绝对地址。
   *
   * 地址在**前端**拼：后端看到的 base_url 开发期是 127.0.0.1:8000（浏览器实际走
   * 5173 的 vite 代理）、生产期是 nginx 反代后的内网地址，都不等于浏览器与桌面
   * 真正能访问到的地址。浏览器最清楚自己是从哪进来的，而 vektor3d 就在同一台机器上。
   */
  async convertTicket(gid: string, ttlSeconds = 1800): Promise<SimConvertTicket> {
    const { data } = await http.post<{
      gid: string;
      token: string;
      expires_in: number;
      source_name: string;
      source_path_suffix: string;
      upload_path_suffix: string;
    }>(`/sim/geometries/${gid}/convert-ticket`, null, {
      params: { ttl_seconds: ttlSeconds },
    });
    const origin = window.location.origin;
    return {
      gid: data.gid,
      token: data.token,
      expiresIn: data.expires_in,
      sourceName: data.source_name,
      sourceUrl: `${origin}/api${data.source_path_suffix}`,
      uploadUrl: `${origin}/api${data.upload_path_suffix}`,
    };
  },

  // --- 网格（契约 2.3~2.11）---

  /** 取单个几何版本（网格作业进行中反复刷它读分析结果，不必拉整个列表） */
  async getGeometry(gid: string): Promise<SimGeometry> {
    const { data } = await http.get<SimGeometry>(`/sim/geometries/${gid}`);
    return data;
  },
  async listMeshes(gid: string): Promise<SimMesh[]> {
    const { data } = await http.get<SimMesh[]>(`/sim/geometries/${gid}/meshes`);
    return data;
  },
  /**
   * 登记一个网格版本。能力作业动辄几十分钟，先落 status='generating' 的一行，
   * 页面才能显示进度；产物与终态由 vektor3d 回传 + updateMesh 补齐。
   */
  async addMesh(
    gid: string,
    body: {
      mesh_type: string;
      mesh_engine?: string;
      mesh_params?: Record<string, unknown>;
      status?: string;
      part_filter?: string | null;
      source_mesh_ids?: string[] | null;
    }
  ): Promise<SimMesh> {
    const { data } = await http.post<SimMesh>(`/sim/geometries/${gid}/meshes`, body);
    return data;
  },
  async updateMesh(
    gid: string,
    mid: string,
    body: { status?: string; quality?: Record<string, unknown> }
  ): Promise<SimMesh> {
    const { data } = await http.patch<SimMesh>(`/sim/geometries/${gid}/meshes/${mid}`, body);
    return data;
  },
  /** 回写 mesh.inventory / mesh.classify 的产出（落在几何版本上） */
  async setGeometryAnalysis(
    gid: string,
    body: { part_inventory?: unknown[]; mesh_strategy?: Record<string, unknown> }
  ): Promise<SimGeometry> {
    const { data } = await http.put<SimGeometry>(`/sim/geometries/${gid}/analysis`, body);
    return data;
  },
  /**
   * 取网格作业票据并拼出 vektor3d 要用的绝对地址（理由同 convertTicket）。
   * 默认 2 小时：网格作业比几何转换慢一个量级，票据先过期会让回传功亏一篑。
   */
  async meshTicket(gid: string, ttlSeconds = 7200): Promise<SimMeshTicket> {
    const { data } = await http.post<{
      gid: string;
      token: string;
      expires_in: number;
      source_name: string;
      source_path_suffix: string;
      mesh_path_prefix: string;
    }>(`/sim/geometries/${gid}/mesh-ticket`, null, { params: { ttl_seconds: ttlSeconds } });
    const origin = window.location.origin;
    return {
      gid: data.gid,
      token: data.token,
      expiresIn: data.expires_in,
      sourceName: data.source_name,
      sourceUrl: `${origin}/api${data.source_path_suffix}`,
      meshUrlPrefix: `${origin}/api${data.mesh_path_prefix}`,
    };
  },
  /** 网格产物地址。kind ∈ ansa/solver/preview/report；token 走 query 供 GLTFLoader 直载 */
  /** 气囊网格化票据。与 meshTicket 同法把相对路径拼成绝对 URL —— 票据要交给
   *  桌面上的 vektor3d，相对路径对它没有意义。 */
  async airbagTicket(gid: string, ttlSeconds = 3600): Promise<SimAirbagTicket> {
    const { data } = await http.post<{
      gid: string;
      token: string;
      expires_in: number;
      source_name: string;
      source_path_suffix: string;
      deck_path_suffix: string;
    }>(`/sim/geometries/${gid}/airbag-ticket`, null, { params: { ttl_seconds: ttlSeconds } });
    const origin = window.location.origin;
    return {
      gid: data.gid,
      token: data.token,
      expiresIn: data.expires_in,
      sourceName: data.source_name,
      sourceUrl: `${origin}/api${data.source_path_suffix}`,
      deckUploadUrl: `${origin}/api${data.deck_path_suffix}`,
    };
  },
  meshArtifactUrl(gid: string, mid: string, kind: string): string {
    return `/api/sim/geometries/${gid}/meshes/${mid}/artifact/${kind}?token=${encodeURIComponent(
      getToken() ?? ""
    )}`;
  },
  async checkoutMesh(gid: string, mid: string, checkoutId: string): Promise<SimMesh> {
    const { data } = await http.post<SimMesh>(
      `/sim/geometries/${gid}/meshes/${mid}/checkout`,
      { checkout_id: checkoutId }
    );
    return data;
  },
  async checkinMesh(gid: string, mid: string): Promise<SimMesh> {
    const { data } = await http.post<SimMesh>(`/sim/geometries/${gid}/meshes/${mid}/checkin`);
    return data;
  },

  // --- 客户需求文档 ---
  async listRequirements(pid: string): Promise<SimRequirementDoc[]> {
    const { data } = await http.get<SimRequirementDoc[]>(`/sim/projects/${pid}/requirements`);
    return data;
  },
  async uploadRequirement(
    pid: string,
    file: File,
    docType = "spec",
    onProgress?: (pct: number) => void
  ): Promise<SimRequirementDoc> {
    const form = new FormData();
    form.append("file", file);
    form.append("doc_type", docType);
    const { data } = await http.post<SimRequirementDoc>(
      `/sim/projects/${pid}/requirements/upload`,
      form,
      {
        onUploadProgress: (e) => {
          if (onProgress && e.total) onProgress(Math.round((e.loaded / e.total) * 100));
        },
      }
    );
    return data;
  },
  async deleteRequirement(rid: string): Promise<void> {
    await http.delete(`/sim/requirements/${rid}`);
  },
  requirementDownloadUrl(rid: string): string {
    return `/api/sim/requirements/${rid}/download?token=${encodeURIComponent(getToken() ?? "")}`;
  },

  /** 解析需求文档 → 需求条目（确定性规则，重解析整体替换） */
  async extractRequirementItems(
    rid: string
  ): Promise<{ summary: SimExtractSummary; items: SimRequirementItem[] }> {
    const { data } = await http.post(`/sim/requirements/${rid}/extract`);
    return data;
  },
  /**
   * 取一次 AI 解析的只读票据，并拼出 vektor3d 要用的下载地址。
   *
   * 与几何票据同理：URL 在前端拼——后端看到的 base_url 在开发/反代下都不是
   * 浏览器与桌面真正能访问的地址。
   */
  async analyzeTicket(rid: string, ttlSeconds = 1800): Promise<SimAnalyzeTicket> {
    const { data } = await http.post<{
      rid: string;
      token: string;
      expires_in: number;
      source_name: string;
      source_path_suffix: string;
    }>(`/sim/requirements/${rid}/analyze-ticket`, null, { params: { ttl_seconds: ttlSeconds } });
    return {
      rid: data.rid,
      token: data.token,
      expiresIn: data.expires_in,
      sourceName: data.source_name,
      sourceUrl: `${window.location.origin}/api${data.source_path_suffix}`,
    };
  },
  /** 把 vektor3d 抽出的条目写回 SDM。整体替换，与规则解析同一语义 */
  async putRequirementItems(
    rid: string,
    items: Record<string, unknown>[],
    summary?: Record<string, unknown>,
    extractor = "ai"
  ): Promise<{ summary: SimExtractSummary; items: SimRequirementItem[] }> {
    const { data } = await http.put(`/sim/requirements/${rid}/items`, { items, summary, extractor });
    return data;
  },
  async listRequirementItems(rid: string): Promise<SimRequirementItem[]> {
    const { data } = await http.get<SimRequirementItem[]>(`/sim/requirements/${rid}/items`);
    return data;
  },
  async updateRequirementItem(
    iid: string,
    body: Partial<Pick<SimRequirementItem, "title" | "raw_text" | "category" | "baseline" | "status">>
      & { needs_clarification?: boolean; clarification_hint?: string }
  ): Promise<SimRequirementItem> {
    const { data } = await http.patch<SimRequirementItem>(`/sim/requirement-items/${iid}`, body);
    return data;
  },

  // --- 质量卡：模板库与项目实例 ---
  async listQualityTemplates(): Promise<SimQualityTemplate[]> {
    const { data } = await http.get<SimQualityTemplate[]>("/sim/quality-templates");
    return data;
  },
  async getQualityTemplate(templateId: string): Promise<SimQualityCardDetail> {
    const { data } = await http.get<SimQualityCardDetail>(`/sim/quality-templates/${templateId}`);
    return data;
  },
  async listQualityCards(pid: string): Promise<SimQualityCard[]> {
    const { data } = await http.get<SimQualityCard[]>(`/sim/projects/${pid}/quality-cards`);
    return data;
  },
  async createQualityCard(
    pid: string,
    body: {
      name: string;
      template_id?: string;
      requirement_doc_id?: string | null;
      overrides?: { target: string; new_value: string; source: string }[];
    }
  ): Promise<SimQualityCard> {
    const { data } = await http.post<SimQualityCard>(`/sim/projects/${pid}/quality-cards`, body);
    return data;
  },
  async getQualityCard(qid: string): Promise<SimQualityCard & { card?: SimQualityCardDetail }> {
    const { data } = await http.get(`/sim/quality-cards/${qid}`);
    return data;
  },
  async deleteQualityCard(qid: string): Promise<void> {
    await http.delete(`/sim/quality-cards/${qid}`);
  },
  /** 导入客户的质量卡文件成为模板。mpar 可省略——有些客户只给判定准则 */
  async importQualityTemplate(body: {
    template_id: string;
    name: string;
    qualFile: File;
    mparFile?: File | null;
    source?: string;
    revision?: string;
    scope?: string;
    description?: string;
  }): Promise<SimQualityCardDetail> {
    const form = new FormData();
    form.append("template_id", body.template_id);
    form.append("name", body.name);
    form.append("qual_file", body.qualFile);
    if (body.mparFile) form.append("mpar_file", body.mparFile);
    for (const k of ["source", "revision", "scope", "description"] as const) {
      if (body[k]) form.append(k, body[k] as string);
    }
    const { data } = await http.post<SimQualityCardDetail>(
      "/sim/quality-templates/import",
      form
    );
    return data;
  },
  /** 改模板元数据（名称/来源/说明等）。阈值不可改——那必须走派生留痕 */
  async updateQualityTemplate(
    templateId: string,
    body: Partial<Pick<SimQualityTemplate, "name" | "source" | "revision" | "scope" | "description">>
  ): Promise<SimQualityTemplate> {
    const { data } = await http.patch<SimQualityTemplate>(
      `/sim/quality-templates/${templateId}`,
      body
    );
    return data;
  },
  /** 删用户模板。既有项目实例不受影响（实例文件派生时已拷走） */
  async deleteQualityTemplate(templateId: string): Promise<void> {
    await http.delete(`/sim/quality-templates/${templateId}`);
  },
  /** 在线改用户模板内容（阈值/网格参数）。每项改动必须带依据，追加进 overrides 留痕 */
  async editQualityTemplateContent(
    templateId: string,
    overrides: { target: string; new_value: string; source: string }[]
  ): Promise<SimQualityCardDetail> {
    const { data } = await http.patch<SimQualityCardDetail>(
      `/sim/quality-templates/${templateId}/content`,
      { overrides }
    );
    return data;
  },
  /** 从既有模板（含内置）派生新的用户模板。内置模板只读，想改它就走这条路 */
  async deriveQualityTemplate(
    baseId: string,
    body: {
      new_id: string;
      name: string;
      overrides?: { target: string; new_value: string; source: string }[];
      source?: string;
      revision?: string;
      scope?: string;
      description?: string;
    }
  ): Promise<SimQualityTemplate> {
    const { data } = await http.post<SimQualityTemplate>(
      `/sim/quality-templates/${baseId}/derive`,
      body
    );
    return data;
  },
  /** 在线编辑实例：改动直接落进 .ansa_qual/.ansa_mpar，每项必须带依据 */
  async editQualityCard(
    qid: string,
    overrides: { target: string; new_value: string; source: string }[]
  ): Promise<SimQualityCard & { card?: SimQualityCardDetail }> {
    const { data } = await http.patch(`/sim/quality-cards/${qid}`, { overrides });
    return data;
  },
  qualityCardExportUrl(qid: string, kind: "qual" | "mpar"): string {
    return `/api/sim/quality-cards/${qid}/export?kind=${kind}&token=${encodeURIComponent(getToken() ?? "")}`;
  },

  // --- 材料库（全局资产；写仅管理员）---
  async listMaterials(params?: {
    category?: string;
    q?: string;
    status?: string;
  }): Promise<SimMaterial[]> {
    const { data } = await http.get<SimMaterial[]>("/sim/materials", { params });
    return data;
  },
  async getMaterial(mid: string): Promise<SimMaterialDetail> {
    const { data } = await http.get<SimMaterialDetail>(`/sim/materials/${mid}`);
    return data;
  },
  /** 导入 LS-DYNA 关键字文件的材料段。幂等：未变跳过，变了整体替换并 revision+1 */
  async importMaterials(
    file: File,
    unitSystem = "t-mm-s",
    solverType = "lsdyna"
  ): Promise<SimMaterialImportReport> {
    const form = new FormData();
    form.append("file", file);
    form.append("unit_system", unitSystem);
    form.append("solver_type", solverType);
    const { data } = await http.post<SimMaterialImportReport>("/sim/materials/import", form);
    return data;
  },
  /** 只改元数据。性能/曲线/卡只能整体走导入——单点改数会让卡原文与结构化参数分叉 */
  async updateMaterial(
    mid: string,
    body: Partial<Pick<SimMaterial, "name" | "category" | "standard_code" | "description" | "source" | "status">>
  ): Promise<SimMaterial> {
    const { data } = await http.patch<SimMaterial>(`/sim/materials/${mid}`, body);
    return data;
  },
  async deleteMaterial(mid: string): Promise<void> {
    await http.delete(`/sim/materials/${mid}`);
  },

  // --- 材料模板文件（组装式）---
  async listMaterialTemplates(params?: { status?: string }): Promise<SimMaterialTemplate[]> {
    const { data } = await http.get<SimMaterialTemplate[]>("/sim/material-templates", { params });
    return data;
  },
  async getMaterialTemplate(tid: string): Promise<SimMaterialTemplateDetail> {
    const { data } = await http.get<SimMaterialTemplateDetail>(`/sim/material-templates/${tid}`);
    return data;
  },
  /** 选卡时先看能不能组装：单位制不一致、MID/LCID 撞车都在这里暴露 */
  async checkMaterialTemplate(cardIds: string[], unitSystem?: string): Promise<SimTemplateCheck> {
    const { data } = await http.post<SimTemplateCheck>(
      "/sim/material-templates/check",
      { card_ids: cardIds },
      { params: unitSystem ? { unit_system: unitSystem } : undefined },
    );
    return data;
  },
  async createMaterialTemplate(body: {
    name: string;
    unit_system: string;
    description?: string;
    card_ids?: string[];
  }): Promise<SimMaterialTemplate> {
    const { data } = await http.post<SimMaterialTemplate>("/sim/material-templates", body);
    return data;
  },
  async setMaterialTemplateItems(tid: string, cardIds: string[]): Promise<SimMaterialTemplate> {
    const { data } = await http.put<SimMaterialTemplate>(
      `/sim/material-templates/${tid}/items`, { card_ids: cardIds });
    return data;
  },
  async updateMaterialTemplate(
    tid: string,
    body: Partial<Pick<SimMaterialTemplate,
      "name" | "description" | "unit_system" | "status" | "summary_json">>,
  ): Promise<SimMaterialTemplate> {
    const { data } = await http.patch<SimMaterialTemplate>(`/sim/material-templates/${tid}`, body);
    return data;
  },
  async deleteMaterialTemplate(tid: string): Promise<void> {
    await http.delete(`/sim/material-templates/${tid}`);
  },
  materialTemplateExportUrl(tid: string): string {
    return `/api/sim/material-templates/${tid}/export`;
  },
  materialParseTicket(tid: string): Promise<SimTemplateParseTicket> {
    return templateParseTicket("material-templates", tid, "material");
  },

  // --- 控制卡模板库（整份存档）---
  async listControlTemplates(params?: { analysis_type?: string; status?: string }):
    Promise<SimControlTemplate[]> {
    const { data } = await http.get<SimControlTemplate[]>("/sim/control-templates", { params });
    return data;
  },
  async getControlTemplate(tid: string): Promise<SimControlTemplate> {
    const { data } = await http.get<SimControlTemplate>(`/sim/control-templates/${tid}`);
    return data;
  },
  async createControlTemplate(body: {
    name: string;
    unit_system: string;
    keyword_text: string;
    analysis_type?: string;
    description?: string;
    source_name?: string;
  }): Promise<SimControlTemplate> {
    const { data } = await http.post<SimControlTemplate>("/sim/control-templates", body);
    return data;
  },
  async updateControlTemplate(
    tid: string,
    body: Partial<Pick<SimControlTemplate,
      "name" | "description" | "analysis_type" | "unit_system" | "status"
      | "keyword_text" | "summary_json">>,
  ): Promise<SimControlTemplate> {
    const { data } = await http.patch<SimControlTemplate>(`/sim/control-templates/${tid}`, body);
    return data;
  },
  async deleteControlTemplate(tid: string): Promise<void> {
    await http.delete(`/sim/control-templates/${tid}`);
  },
  controlTemplateExportUrl(tid: string): string {
    return `/api/sim/control-templates/${tid}/export`;
  },
  controlParseTicket(tid: string): Promise<SimTemplateParseTicket> {
    return templateParseTicket("control-templates", tid, "control");
  },

  // --- 模板发布版本（不可变快照，项目引用它做结算组装）---
  async publishTemplate(kind: "material" | "control", tid: string, note = ""):
    Promise<SimTemplateRelease> {
    const { data } = await http.post<SimTemplateRelease>(
      `/sim/${kind}-templates/${tid}/publish`, null, { params: { note } });
    return data;
  },
  async listTemplateReleases(kind: "material" | "control", tid: string):
    Promise<SimTemplateRelease[]> {
    const { data } = await http.get<SimTemplateRelease[]>(
      `/sim/${kind}-templates/${tid}/releases`);
    return data;
  },
  templateReleaseUrl(rid: string): string {
    return `/api/sim/template-releases/${rid}?download=1`;
  },

  // --- 项目引用模板（通用列表）---
  async listProjectTemplateRefs(pid: string): Promise<SimProjectTemplateRef[]> {
    const { data } = await http.get<SimProjectTemplateRef[]>(`/sim/projects/${pid}/template-refs`);
    return data;
  },
  async addProjectTemplateRef(pid: string, category: string, releaseId: string):
    Promise<SimProjectTemplateRef> {
    const { data } = await http.post<SimProjectTemplateRef>(
      `/sim/projects/${pid}/template-refs`, { category, release_id: releaseId });
    return data;
  },
  async uploadProjectTemplateRef(pid: string, category: string, file: File):
    Promise<SimProjectTemplateRef> {
    const fd = new FormData();
    fd.append("category", category);
    fd.append("file", file);
    const { data } = await http.post<SimProjectTemplateRef>(
      `/sim/projects/${pid}/template-refs/upload`, fd);
    return data;
  },
  async deleteProjectTemplateRef(pid: string, rid: string): Promise<void> {
    await http.delete(`/sim/projects/${pid}/template-refs/${rid}`);
  },
  async createSimJob(sid: string, body?: { submit_mode?: string }): Promise<SimJob> {
    const { data } = await http.post<SimJob>(`/sim/subjects/${sid}/jobs`, body ?? {});
    return data;
  },
  /** 为一次计算实例化独立 run 目录（deck+模板+main.key），提交仍走作业提交页 */
  async materializeJob(jid: string, geometryId: string): Promise<SimJob> {
    const { data } = await http.post<SimJob>(`/sim/jobs/${jid}/materialize`,
      { geometry_id: geometryId });
    return data;
  },

  // --- AI 会话（契约：docs/vektor3d-ai-session-contract.md）---
  /** 项目级只读票据：交给 vektor3d 拉主数据填充 workspace。写永远走提案-确认 */
  async aiReadTicket(pid: string, ttlSeconds = 1800): Promise<{
    pid: string;
    token: string;
    expiresIn: number;
    contextUrl: string;
  }> {
    const { data } = await http.post<{
      pid: string;
      token: string;
      expires_in: number;
      context_path_suffix: string;
    }>(`/sim/projects/${pid}/ai-read-ticket`, null, { params: { ttl_seconds: ttlSeconds } });
    return {
      pid: data.pid,
      token: data.token,
      expiresIn: data.expires_in,
      contextUrl: `${window.location.origin}/api${data.context_path_suffix}`,
    };
  },
  async listAiSessions(pid: string): Promise<SimAiSession[]> {
    const { data } = await http.get<SimAiSession[]>(`/sim/projects/${pid}/ai-sessions`);
    return data;
  },
  async createAiSession(pid: string, title = ""): Promise<SimAiSession> {
    const { data } = await http.post<SimAiSession>(`/sim/projects/${pid}/ai-sessions`, { title });
    return data;
  },
  /** 会话详情含全部消息（消息流是主数据，跨设备可见） */
  async getAiSession(sid: string): Promise<SimAiSession & { messages: SimAiMessage[] }> {
    const { data } = await http.get(`/sim/ai-sessions/${sid}`);
    return data;
  },
  async deleteAiSession(sid: string): Promise<void> {
    await http.delete(`/sim/ai-sessions/${sid}`);
  },
  async addAiMessage(
    sid: string,
    body: {
      role: "user" | "assistant" | "system";
      content: string;
      proposals?: Omit<SimAiProposal, "status" | "decided_by" | "decided_at">[];
      citations?: { text?: string; ref: string }[];
      meta?: Record<string, unknown>;
    }
  ): Promise<SimAiMessage> {
    const { data } = await http.post<SimAiMessage>(`/sim/ai-sessions/${sid}/messages`, body);
    return data;
  },
  /** 裁决一条提案（只翻 UI 状态；数据变更由前端另行调既有接口完成） */
  async decideAiProposal(
    sid: string,
    mid: string,
    index: number,
    decision: "confirmed" | "rejected",
    note = ""
  ): Promise<SimAiMessage> {
    const { data } = await http.post<SimAiMessage>(
      `/sim/ai-sessions/${sid}/messages/${mid}/proposal-decision`,
      { index, decision, note }
    );
    return data;
  },

  // --- 工况 ---
  async listSubjects(pid: string, status?: string): Promise<SimSubject[]> {
    const { data } = await http.get<SimSubject[]>(`/sim/projects/${pid}/subjects`, {
      params: status ? { status_filter: status } : undefined,
    });
    return data;
  },
  async createSubject(
    pid: string,
    body: {
      name: string;
      subject_type: string;
      solver_type: string;
      template_id?: string | null;
      sim_mesh_version_id?: string | null;
      config?: Record<string, unknown>;
    }
  ): Promise<SimSubject> {
    const { data } = await http.post<SimSubject>(`/sim/projects/${pid}/subjects`, body);
    return data;
  },
  async deleteSubject(sid: string): Promise<void> {
    await http.delete(`/sim/subjects/${sid}`);
  },

  // --- 作业与结果 ---
  async listProjectJobs(pid: string): Promise<SimJob[]> {
    const { data } = await http.get<SimJob[]>(`/sim/projects/${pid}/jobs`);
    return data;
  },
  async listProjectResults(pid: string): Promise<SimResult[]> {
    const { data } = await http.get<SimResult[]>(`/sim/projects/${pid}/results`);
    return data;
  },

  // --- 编排：节点类型 / 定义 / 运行 ---
  async listNodeTypes(): Promise<NodeTypeDef[]> {
    const { data } = await http.get<NodeTypeDef[]>("/sim/node-types");
    return data;
  },
  async listPipelines(): Promise<PipelineDef[]> {
    const { data } = await http.get<PipelineDef[]>("/sim/pipelines");
    return data;
  },
  async getPipeline(pid: string): Promise<PipelineDef> {
    const { data } = await http.get<PipelineDef>(`/sim/pipelines/${pid}`);
    return data;
  },
  async createPipeline(body: {
    name: string;
    description?: string | null;
    doc: DagDoc;
  }): Promise<PipelineDef> {
    const { data } = await http.post<PipelineDef>("/sim/pipelines", body);
    return data;
  },
  async updatePipeline(
    pid: string,
    body: { name?: string; description?: string | null; doc?: DagDoc }
  ): Promise<PipelineDef> {
    const { data } = await http.patch<PipelineDef>(`/sim/pipelines/${pid}`, body);
    return data;
  },
  async deletePipeline(pid: string): Promise<void> {
    await http.delete(`/sim/pipelines/${pid}`);
  },
  /** 校验但不保存——画布可在编辑时实时提示环、悬空边等问题 */
  async validatePipeline(doc: DagDoc): Promise<{ ok: boolean; error?: string }> {
    const { data } = await http.post<{ ok: boolean; error?: string }>(
      "/sim/pipelines/validate",
      { doc }
    );
    return data;
  },
  async startRun(
    pid: string,
    body: { sim_project_id?: string | null; sim_subject_id?: string | null }
  ): Promise<PipelineRun> {
    const { data } = await http.post<PipelineRun>(`/sim/pipelines/${pid}/runs`, body);
    return data;
  },
  async listRuns(params?: {
    sim_subject_id?: string;
    active_only?: boolean;
  }): Promise<PipelineRun[]> {
    const { data } = await http.get<PipelineRun[]>("/sim/runs", { params });
    return data;
  },
  async getRun(rid: string): Promise<PipelineRun> {
    const { data } = await http.get<PipelineRun>(`/sim/runs/${rid}`);
    return data;
  },
  async cancelRun(rid: string): Promise<PipelineRun> {
    const { data } = await http.post<PipelineRun>(`/sim/runs/${rid}/cancel`);
    return data;
  },
  /** 外部回流：HPC 完成 / 浏览器代理调完能力 / 人工确认，共用此接口 */
  async completeNode(
    rid: string,
    nodeId: string,
    body: { outputs?: Record<string, unknown>; error?: string }
  ): Promise<PipelineRun> {
    const { data } = await http.post<PipelineRun>(
      `/sim/runs/${rid}/nodes/${nodeId}/complete`,
      body
    );
    return data;
  },
  /** 浏览器代理的待办：需本机 vektor3d 执行的能力节点 */
  async pendingCapabilityNodes(): Promise<NodeRun[]> {
    const { data } = await http.get<NodeRun[]>("/sim/pending-capability-nodes");
    return data;
  },

  // --- 工况模板 ---
  async listTemplates(subjectType?: string, solverType?: string): Promise<SimTemplate[]> {
    const { data } = await http.get<SimTemplate[]>("/sim/templates", {
      params: { subject_type: subjectType, solver_type: solverType },
    });
    return data;
  },
  async createTemplate(body: {
    name: string;
    subject_type: string;
    solver_type: string;
    schema?: Record<string, unknown>;
    default_values?: Record<string, unknown>;
    validation_rules?: Record<string, unknown>;
    export_mapping?: Record<string, unknown>;
  }): Promise<SimTemplate> {
    const { data } = await http.post<SimTemplate>("/sim/templates", body);
    return data;
  },
  async deleteTemplate(tid: string): Promise<void> {
    await http.delete(`/sim/templates/${tid}`);
  },
};

// 在线 shell WebSocket：token 同样经 query 传递，仅管理员可连。
export function shellWs(): WebSocket {
  const token = getToken() ?? "";
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws/shell?token=${encodeURIComponent(
    token
  )}`;
  return new WebSocket(url);
}
