// 与后端 Pydantic 模型一一对应的前端类型定义。

// 模板用途：pbs=提交到 PBS 的作业脚本；trial=在管理节点直跑的试算命令行。
export type TemplateKind = "pbs" | "trial";

export interface JobTemplate {
  id: number;
  name: string;
  content: string;
  kind: TemplateKind;
  created_at: number;
  updated_at: number;
}

// 试算提交结果
export interface TrialSubmitResult {
  id: number;
  jobid: string; // "trial:<id>"
  exec_user: string;
  name: string;
}

// 试算命令输出增量拉取结果
export type TrialStatus =
  | "running"
  | "finished"
  | "failed"
  | "killed"
  | "interrupted";

export interface TrialOutput {
  id: number;
  status: TrialStatus;
  exit_code: number | null;
  msg: string | null;
  data: string; // 从请求 offset 起的新增输出
  offset: number; // 新的偏移，下次带上
  size: number;
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

// 计算节点监控
export interface NodeInfo {
  name: string;
  state: string;
  states: string[];
  health: "up" | "down" | "offline" | "unknown";
  np: number | null;
  used_slots: number;
  running_jobs: number;
  ntype: string | null;
  power_state: string | null;
  loadave: string | null;
  ncpus: number | null;
  physmem: string | null;
  availmem: string | null;
  totmem: string | null;
  gpus: number | null;
  raw: Record<string, string>;
}

export interface NodesResponse {
  nodes: NodeInfo[];
  summary: {
    total: number;
    up: number;
    down: number;
    offline: number;
    unknown: number;
  };
}

// --- SDM 仿真设计 ---

export interface SimProjectStats {
  targets: number;
  subjects: number;
  jobs: number;
}

export interface SimProject {
  id: string;
  name: string;
  description: string | null;
  owner: string;
  dbit_project_code: string | null;
  status: "active" | "archived";
  default_solver: string | null;
  unit_system: string;
  workdir: string | null;
  created_at: number;
  updated_at: number;
  stats?: SimProjectStats;
}

export interface SimProjectInput {
  name: string;
  description?: string | null;
  dbit_project_code?: string | null;
  default_solver?: string | null;
  unit_system?: string;
  workdir?: string | null;
}

export interface SimTarget {
  id: string;
  sim_project_id: string;
  name: string;
  target_type: string;
  source_ref: Record<string, unknown> | null;
  created_at: number;
}

export interface SimSubject {
  id: string;
  sim_project_id: string;
  name: string;
  subject_type: string;
  solver_type: string;
  template_id: string | null;
  sim_mesh_version_id: string | null;
  config: Record<string, unknown> | null;
  status: "draft" | "ready" | "running" | "done" | "failed";
  created_at: number;
  updated_at: number;
}

export interface SimJob {
  id: string;
  sim_subject_id: string;
  sim_mesh_version_id: string | null;
  /** 跨库软引用 portal.db 的 jobs.jobid；未投递时为 null */
  hpc_jobid: string | null;
  submit_mode: string;
  submit_payload: Record<string, unknown> | null;
  status: "draft" | "submitted" | "running" | "done" | "failed";
  error_message: string | null;
  subject_name?: string;
  created_at: number;
  submitted_at: number | null;
  finished_at: number | null;
}

export interface SimTemplate {
  id: string;
  name: string;
  subject_type: string;
  solver_type: string;
  schema: Record<string, unknown> | null;
  default_values: Record<string, unknown> | null;
  validation_rules: Record<string, unknown> | null;
  export_mapping: Record<string, unknown> | null;
  is_builtin: number;
  created_at: number;
  updated_at: number;
}

export interface SimResult {
  id: string;
  sim_job_id: string;
  /** 查看器插件的分发键：d3plot / binout / ... */
  result_type: string;
  file_path: string;
  meta: Record<string, unknown> | null;
  subject_name?: string;
  created_at: number;
}

// --- SDM 编排 ---

/** 节点类型声明。画布的节点面板与参数表单完全由它生成。 */
export interface NodeTypeDef {
  type_id: string;
  label: string;
  category: "internal" | "capability" | "hpc" | "manual";
  description: string;
  /** JSON Schema，编辑器据此渲染参数表单 */
  params_schema: {
    type?: string;
    properties?: Record<string, JsonSchemaProp>;
    required?: string[];
  };
  inputs: string[];
  outputs: string[];
}

export interface JsonSchemaProp {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
}

export interface DagNode {
  id: string;
  type: string;
  label?: string;
  params?: Record<string, unknown>;
  /** 只供画布，引擎忽略 */
  position?: { x: number; y: number };
}

export interface DagEdge {
  from: string;
  to: string;
}

export interface DagDoc {
  nodes: DagNode[];
  edges: DagEdge[];
}

export interface PipelineDef {
  id: string;
  name: string;
  description: string | null;
  version: number;
  doc: DagDoc;
  owner: string;
  created_at: number;
  updated_at: number;
}

export type NodeRunStatus =
  | "pending"
  | "running"
  | "waiting"
  | "done"
  | "failed"
  | "skipped";

export interface NodeRun {
  id: string;
  pipeline_run_id: string;
  node_id: string;
  node_type: string;
  status: NodeRunStatus;
  external_ref: string | null;
  wait_hint: string | null;
  inputs: Record<string, unknown> | null;
  outputs: Record<string, unknown> | null;
  error_message: string | null;
  started_at: number | null;
  finished_at: number | null;
  /** 仅待办接口返回：该节点在 DAG 文档里配置的参数 */
  params?: Record<string, unknown>;
  run_id?: string;
}

export interface PipelineRun {
  id: string;
  pipeline_def_id: string;
  def_version: number;
  doc_snapshot: DagDoc;
  sim_project_id: string | null;
  sim_subject_id: string | null;
  owner: string;
  status: "running" | "waiting" | "done" | "failed" | "canceled";
  error_message: string | null;
  created_at: number;
  updated_at: number;
  finished_at: number | null;
  /** 详情接口才带 */
  nodes?: NodeRun[];
}
