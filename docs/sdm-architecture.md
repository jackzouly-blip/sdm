# SDM 架构设计

> SDM = Simulation Design Management。由现有 HPC 门户升级而来：管理端到端的仿真任务，
> 以及 HPC 资源的管理与调度。

本文是架构基线，记录**已定的决策与其理由**，以及仍未决的问题。实现细节随代码走，不在此复述。

---

## 1. 三系统定位与边界

| 系统 | 管什么 | 不管什么 |
|---|---|---|
| **dbit** | 产品端到端研发：一级流程、交付物、需求、角色、归档正本 | 仿真怎么算、几何怎么处理 |
| **SDM** | 端到端仿真任务、HPC 资源管理与调度 | 产品级流程、AI 推理、3D 内核 |
| **vektor3d** | AI 分析、3D 数据处理（几何、网格、相似性、知识检索） | 业务流程、集群调度 |

数据流：dbit 把产品数据交给 SDM → SDM 分解并在集群执行 → 结果回传 dbit 归档。

### 沿用的原则

来自《智能设计管理总体方案》第 18 页，对 vektor3d 的约束，**同样适用于 SDM 对 dbit**：

> 能力只描述「给定输入、产出结果」，不含业务概念 —— 上游调整流程无需下游发版。

推论：SDM 对 dbit 暴露的是**能力服务**，不是业务接口。dbit 侧绑定 SDM 能力与绑定
vektor3d 能力走同一套代码，零新增概念。

### 与 dbit 向导的根本差异

方案第 14 页给 dbit 向导定的前提是**人工步进**：

> AI 产出一律是草稿…引导而非管控：不做自动流转、不替代审批；人可跳过、可回退、可修改。

SDM 的编排相反，**自动化优先、无人值守**。因此不复用向导模式，而是引入 pipeline 编排。

两者的原则在边界上调和：**SDM 内部全自动，人工闸门设在 dbit 边界**——仿真结果回到
dbit 时作为草稿交付物，仍需人工确认后才进入正式交付。

---

## 2. 硬约束：vektor3d 是桌面端

vektor3d 以 Electron 桌面应用形态部署，其 capability server 监听 **`localhost:23710`**
（Origin 白名单 + token 双重校验，异步 job manager，SSE 进度，可取消）。

SDM 后端运行在 Linux 集群上，**永远无法直接调用 vektor3d**。

dbit 的既有做法是浏览器代理——`RdwInstanceServiceImpl` 注释写明"后端纯存储：不调用
vektor3d，只保存前端拿回来的交付物"。SDM 沿用同一模式：

```
一期（浏览器代理）
  SDM 前端（用户浏览器） ──→ localhost:23710 ──→ 产出上传 ──→ SDM 后端

二期（按需，无人值守批量场景）
  SDM 后端 ──→ vektor3d 无头节点（集群旁 worker）
```

capability server 本质是 Electron 内的一个 HTTP server，做无头形态是"换宿主"而非重写，
**协议零改动**。因此二期是可选的扩容路径，不是一期负债。

> **现状（权威口径是 `GET /v1/capabilities` 的实时返回，不是本文）**：
> capability server 已注册 19 个能力，面向 SDM 的 14 个：
>
> | 域 | 能力 |
> |---|---|
> | 几何 | `geometry.convert`、`geometry.inspect` |
> | 网格 | `mesh.inventory`、`mesh.classify`、`mesh.generate`、`mesh.check`、`mesh.merge`、`mesh.export`、`mesh.checkout`、`mesh.checkin`、`mesh.params.derive` |
> | 文档/会话 | `context.ensure`、`doc.analyze`、`llm.chat` |
>
> 另有 5 个供 dbit 智能研发向导使用（`requirement.analyze`、`solution.design`、
> `plm.analyze`、`rd.chat`、`report.generate`）。
>
> 能力清单随 vektor3d 迭代变化，**任何时候都应打 `/v1/capabilities` 现场确认**
> ——它带每个能力的 `ready` 状态与完整输入输出 schema，比读文档可靠（这段提示
> 本身就曾脱节两个迭代）。`context.ensure` / `doc.analyze` / `mesh.params.derive`
> 依赖 AI 引擎，目录里看不到它们通常意味着对方 AI 引擎没起来，而非版本不对。

---

## 3. 数据模型

参考 vektor3d 原型（`packages/storage-service/src/simulation-store.js`）的 schema 设计，
**仅借鉴模型，前端与数据访问层重写**（原型是 Electron + IPC，SDM 是 HTTP + SQLite）。

仿真数据的主权在 SDM。

```
sim_project              仿真项目（求解器、单位制）
├─ sim_analysis_target   分析对象 ──→ 关联 dbit 产品数据
│  └─ sim_geometry_version   几何版本（STEP/BREP/轻量化/拓扑摘要）
│     └─ sim_mesh_version    网格版本（引擎、参数、质量报告）
└─ sim_subject           工况（类型 + 求解器 + 模板 + 配置）
   └─ sim_job            作业 ──→ 外键指向现有 jobs.jobid
      └─ sim_result      结果（类型、路径、元数据）

sim_template             工况模板：schema_json / validation_rules_json / export_mapping_json

sim_material             材料库（全局资产，与模板同级）：物理材料 → 性能/曲线/求解器卡
                         详见 docs/sdm-material-library.md
```

### 关键设计

**`sim_job` 不重复实现执行**，外键指向现有 `jobs` 表。现有 jobs 保留"裸作业"语义
（手工提交的照常可用），`sim_job` 是其上的编排层。现有 HPC 功能零回归。

**`sim_template` 的三个 json 字段是 AI 的约束边界**：`schema_json` 定义可填什么、
`validation_rules_json` 定义什么算对、`export_mapping_json` 定义如何导出求解器输入卡。
Agent 不是自由生成输入卡，而是填一个受约束的结构——这是让 AI 产出可控、可验证的关键。

---

## 4. Pipeline 编排

### 选型：自研轻量 DAG

不引入 Prefect / Dagster / Airflow / Argo / Temporal。理由不是"框架重"，而是**模型不匹配**：

这些框架的核心假设是 task 在它的 worker 里执行。而 SDM 的节点大多是"提交出去 + 等外部
状态"——求解在 PBS 集群（小时至天级），几何与网格在 vektor3d。SDM 是**编排者而非执行者**，
用通用框架会把主要精力花在写 sensor/poller 适配器上，而框架真正能帮忙的部分（分发、重试、
资源池）恰好是现有 `TaskManager` / `submit.scheduler` / `jobs.poller` 已经具备的。
Argo 需要 K8s，与裸机 PBS 集群不符。

另一层考虑：现有后端是**单个 systemd unit + SQLite**，这个部署简洁度有实际价值，
不宜为 DAG 引入独立 server 与数据库。

**换用框架的条件**：SDM 自身需要跑大量进程内计算任务（而非委托外部），或需要跨多集群
调度。届时再迁移——下述节点定义是声明式的，迁移代价可控。

### 存储模型

```
pipeline_def        声明式 DAG 文档，版本化
pipeline_run        一次运行实例，快照所用的 def 版本（保证可复现）
pipeline_node_run   节点级状态，SQLite 持久化 → 服务重启可续跑
```

### 节点类型

| 类型 | 解析到 |
|---|---|
| `internal` | SDM 内置能力，进程内直接调用 |
| `capability` | 能力服务：vektor3d（浏览器代理 / 未来无头节点）或其他 |
| `hpc` | 提交 PBS 作业，挂起等待现有 poller 的状态事件 |
| `manual` | 人工确认。**可选，默认不用**——这是与 dbit 向导的根本区别 |

`hpc` 节点的提交是**两段式**的，因为现有链路本身就是：先入本地排队队列，再由准入
调度器按用户配额与全局核数余量决定何时 qsub。故外部句柄有两种形态——排队阶段是
`sq:<队列项 id>`，qsub 成功后升级为 PBS 作业号。回流时先升级再匹配，"排队中"这个
中间态因此不会让节点失去追踪。编排层不重新实现提交与轮询，只做桥接。

**能力的物理位置对 DAG 定义透明**：节点只写 `capabilityId`，运行时解析它落在 SDM 内置、
vektor3d 还是别处。能力搬家不需要改 DAG。

### 节点类型 schema 驱动

编辑器**不认识任何具体节点类型**。每个节点类型自行声明：

- `type_id` / 名称 / 图标
- `params_schema`（JSON Schema）—— 编辑器据此渲染参数表单
- 输入输出端口声明
- 执行器实现

新增节点类型 = 注册一条定义，编辑器零改动。这与 vektor3d capability registry 的
`register({ id, name, inputSchema, outputSchema })` 是同一模式。

此设计同时化解了"可视化编辑器会把仍在演进的模型固化住"的风险——模型演进体现为节点类型
的增减，不触及编辑器。

### 可视化编排

采用 **Vue Flow**（`@vue-flow/core`），与现有 Vue 3 + Tailwind 栈一致。

DAG 文档需**可导出**：可视化编辑器 ↔ 声明式文档双向可逆，便于版本对比与问题排查。

### 扇出

一等能力：一个设计 → N 个工况变体 → 并行提交 → 汇总对比。扇出规则直接建模在
`sim_subject` 上，不套用框架的 mapped task 抽象。

### 执行驱动

**事件驱动 + 兜底轮询**——沿用现有模式。`submit/scheduler.py` 已是"新提交"与"轮询发现
任务结束"双事件触发 + 兜底间隔的结构。DAG 推进复用同一套：作业状态变化、能力回调、
人工确认均为推进事件，另设兜底 tick 防漏。

---

## 5. 能力分工原则

判据是**依赖什么**，而非"谁先做"：

- **实现在 SDM**：依赖 HPC / 集群文件系统、需要无人值守、依赖仿真领域模型
  （输入卡生成、校验、结果提取、可视化产物、工况展开、结果汇总）
- **实现在 vektor3d**：依赖 3D 内核与 AI 推理
  （几何清理、网格生成与检查、相似性分析、输入参数优化建议）

SDM 自身也实现同一套能力契约，因此"部分能力直接建在 SDM"不是特例，而是能力解析的常规分支。

---

## 6. 对外契约

SDM 对 dbit 暴露能力服务，沿用方案第 19 页的四步契约：

| 步骤 | 说明 |
|---|---|
| 发现能力 | 能力目录：输入输出规格、预估耗时、就绪状态 |
| 提交作业 | 按能力 ID 提交输入，立即返回作业号 |
| 跟踪进度 | 轮询或订阅推送 |
| 取回结果 | 结构化结果 + 产出物；失败给出可读原因 |

性质要求：**异步作业、幂等提交、可取消、就绪实时求值**。

**结果回传必须是 SDM → dbit 的服务端回调**，不能走浏览器——HPC 作业运行数小时至数天，
依赖页面常开不可靠。这不违反 dbit"后端不调 vektor3d"的原则：方向是入站，SDM 调用
dbit 的 REST 接口。

---

## 7. 用户体系映射

SDM 使用 PAM 系统账号（作业须以真实用户身份 setuid 执行），dbit 有独立用户体系。

**复用现有机制**：`Settings.agent_api_token` + `X-Act-As-User` 头本就是为"3dix 门户作为
可信内部调用方"设计的，agent 据用户名 setuid 执行。dbit → SDM 直接沿用，仅需新增一张
`dbit_user ↔ 系统账号` 映射表。

---

## 8. 前端一级 APP

现有 11 个路由平铺于单一 `AppLayout`，改为 App 注册表 + 顶层切换器：

| 一级 APP | 内容 |
|---|---|
| `/hpc/*` 算力管理 | 现有全部平移：任务、文件、打包、后处理、模板、策略、统计、节点、终端 |
| `/sim/*` 仿真设计 | 仿真项目、分析对象、工况、pipeline 编排 |
| `/viewer/*` 结果查看 | d3plot 迁入并插件化 |
| `/agent/*` AI 工作台 | Agent 会话、工具调用可视化 |

现有页面整体平移，仅路由前缀与嵌套 layout 变化，风险可控。

### 结果查看的插件化

现有链路 `backend/app/d3plot/`（lasso-python 解析 → 缓存产物）+ `D3plotView.vue` 仅支持
LS-DYNA 碰撞。抽象为按 `sim_result.result_type` 注册的 viewer 插件：后端解析器与前端展示
组件成对注册。碰撞是第一个实现，CFD / NVH / 疲劳各自注册，互不影响。

---

## 9. 一期范围

网格能力由 vektor3d 后续迭代，一期**立骨架与契约**：

1. `sim_*` 数据模型
2. DAG 引擎 + `internal` / `capability` / `hpc` 三类节点
3. 节点类型注册表（schema 驱动）+ Vue Flow 可视化编排
4. SDM 内置能力注册表 + 能力服务客户端（vektor3d 走浏览器代理）
5. **一条端到端可用的 pipeline**：人工上传网格 → 输入卡生成 → 校验 → 提交 HPC → 结果
   提取 → 可视化产物 → 回传 dbit。网格节点先用 `manual` 占位，vektor3d 能力就绪后改为
   `capability`，DAG 定义只改一处
6. SDM 对 dbit 的能力服务契约
7. 前端一级 APP 骨架 + 现有页面平移至 `/hpc/*`

第 5 条从一开始就是真实可用的链路，而非演示。

### 进度

第 5 条那条 pipeline 已端到端可用：**配置校验 → 输入卡生成 → 提交 → 求解 →
结果收集 → 查看产物 → 在线查看**。1、2、3、4、7 均已完成。

剩余：第 6 条对 dbit 的能力服务契约；`capability.invoke` 的**浏览器代理循环**
（服务端的待办/claim/complete 三个 API 已备齐，但前端 `pendingCapabilityNodes()`
至今零调用——能力节点编进 DAG 会永久挂起，这是接入 mesh.* 编排前的最后一块）。

**网格链已按"页面按钮直调"落地**（`MeshPanel.vue`：分析零件形态 → 逐零件生成 →
合并回装 → 检查/导出 → 在 ANSA 中微调再提交），与几何链同一模式、绕开编排引擎。
这是刻意的:那条路已被几何链验证过一遍,不必等代理循环就能端到端跑通;
代理循环补上后,这些动作可以再编进 DAG 做无人值守批量。

**刻意未做**：提取节点。作业完成时轮询器已自动派发提取规则
（poller → dispatcher.dispatch_many），再加编排节点只会重复执行同一批命令。
若将来需要把提取作为流水线里的显式步骤（如按工况用不同规则），
再基于 `task:` 句柄机制补。

### 外部句柄一览

三种"等外部"共用一套挂起/回流机制，区别只在句柄形态与解析方式：

| 句柄 | 含义 | 解析时机 |
|---|---|---|
| `sq:<队列项 id>` | 已入本地排队，尚未 qsub | 每轮轮询升级为作业号 |
| `<PBS 作业号>` | 已提交，等求解 | 作业完成事件 |
| `task:<任务 id>` | 已投异步任务（如 d3plot 解析） | 每轮轮询查任务状态 |

句柄一律落库而非用内存订阅，故服务重启后仍能续上。

---

## 10. 未决项

- **SDM 在总体方案中的位置**：《智能设计管理总体方案》是 3DiX + vektor3d 的两方叙事，
  SDM 作为第三方需要补入。
- **vektor3d 几何/网格能力的交付时间**：决定 `manual` 占位节点何时能替换为 `capability`。
- **dbit 侧能力注册的具体协议**：`RdwCapabilityBinding.capabilityId` 目前是模板配置里的
  元数据，由前端解析调用；SDM 作为跨网能力提供方接入时的注册与鉴权方式待与 dbit 侧确认。
- **无头 vektor3d 节点**：批量网格成为瓶颈时启动，非一期。
