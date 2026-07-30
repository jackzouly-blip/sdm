# SDM 需要 vektor3d 提供的 AI 会话与推理能力 —— 接口契约（草案）

> 面向 vektor3d 团队。SDM 计划在仿真项目内提供 AI 会话交互：澄清需求疑问、修改
> 需求条目、确认工况与加载条件等。本文给出两侧的职责边界、工作区约定、所需能力
> 的接口契约与设计理由。
>
> 契约沿用 vektor3d 现有的 capability server 机制与《几何能力契约》
> （`vektor3d-geometry-capability-contract.md`）已验证的调用模式：浏览器发起、
> 文件与数据走 vektor3d ↔ SDM 直连、受限票据划权。**新增的基础设施只有一项：
> 项目级推理工作区（workspace）**，其余都是按既有形式注册能力。

---

## 1. 背景与职责边界

| 系统 | 职责 |
|---|---|
| **SDM** | **主数据（record）**：需求文档/条目、质量卡、工况、几何、会话消息；**唯一写闸**与留痕 |
| vektor3d | **推理自治（scratch）**：agent 循环、工具与算法调用、过程数据的工作区 |

连通方向不对称与几何契约相同，是所有设计的前提：

```
SDM 后端  ──✗──>  vektor3d      永远调不通（vektor3d 只监听 localhost）
vektor3d  ──✓──>  SDM 后端      可以，桌面访问内网服务器是通的
浏览器    ──✓──>  两者          调用由浏览器发起
```

### 1.1 一条判据划清两侧（本契约最重要的一句话）

> **workspace 里的任何东西丢了，都必须能从 SDM 主数据重新生成。**

满足这条，工作区放在用户桌面就不构成风险：换电脑、清缓存、重装 vektor3d，损失
的只是重算时间，不是事实。反过来推论：

- 任何"丢了找不回来"的东西——澄清结论、条目修改、阈值调整、会话中做出的决定——
  **必须经提案-确认协议（第 5 节）落回 SDM**；
- **不写回 SDM 的决定等于没有发生**。workspace 不得成为影子主数据：SDM 侧的
  页面、报表、编排一律不读 workspace，两边不存在"以桌面那份为准"的场景。

### 1.2 推理自治的含义

agent 循环（多步推理、中间试错、反复调工具、选什么模型、花多少步）整个属于
vektor3d，SDM **不要求看到每一步**，也不规定提示词与模型。SDM 只收两个口：

1. **读口**：项目级只读票据（第 3 节），vektor3d 凭它按需拉主数据填充 workspace；
2. **写口**：结构化提案，经浏览器带**用户**凭据、用户确认后走 SDM 既有留痕接口。

---

## 2. 推理工作区（workspace）约定

### 2.1 布局

```
<vektor3d 数据目录>/workspaces/<SDM 主机标识>/<project_id>/
  manifest.json          # 缓存清单与来源（见 2.2）
  sources/               # 从 SDM 拉的源文件缓存：需求文档、几何、质量卡导出
  derived/               # 解析中间产物：页面图像、OCR、抽表结果、几何度量
  runs/<job_id>/         # 每次推理/算法运行的过程轨迹与产物
  drafts/                # 会话进行中的草稿：未确认的提案、临时计算
```

目录内部结构 vektor3d 可自行调整（这是你们的地盘），但 `manifest.json` 的语义
是契约的一部分，因为"可再生"承诺靠它兑现。

### 2.2 manifest：provenance 是硬要求

每份从 SDM 拉取的缓存必须记录来源与内容指纹：

```jsonc
{
  "entries": [
    {
      "path": "sources/req-01.pptx",
      "sdmObject": { "type": "requirement_doc", "id": "rd-7f3a…", "hash": "sha256:…" },
      "fetchedAt": "2026-07-28T10:00:00Z"
    }
  ]
}
```

**用途**：推理前对照 SDM 现值判缓存是否过期（hash 不一致 → 重拉）。没有这一条，
AI 会拿着三天前的需求条目回答今天的问题——这类错误用户几乎无法察觉，比"不可用"
危险得多。SDM 的读接口会在响应头/字段中给出内容 hash 以配合判断。

### 2.3 生命周期

- 清理策略（LRU / TTL / 按项目整删）由 vektor3d 自治，SDM 不管也不该管；
- 同一项目在多台桌面各有一份 workspace 是**正常状态**，不需要同步——冲突只可能
  发生在写 SDM 那一刻，由 SDM 的写闸裁决；
- `drafts/` 里未确认的提案随会话过期丢弃即可，**不要**做成"下次打开自动恢复并
  提交"——过期草稿基于过期上下文，恢复它比丢掉它危险。

---

## 3. 项目级只读票据（SDM 侧提供）

沿用几何转换票据（`convert-ticket`）的机制，扩一档范围：

```
POST /api/sim/projects/{pid}/ai-read-ticket
→ { "token": "…", "expiresAt": … }
```

- `scope=ai.read`，绑定单个项目，默认 30 分钟过期；
- 只在**枚举的只读接口**上有效（默认拒绝，不是逐个排除）：需求文档下载、需求
  条目列表、质量卡模板/实例详情、工况列表、几何元数据与轻量化产物下载；
- 在门户其余接口上一律 401；换项目 403；不能用它再签新票据；
- 票据由浏览器申请后随 job 输入交给 vektor3d，与几何契约的 `authToken` 同法。

**为什么是枚举白名单**：读票据会长期、高频地出现在桌面进程里，它的泄露半径必须
一开始就钉死为"这一个项目的只读视图"。

---

## 4. 需要新增的能力

### 4.1 `llm.chat`（必需，优先级最高）

一轮会话推理。输入是消息历史与项目引用，输出是回复与结构化提案。

#### 输入 schema

```jsonc
{
  "type": "object",
  "properties": {
    "project":   {
      "type": "object",
      "properties": {
        "id":        { "type": "string" },
        "baseUrl":   { "type": "string", "description": "SDM 地址，由浏览器用 window.location.origin 拼出" },
        "authToken": { "type": "string", "description": "第 3 节的项目级只读票据" },
        "contextUrl":{ "type": "string", "description": "SDM 汇总的项目上下文快照地址（见下）" }
      },
      "required": ["id", "baseUrl", "authToken"]
    },
    "sessionId": { "type": "string", "description": "SDM 侧会话 id，用于关联 workspace 的 runs/" },
    "messages":  {
      "type": "array",
      "items": { "type": "object", "properties": {
        "role":    { "type": "string", "enum": ["user", "assistant"] },
        "content": { "type": "string" }
      }, "required": ["role", "content"] }
    },
    "allowedActions": {
      "type": "array", "items": { "type": "string" },
      "description": "本轮允许产生的提案类型（第 5 节枚举）。空数组 = 只答疑不提案"
    }
  },
  "required": ["project", "sessionId", "messages"]
}
```

`contextUrl` 指向 SDM 的 `GET /api/sim/projects/{pid}/ai-context`：一份紧凑的项目
快照（条目清单含待澄清标记与出处、质量卡现值、工况列表，均带 hash）。**它是推荐
起点而非强制边界**——vektor3d 可以只用它，也可以凭票据拉原文档做深读。这正是
"推理自治"的体现：SDM 保证有一条低成本的取数路径，但不规定你们怎么用。

#### 输出 schema

```jsonc
{
  "type": "object",
  "properties": {
    "message":   { "type": "string", "description": "给用户看的回复（markdown）" },
    "proposals": { "type": "array", "items": { "$ref": "#/definitions/proposal" },
                   "description": "结构化提案，见第 5 节。可为空" },
    "citations": { "type": "array", "items": { "type": "object", "properties": {
                     "text": { "type": "string" },
                     "ref":  { "type": "string", "description": "如「req-01.pptx 第3页 表1 第2行」" }
                   } },
                   "description": "回复中事实性论断的出处。答疑也要可追溯" },
    "workspaceRun": { "type": "string", "description": "本轮在 workspace 的 runs/<id>，诊断用" }
  },
  "required": ["message", "proposals"]
}
```

进度沿用既有 job 机制（浏览器轮询 `GET /v1/jobs/{id}`，与几何联调一致的原因：
`EventSource` 设不了鉴权头）。多步工具调用期间请通过 `progress` 上报可读的阶段
说明（"正在重读需求文档第 3 页…"），会话 UI 直接展示它。

### 4.2 工具与算法目录（vektor3d 侧自治，两条红线）

agent 用什么工具、怎么注册（进程内还是能力目录），vektor3d 自定。SDM 只提两条：

1. **判定类算法语义必须单一实现**。网格质量四档判定、方向判定、时间步估算这些
   算法目前在 SDM 后端，且有真卡基准测试守着（`test_sim_quality_card.py`）。
   agent 需要算这些时，**调 SDM 只读接口取结果**，不要在 vektor3d 侧重写一份——
   两份实现必然漂移，漂移的后果是"AI 说合格、平台说不合格"。
2. **工具只读主数据**。工具箱里的任何工具都不得直接写 SDM——写只有提案-确认
   一条路。这条与几何契约"写库必须经 SDM 鉴权"同源。

候选工具供参考（均已存在或有明确落点）：doc 解析族（`doc.analyze` 的拆解：读页、
抽表、OCR）、几何度量（`geometry.inspect`）、SDM 查询族（凭读票据查条目/卡/工况）。

---

## 5. 提案-确认协议（写闸）

**AI 只能"提案"，不能"落笔"。** 提案由会话 UI 渲染成卡片，用户逐条确认；确认后
由**浏览器带用户自己的凭据**调 SDM 既有接口执行。这样现有的一切规矩自动生效：
属主校验、改阈值必须带依据、留痕追加、内置模板不可改。**SDM 不为 AI 开任何新的
写接口**。

### 5.1 proposal 结构

```jsonc
{
  "action":   "requirement_item.update",      // 类型，见 5.2 枚举
  "targetId": "item-3f8c…",                   // 目标对象 id
  "patch":    { "…": "与对应 SDM 接口的请求体字段一致" },
  "reason":   "客户 7/25 邮件确认压头直径按 165mm 执行",   // 为什么改——落留痕
  "evidence": [ { "ref": "req-01.pptx 第5页 表2 第3行" } ] // 出处，可多条
}
```

`reason` 与 `evidence` 是**强制**的：确认执行时由前端拼进对应接口的依据字段
（如质量卡覆盖的 `source`），格式为
`"AI 会话 <sessionId> 提案，经 <用户> 确认：<reason>（依据 <evidence>）"`。
没有出处的提案 SDM 前端直接不渲染确认按钮——与"无出处的阈值就是编的"同一条铁律。

### 5.2 动作类型（v1 枚举，按落地顺序）

| action | 落到的 SDM 接口 | 场景 |
|---|---|---|
| `requirement_item.update` | `PATCH /api/sim/requirement-items/{iid}` | 修改条目目标值/基准/说明 |
| `requirement_item.resolve_clarification` | 同上（`needs_clarification=false` + 结论写入） | **首发场景**：澄清工作流 |
| `quality_template.edit_content` | `PATCH /api/sim/quality-templates/{id}/content` | 改用户模板阈值/网格参数 |
| `quality_template.derive` | `POST /api/sim/quality-templates/{id}/derive` | 以内置模板为底座派生 |
| `subject.confirm`（后续） | 工况接口，待工况确认流程定型后补充 | 确认工况与加载条件 |

不在枚举里的 action，SDM 前端一律拒绝渲染——新场景通过修订本契约加入，
不通过"先发了再说"加入。

### 5.3 会话记录的归属

- **用户可见的消息流**（user/assistant 往来、提案及其确认结果）由 SDM 持久化
  （`sim_ai_sessions` / `sim_ai_messages`）——它要跨设备可见、要审计；
- **推理过程**（工具调用轨迹、中间试错、模型往返）留在 workspace `runs/`，可丢；
- 产生确认动作时，"当时为什么这么改"通过 5.1 的 reason/evidence 随留痕落 SDM——
  审计链不依赖 workspace。

---

## 6. 调用时序

```
用户在 SDM 项目页打开 AI 会话，输入一句话
        │
        ├─1─> 浏览器 POST SDM /api/sim/projects/{pid}/ai-sessions/{sid}/messages
        │        （用户消息先落库——消息流是主数据）
        ├─2─> 浏览器 POST SDM /api/sim/projects/{pid}/ai-read-ticket   ← 读票据
        ├─3─> 浏览器 POST localhost:23710/v1/jobs
        │        { capabilityId: "llm.chat", input: { project{…ticket…}, sessionId, messages, allowedActions } }
        │
        │     4─> vektor3d 对照 manifest 判缓存，凭票据拉过期/缺失的主数据 → workspace
        │     5─> agent 循环：推理 ↔ 工具/算法调用（进度经 job progress 上报）
        │
        ├─6─> 浏览器轮询 job → 取回 { message, proposals, citations }
        ├─7─> 浏览器 POST assistant 消息 + 提案落 SDM 会话库
        │
        └─8─> 用户逐条确认提案 → 浏览器带用户凭据调对应 SDM 接口
                 （PATCH 条目 / PATCH 模板内容 / …）→ 留痕自动生效
              确认结果回写会话消息，闭环
```

vektor3d 不可用时，会话入口整体降级隐藏（与 doc.analyze 的探测-降级同一模式）。

---

## 7. SDM 侧准备情况

**SDM 侧配套已实现并可联调**，`llm.chat` 能力上线后即可打通全链：

| 项 | 状态 |
|---|---|
| 会话表 `sim_ai_session` / `sim_ai_message` 及 CRUD 接口 | **已上线**：会话挂项目下、消息按 seq 排序、随项目属主隔离 |
| `POST /projects/{pid}/ai-read-ticket` | **已上线**：scope=ai.read、绑单项目、默认 30 分钟；白名单外 401、跨项目 403、不能续签（有测试守着） |
| `GET /projects/{pid}/ai-context` | **已上线**：条目/质量卡/工况三节各带内容 hash；接受用户令牌或 ai.read 票据 |
| 读接口内容 hash | **已上线**：需求文档下载带 `ETag`（内容 sha256，按 mtime 缓存）；快照各节带 `hash` 字段 |
| 会话 UI | **已上线**：项目详情页「AI 会话」抽屉——消息流、提案卡片、确认/拒绝、vektor3d 探测-降级 |
| 提案枚举与依据校验 | **已上线**：不在 5.2 枚举的 action 400；缺 reason 400；status 由服务端置 pending，裁决一次有效（重复 409） |
| 提案落点接口（条目 PATCH、模板内容 PATCH/derive） | **已上线**（本契约没有引入任何新写接口） |

浏览器侧调用顺序（已实现，供对照 4.1 输入调试）：签票据 → `POST /v1/jobs`
`{capabilityId:"llm.chat", input:{project{id,baseUrl,authToken,contextUrl}, sessionId,
messages, allowedActions}}` → 轮询 → assistant 消息与提案落库 → 提案卡片确认执行。
vektor3d 缺 `llm.chat` 能力或未连接时，会话入口降级为"仅记录消息"并明示原因。

首发场景定为**待澄清条目的澄清工作流**：解析产物已带 `needs_clarification` 标记
与 `clarification_hint`，数据结构现成，价值最直接。

---

## 8. 需要 vektor3d 团队确认的问题

1. `llm.chat` 是否可行、预计排期？agent 循环与工具调用是否已有可复用的实现？
2. workspace 的落盘位置与配额：放 vektor3d 数据目录下是否合适？单项目缓存上限
   建议多少（需求文档 + 几何轻量化产物，可能到百 MB 级）？
3. manifest 判缓存需要 SDM 读接口给内容 hash——响应头（`ETag`）还是响应体字段，
   你们哪种好接？
4. 多步推理的单轮时长可能到分钟级：job 机制对长任务的心跳/超时策略是什么？
   浏览器轮询间隔建议多少？
5. 模型与推理成本由 vektor3d 侧管理（本地模型或你们的 API 通道），SDM 不传模型
   参数——这个分工是否成立？若用户需要选模型，能力目录里如何暴露？
6. `allowedActions` 之外，是否需要 SDM 传"提示词补充"（如项目术语表）？我们倾向
   于**不传**（提示词属于推理自治），除非你们认为有必要。
