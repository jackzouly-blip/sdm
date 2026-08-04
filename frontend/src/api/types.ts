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

/** 跨目录移动的结果。同一文件系统内瞬时完成（task_id 为 null）；
 *  跨文件系统要复制字节，返回 task_id 由前端轮询进度。 */
export interface MoveResult {
  task_id: string | null;
  moved: string[];
  failed: { path: string; error: string }[];
  cross_device: boolean;
}

// --- 网盘数据管理（入站：客户分享链接 → 集群）---
// 注意与上面的 Netdisk* 区分：那些是**出站**（结果上传网盘并分享），
// 下面这组是**入站**（把客户放在网盘里的输入数据同步到 HPC 服务器）。

/** 平台凭据状态。**只有状态，永远不含凭据本身**——后端不回显。 */
export interface NetdiskCredStatus {
  configured: boolean;
  /** db=管理页配置（可热改）；env=部署文件配置（改了要重启）；none=未配 */
  source: "db" | "env" | "none";
  has_stoken: boolean;
  /** unknown=尚未验证；ok=可用；auth_failed=已失效需更换 */
  state: "none" | "unknown" | "ok" | "auth_failed";
  account: string;
  updated_at: number;
  updated_by: string;
  last_checked_at: number;
}

/** 入站功能可用性；ready=false 时表示平台未配置网盘凭据。 */
export interface NetdiskSyncStatus {
  ready: boolean;
  inbox_base: string;
  poll_enabled: boolean;
  default_interval: number;
  is_admin: boolean;
  credentials: NetdiskCredStatus;
}

/** 「测试连接」的返回：状态字段 + 本次结论。 */
export interface NetdiskCredTestResult extends NetdiskCredStatus {
  ok: boolean;
  detail: string;
}

/** 链接健康度：invalid 需用户重交链接，auth_failed 是平台侧故障。 */
export type ShareLinkState = "unknown" | "ok" | "invalid" | "auth_failed";

/** 一轮同步的结果状态。 */
export type ShareSyncStatus = "" | "idle" | "syncing" | "done" | "partial" | "failed";

export interface NetdiskShare {
  id: number;
  owner: string;
  name: string;
  share_url: string;
  pwd: string;
  sub_dir: string;
  local_dir: string;
  enabled: boolean;
  /** 秒；0 = 仅手动同步 */
  poll_interval: number;
  link_state: ShareLinkState;
  last_poll_at: number;
  last_status: ShareSyncStatus;
  last_error: string;
  last_task_id: string;
  syncing: boolean;
  created_at: number;
  updated_at: number;
  /** 各状态文件计数，如 { done: 12, failed: 2 } */
  counts?: Record<string, number>;
}

export interface NetdiskShareInput {
  name: string;
  share_url: string;
  pwd: string;
  sub_dir: string;
  /** 留空由后端派生到 inbox 根下，用户不必知道集群绝对路径 */
  local_dir: string;
  enabled: boolean;
  poll_interval: number;
}

export type ShareFileState = "seen" | "transferred" | "done" | "failed";

export interface NetdiskShareFile {
  /** 字符串：百度 fs_id 超出 JS 安全整数范围，**切勿 Number() 转换** */
  fs_id: string;
  share_path: string;
  filename: string;
  size: number;
  md5: string;
  batch_id: string;
  state: ShareFileState;
  local_path: string;
  error: string;
  updated_at: number;
}

export interface SharePreviewItem {
  fs_id: string;
  name: string;
  path: string;
  isdir: boolean;
  size: number;
}

/** 新建向导里"先验链接再落库"的返回。 */
export interface SharePreview {
  sub_dir: string;
  items: SharePreviewItem[];
  file_count: number;
  total_bytes: number;
}

/** 中转区批次汇总，供人工清理决策。 */
export interface ShareBatch {
  batch_id: string;
  files: number;
  bytes: number;
  done: number;
  complete: boolean;
  remote_dir: string;
}

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
  /** 结算组装引用的模板发布版本（不可变快照）id */
  control_release_id?: string | null;
  material_release_id?: string | null;
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
  control_release_id?: string | null;
  material_release_id?: string | null;
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

/**
 * 一次几何转换的受限票据。交给桌面端 vektor3d，让它自己来拉源文件、推产物。
 * token 只对这一个 gid、只对这两个端点有效，且以分钟计过期。
 */
export interface SimConvertTicket {
  gid: string;
  token: string;
  expiresIn: number;
  sourceName: string;
  /** vektor3d GET 这个地址拉源文件 */
  sourceUrl: string;
  /** vektor3d 转换完 POST 这个地址回传 GLB */
  uploadUrl: string;
}

/** 客户需求文档：仿真项目的输入源头，质量卡实例最终要从它推导出来 */
export interface SimRequirementDoc {
  id: string;
  sim_project_id: string;
  name: string;
  doc_type: string;
  source_file: { name: string; size: number; path: string } | null;
  /** AI 分析产出；未接入时为 null */
  analysis: Record<string, unknown> | null;
  analysis_status: "pending" | "analyzing" | "done" | "failed";
  note: string | null;
  created_at: number;
}

/** 一个量纲化的指标。kind 区分主判定值/内控值/对外目标/项目例外 */
export interface SimRequirementMetric {
  quantity: string;
  op: string;
  value: number;
  unit: string;
  raw: string;
  kind: "target" | "internal" | "external" | "override";
  scope: string;
}

/** 需求条目：需求进入平台后的最小可追溯单元 */
export interface SimRequirementItem {
  id: string;
  doc_id: string;
  seq: string;
  category: "subject" | "loading" | "mesh" | "delivery" | "other";
  title: string;
  raw_text: string;
  metrics: SimRequirementMetric[];
  /** required=合格(验收门槛) / reference=参考 */
  baseline: string;
  project_note: string | null;
  /** 原文出处，如「第1页 表1 第3行」——条目的价值全在可追溯 */
  source_ref: string;
  needs_clarification: number;
  clarification_hint: string | null;
  load_points: number;
  indenter_diameter_mm: number | null;
  extracted_by: "rule" | "ai" | "manual";
  status: string;
}

/** 一次 AI 解析的只读票据。交给桌面端 vektor3d，让它自己来拉文档原件 */
export interface SimAnalyzeTicket {
  rid: string;
  token: string;
  expiresIn: number;
  sourceName: string;
  sourceUrl: string;
}

export interface SimExtractSummary {
  itemCount: number;
  subjectCount: number;
  loadingCount: number;
  needsClarification: number;
  requiredCount: number;
  loadPointTotal: number;
  /** 点位坐标在图上，文字层抽不到 */
  loadPointCoordsAvailable: boolean;
  extractor: string;
  /** AI 读不懂/有矛盾/缺失之处，逐条说明 */
  notes?: string[];
  /** 规则/AI 交叉核对统计（AI 回写时服务端独立跑规则抽取合并的结果） */
  merge?: {
    ruleTotal: number;
    aiTotal: number;
    agreed: number;
    conflicts: number;
    ruleOnly: number;
    aiOnly: number;
  };
}

export interface SimQualityTemplate {
  id: string;
  name: string;
  source: string;
  revision: string;
  scope: string;
  description: string;
  builtin: boolean;
  based_on: string;
  /** 导入者。老模板为空，视为仅管理员可管理 */
  created_by: string;
  /** 被项目实例引用的次数。实例文件已拷走，删模板不影响既有实例 */
  used_by: number;
  overrides: SimQualityOverride[];
}

export interface SimQualityOverride {
  target: string;
  old_value: string;
  new_value: string;
  /** 依据出处。AI 生成时必填——无出处的阈值就是编的 */
  source: string;
  by: string;
}

/** 一条质量判据。calculation 是算法族：同一指标按 NASTRAN 与按 IDEAS 算数值不同 */
export interface SimQualityCriterion {
  name: string;
  domain: string;
  /** 停用的判据在这张卡里不检查 */
  enabled: boolean;
  calculation: string;
  weight: number;
  higherIsBetter: boolean;
  thresholds: { best: number | null; good: number | null; failed: number | null; worst: number | null };
}

export interface SimQualityCardDetail {
  id: string;
  name: string;
  source: string;
  revision: string;
  scope: string;
  description: string;
  builtin: boolean;
  ansaVersion: string;
  /** 启用的 shells 判据——"这张卡在管什么"的答案 */
  criteria: SimQualityCriterion[];
  /** 全部判据，含停用的与 solids 域——整份 .ansa_qual 的完整内容 */
  allCriteria: SimQualityCriterion[];
  /** 生成侧 .ansa_mpar 全部参数，按文件分节分组。值为原始字符串（数字/布尔/枚举/表达式混杂） */
  meshGroups: { title: string; params: { key: string; value: string }[] }[];
  meshParams: {
    targetElementLength: number;
    minTargetLength: number;
    maxTargetLength: number;
    elementType: string;
    featureHandling: string;
  };
  /** 判废线上的最小单元长度对应的显式时间步(秒)——碰撞机时的总闸 */
  timeStepAtFailedMinLength: number;
}

export interface SimQualityCard {
  id: string;
  sim_project_id: string;
  template_id: string;
  name: string;
  card_dir: string | null;
  overrides: SimQualityOverride[];
  derived_from_doc_id: string | null;
  status: string;
  created_at: number;
  card?: SimQualityCardDetail;
}

export interface SimGeometry {
  id: string;
  sim_target_id: string;
  version_no: number;
  source_type: string;
  source_file: { name: string; size: number; path: string } | null;
  step_file: string | null;
  brep_file: string | null;
  /** 轻量化产物（glTF/GLB）路径；未转换时为 null */
  lightweight_file: string | null;
  topo_summary: Record<string, unknown> | null;
  /** mesh.inventory 的零件清单（ANSA 产品树口径） */
  part_inventory: SimPartInventoryItem[] | null;
  /** mesh.classify 的网格策略建议 */
  mesh_strategy: SimMeshStrategy | null;
  /** 派生溯源：由哪个几何版本生成（气囊平面图 → deck 就是这条链） */
  derived_from_id: string | null;
  /** 生成它的能力 id，如 vektor3d:mesh.airbag.generate */
  derived_by: string | null;
  status: string;
  created_at: number;
}

/** 气囊网格化票据：让桌面端 vektor3d 自取 .igs、自送 deck 回来 */
export interface SimAirbagTicket {
  gid: string;
  token: string;
  expiresIn: number;
  sourceName: string;
  sourceUrl: string;
  deckUploadUrl: string;
}

// --- 网格（契约：docs/vektor3d-geometry-capability-contract.md 2.3~2.11）---

/** 一个零件（mesh.inventory）。name 可直接作为 mesh.generate 的 partFilter */
export interface SimPartInventoryItem {
  index: number;
  id: number;
  name: string;
  moduleId?: string;
  faceCount: number;
  /** [minx,miny,minz,maxx,maxy,maxz]，全局坐标 */
  bbox: number[] | null;
}

/** 逐零件/逐体的网格策略建议（mesh.classify） */
export interface SimMeshStrategyPart {
  partId: string;
  meshType: "surface" | "volume" | "midsurface";
  fallback: string | null;
  confidence: number;
  needsReview: boolean;
  /** 二级复核（BREP 壁厚分布）给出的中面最小壁厚建议 */
  recommendedMinThickness?: number;
  refined?: boolean;
  reasons: string[];
  signals: Record<string, unknown>;
}

export interface SimMeshStrategy {
  parts: SimMeshStrategyPart[];
  summary: {
    partCount: number;
    counts: Record<string, number>;
    needsReviewCount: number;
  };
  refineUsed?: boolean;
  warnings?: string[];
}

/**
 * 网格版本。**.ansa 是正本**（几何+网格+属性+厚度全量），solver_file 只是派生物：
 * 手工微调、装配合并、换求解器格式导出都以 ansa_file 为源。
 */
export interface SimMesh {
  id: string;
  sim_geometry_version_id: string;
  version_no: number;
  mesh_type: string;
  mesh_engine: string;
  mesh_params: Record<string, unknown> | null;
  mesh_file: string | null;
  ansa_file: string | null;
  solver_file: string | null;
  solver_format: string | null;
  preview_file: string | null;
  report_file: string | null;
  /** 逐零件生成时的零件名 */
  part_filter: string | null;
  /** 合并回装的来源网格 id */
  source_mesh_ids: string[] | null;
  checkout_id: string | null;
  checkout_by: string | null;
  checkout_at: number | null;
  quality: Record<string, unknown> | null;
  /** generating / ready / failed / checked-out */
  status: string;
  created_at: number;
  updated_at: number | null;
}

/** 网格作业票据（绑单个 gid，默认 2 小时——网格作业比几何转换慢一个量级） */
export interface SimMeshTicket {
  gid: string;
  token: string;
  expiresIn: number;
  sourceName: string;
  sourceUrl: string;
  /** 产物地址前缀，后接 /{mid}/artifact/{kind} */
  meshUrlPrefix: string;
}

// --- AI 会话（契约：docs/vektor3d-ai-session-contract.md）---

/** 一条结构化提案。AI 只能"提案"，确认后由前端带用户凭据调既有接口执行 */
export interface SimAiProposal {
  /** 动作类型，服务端按契约 5.2 的枚举校验 */
  action: string;
  targetId?: string;
  /** 与对应 SDM 接口的请求体字段一致 */
  patch?: Record<string, unknown>;
  /** 为什么改——强制，落目标对象的留痕 */
  reason: string;
  evidence?: { ref: string; text?: string }[];
  /** UI 状态：真正的数据变更留痕在目标对象自己的机制里 */
  status: "pending" | "confirmed" | "rejected";
  decided_by?: string;
  decided_at?: number | null;
  note?: string;
}

export interface SimAiMessage {
  id: string;
  session_id: string;
  seq: number;
  role: "user" | "assistant" | "system";
  content: string;
  proposals: SimAiProposal[] | null;
  citations: { text?: string; ref: string }[] | null;
  meta: Record<string, unknown> | null;
  created_at: number;
}

export interface SimAiSession {
  id: string;
  sim_project_id: string;
  title: string;
  created_by: string;
  status: string;
  created_at: number;
  updated_at: number;
  /** 详情接口才带 */
  messages?: SimAiMessage[];
}

// --- 材料库（全局资产；设计：docs/sdm-material-library.md）---

/** 物理材料（求解器无关）。模型变体在 cards 层 */
export interface SimMaterial {
  id: string;
  name: string;
  category: string;
  standard_code: string | null;
  description: string | null;
  source: string | null;
  /** 内容变更时 +1；二期零件匹配钉住它保证可复现 */
  revision: number;
  status: "active" | "deprecated";
  created_by: string;
  created_at: number;
  updated_at: number;
  /** 列表接口带的资产计数 */
  card_count?: number;
  curve_count?: number;
}

export interface SimMaterialProperty {
  id: string;
  material_id: string;
  name: string;
  value: number;
  unit: string;
  condition: Record<string, unknown> | null;
}

/** 曲线。points 是文件原始数值，真实值 = sfa*(x+offa) / sfo*(y+offo) */
export interface SimMaterialCurve {
  id: string;
  material_id: string;
  curve_type: string;
  title: string;
  /** 同族曲线（如同一张应变率表展开的一组）共享 family_key */
  family_key: string;
  condition: { strain_rate?: number; unit?: string } | null;
  x_quantity: string;
  x_unit: string;
  y_quantity: string;
  y_unit: string;
  points: [number, number][];
  scale: { sfa: number; sfo: number; offa: number; offo: number } | null;
  source_lcid: number | null;
}

/** 求解器卡：keyword_text 是自包含关键字原文块，导出以它为准 */
export interface SimMaterialCard {
  id: string;
  material_id: string;
  solver_type: string;
  mat_type: string;
  title: string;
  variant: "primary" | "null" | "alt";
  unit_system: string;
  source_mid: number | null;
  params: Record<string, number> | null;
  keyword_text: string;
}

export interface SimMaterialDetail extends SimMaterial {
  properties: SimMaterialProperty[];
  curves: SimMaterialCurve[];
  cards: SimMaterialCard[];
}

/** 材料模板文件：一组材料卡的具名选择，导出即一份可 *INCLUDE 的 MAT.K。
 *  组装式——模板只记"选了哪几张卡"，材料库才是正本。 */
export interface SimMaterialTemplate {
  id: string;
  name: string;
  description: string;
  solver_type: string;
  unit_system: string;
  /** vektor3d 带 KB 的复核结果（字段级闭包/ID 区段），由 cae.template.parse 回填 */
  summary_json: string | null;
  status: "active" | "deprecated";
  /** 改成员列表即 +1 */
  revision: number;
  created_by: string;
  created_at: number;
  updated_at: number;
  card_count?: number;
}

/** 交给桌面端 vektor3d 自取模板正文的短期票据（15 分钟，只读单份） */
export interface SimTemplateParseTicket {
  tid: string;
  kind: "material" | "control";
  token: string;
  expiresIn: number;
  sourceName: string;
  sourceUrl: string;
  unitSystem: string;
  /** 变了即缓存失效，能力侧据此决定重拉重算 */
  version: string;
  expectedSha256: string;
}

/** cae.template.parse 的返回（vektor3d → 浏览器） */
export interface SimTemplateParseResult {
  cached: boolean;
  sourceSha256: string;
  unitSystem: string;
  summary: Record<string, unknown>;
  manifest?: Record<string, unknown>;
  issues: { level: "ERR" | "WARN" | "INFO"; message: string; keyword?: string }[];
}

export interface SimMaterialTemplateItem {
  template_id: string;
  card_id: string;
  seq: number;
  title: string;
  mat_type: string;
  unit_system: string;
  source_mid: number | null;
  material_id: string;
  material_name: string;
}

export interface SimMaterialTemplateDetail extends SimMaterialTemplate {
  items: SimMaterialTemplateItem[];
}

/** 组装校验结果。REJECT=不可组装；CONFLICT=需重编号。
 *  这两类进了 deck 求解器都不会报错，但结果是错的。 */
export interface SimTemplateCheck {
  ok: boolean;
  unit_system: string | null;
  problems: {
    level: "REJECT" | "CONFLICT" | "WARN";
    kind: string;
    message: string;
    mid?: number;
    lcid?: number;
    card_ids?: string[];
    units?: string[];
  }[];
  cards: { id: string; title: string; mat_type: string; unit_system: string; source_mid: number | null }[];
  id_ranges: { MID: number[]; LCID: number[] };
}

/** 控制卡模板：整份存档。控制卡无 ID、彼此无引用，
 *  且 *CONTROL_* 之间是一套互相配合的策略，拆开反而丢了整体性。 */
export interface SimControlTemplate {
  id: string;
  name: string;
  description: string;
  analysis_type: string;
  solver_type: string;
  unit_system: string;
  /** 求解策略摘要，由 vektor3d cae.template.parse 回填 */
  summary_json: string | null;
  source_name: string;
  source_sha256: string;
  status: "active" | "deprecated";
  revision: number;
  created_by: string;
  created_at: number;
  updated_at: number;
  /** 详情才带；列表接口不返回（几 KB 正文） */
  keyword_text?: string;
}

export interface SimMaterialImportReport {
  materials_created: number;
  materials_updated: number;
  materials_unchanged: number;
  cards: number;
  curves: number;
  warnings: string[];
}

/** 模板发布版本：发布时点完整原文的不可变快照，项目引用它做结算组装 */
export interface SimTemplateRelease {
  id: string;
  kind: "material" | "control";
  template_id: string;
  version_no: number;
  name: string;
  unit_system: string;
  note: string;
  created_by: string;
  created_at: number;
  content_bytes?: number;
}
