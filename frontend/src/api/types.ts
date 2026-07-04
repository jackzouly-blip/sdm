// 与后端 Pydantic 模型一一对应的前端类型定义。

export interface JobTemplate {
  id: number;
  name: string;
  content: string;
  created_at: number;
  updated_at: number;
}

// 用户提交策略：未配置的用户 = 不限并发、优先级0（与不开启限流时行为一致）。
export interface UserPolicy {
  user: string;
  max_concurrent: number | null; // null=不限并发（仍受全局核数约束）
  priority: number; // 越大越优先抢占空出来的核数/名额
  updated_at: number;
}

export interface SubmitResult {
  // submitted: 已直接进入 PBS，jobid 有值；
  // queued: 受用户并发配额或全局核数余量限制，先在门户本地排队，queue_id 有值。
  status: "submitted" | "queued";
  jobid?: string;
  queue_id?: number;
  queued_total?: number;
  exec_user: string;
  name: string;
  script: string;
}

export interface LoginResponse {
  token: string;
  username: string;
}

export interface MeResponse {
  username: string;
  uid: number;
  home: string;
  is_admin: boolean;
}

export interface JobSummary {
  jobid: string;
  short_id: string;
  name: string;
  owner: string;
  pbs_state: string;
  derived_state: string;
  queue: string | null;
  workdir: string | null;
  submit_ts: number | null;
  start_ts: number | null;
  end_ts: number | null;
  walltime_used: string | null;
  nodes: string | null;
  exec_host: string | null;
  // 本地排队中/提交失败的记录才会有值(derived_state=queued_local/queue_failed)；
  // 真实 PBS 任务恒为 null。
  queue_id: number | null;
  msg: string | null;
}

export interface JobDetail extends JobSummary {
  exec_host: string | null;
  walltime_limit: string | null;
  exit_status: number | null;
  extract_state: ExtractState;
  raw: Record<string, unknown>;
  // 结果网盘自动分享
  netdisk_state: NetdiskState;
  netdisk_share_url: string | null;
  netdisk_share_pwd: string | null;
  netdisk_expire_at: number | null;
  netdisk_files: string[] | null;
  netdisk_msg: string | null;
  netdisk_updated: number | null;
}

export interface NetdiskPreviewItem {
  name: string;
  size: number;
}

export interface NetdiskPreview {
  count: number;
  total_bytes: number;
  files: NetdiskPreviewItem[];
}

export type NetdiskState =
  | "none"
  | "pending"
  | "uploading"
  | "partial" // 运行中流式上传：已传部分 d3plot 并生成链接，余下待任务结束后续传
  | "done"
  | "failed"
  | "skipped";

export type ExtractState =
  | "none"
  | "pending"
  | "dispatched"
  | "skipped";

export interface ExtractRule {
  id: number;
  name: string;
  command: string;
  match_name: string;
  match_queue: string;
  match_workdir_prefix: string;
  match_owner: string;
  enabled: boolean;
  priority: number;
  created_at: number;
  updated_at: number;
}

// 新建/编辑规则的表单载荷（id 由后端生成）。
export interface ExtractRuleInput {
  name: string;
  command: string;
  enabled: boolean;
  priority: number;
}

// --- d3plot 网页可视化 ---
export interface D3plotField {
  name: string;
  min: number;
  max: number;
  lo?: number; // 色阶下界(1% 分位)，避免异常值拉爆图例
  hi?: number; // 色阶上界(99% 分位)
  kind?: string; // "deformation"=前端用坐标现算，无 file
  file?: string; // 云图场二进制文件名（按需下载，如 field_1.bin）
}
export interface D3plotPart {
  title: string;
  region: string;
}
export interface D3plotManifest {
  n_verts: number;
  n_tris: number;
  n_shell_tris: number;
  n_states: number;
  n_beam_verts: number;
  n_beam_lines: number;
  n_parts: number;
  parts: D3plotPart[];
  fields: D3plotField[];
  times: number[] | null;
  bytes: number;
  decimated?: boolean; // 是否减面(预览模式)
  n_tris_full?: number; // 减面前原始三角面数
}
export interface D3plotFindResult {
  dir: string;
  found: { path: string; size: number }[];
  n_state_files: number;
}
export interface D3plotPrepareResult {
  ready: boolean;
  key: string;
  task_id?: string;
}

export interface FsEntry {
  name: string;
  is_dir: boolean;
  is_link: boolean;
  size: number;
  mtime: number;
  mode: string;
}

export interface ListResponse {
  path: string;
  roots: string[];
  entries: FsEntry[];
}

export interface PreviewResponse {
  path: string;
  size: number;
  truncated: boolean;
  binary: boolean;
  content: string;
}

export interface PackageRequest {
  paths: string[];
  archive?: string | null;
  pwd?: string | null;
  period?: number;
}

export type TaskStatus =
  | "queued"
  | "running"
  | "success"
  | "failed"
  | "interrupted";

export interface TaskSnapshot {
  id: string;
  type: string;
  owner: string;
  status: TaskStatus;
  phase: string;
  progress: number;
  result: Record<string, unknown> | null;
  error: string | null;
  created_at: number;
  updated_at: number;
}
