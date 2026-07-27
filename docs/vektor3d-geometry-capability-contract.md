# SDM 需要 vektor3d 提供的几何能力 —— 接口契约

> 面向 vektor3d 团队。SDM（由 HPC 门户升级而来）需要把 CAD 数模转成可在浏览器中
> 渲染的轻量化模型。本文给出所需能力的接口契约与设计理由。
>
> 契约完全沿用 vektor3d 现有的 capability server 机制
> （`packages/app-electron/src/capability-server`），**不要求新增任何基础设施**——
> 只是按现有 `registry.register({...})` 的形式多注册几个能力。

---

## 1. 背景与边界

| 系统 | 职责 |
|---|---|
| dbit | 产品端到端研发：一级流程、交付物 |
| **SDM** | 端到端仿真任务、HPC 资源管理与调度 |
| vektor3d | AI 分析、**3D 数据处理** |

沿用《智能设计管理总体方案》第 18 页的原则——能力只描述「给定输入、产出结果」，
不含业务概念。SDM 不希望把 3D 处理搬到自己这边，因此需要 vektor3d 以能力形式提供。

### 一个决定设计的事实：连通方向是单向的

- SDM 后端运行在 **Linux 集群**（内网服务器）
- vektor3d 运行在 **用户 Windows 桌面**，capability server 监听 `localhost:23710`

因此：

```
SDM 后端  ──✗──>  vektor3d      永远调不通（vektor3d 只监听 localhost）
vektor3d  ──✓──>  SDM 后端      可以，桌面访问内网服务器是通的
浏览器    ──✓──>  两者          页面既能调 localhost 也能调 SDM
```

**这个不对称是下面所有设计的依据**：调用由浏览器发起（与 dbit 现有做法一致），
但**文件传输走 vektor3d 与 SDM 之间的直连**，不经浏览器中转。

---

## 2. 需要新增的能力

### 2.1 `geometry.convert`（必需，优先级最高）

把 CAD 原生格式转成可在浏览器渲染的轻量化模型。

**输入格式**：CATIA（.CATPart/.CATProduct）、STEP（.stp/.step）、JT、3DXML、
IGES、SolidWorks、NX —— 以现有 `cad-pipeline` 已支持的为准。

**输出格式：请输出 glTF 2.0 / GLB，不要输出 `.3dix`。**

这一条是本文最重要的请求，理由：

1. `.3dix` 是私有格式，SDM 要渲染就得把 vektor3d 的 worker 解析器复制过去，
   等于开一个维护分叉——你们改格式，SDM 就得跟着改。
2. glTF/GLB 是 web 标准，three.js 原生支持。SDM 已经在用 three.js（d3plot 查看器），
   零新增依赖。
3. 边界更干净：vektor3d 负责「把 CAD 变成标准格式」，SDM 只负责展示，
   两边不共享任何私有实现。

如果内部管线产出的仍是 `.3dix`，只需在能力的 `run` 里加一步 `.3dix → GLB` 的导出即可，
SDM 不关心中间过程。

#### 输入 schema

```jsonc
{
  "type": "object",
  "properties": {
    "sourceUrl":  { "type": "string", "description": "SDM 上的源文件下载地址（HTTP GET）" },
    "uploadUrl":  { "type": "string", "description": "转换产物的回传地址（HTTP PUT/POST）" },
    "authToken":  { "type": "string", "description": "访问上述两个地址的短期令牌，置于 Authorization: Bearer" },
    "sourceName": { "type": "string", "description": "原始文件名，用于判定格式" },
    "options": {
      "type": "object",
      "properties": {
        "maxTriangles": { "type": "number", "description": "三角面预算，超出则减面；0=不减面" },
        "unit":         { "type": "string", "description": "目标单位，默认 mm" },
        "mergeParts":   { "type": "boolean", "description": "是否合并零件为单一网格，默认 false（保留装配树）" }
      }
    }
  },
  "required": ["sourceUrl", "uploadUrl", "authToken", "sourceName"]
}
```

#### 输出 schema

产物本身通过 `uploadUrl` 回传，`result` 只返回元数据：

```jsonc
{
  "type": "object",
  "properties": {
    "uploaded":      { "type": "boolean", "description": "产物是否已成功回传" },
    "format":        { "type": "string",  "description": "glb / gltf" },
    "bytes":         { "type": "number" },
    "triangleCount": { "type": "number" },
    "partCount":     { "type": "number" },
    "boundingBox":   { "type": "object", "description": "{min:[x,y,z], max:[x,y,z]}，单位同 options.unit" },
    "unit":          { "type": "string" },
    "warnings":      { "type": "array", "items": { "type": "string" } }
  },
  "required": ["uploaded", "format"]
}
```

**为什么不用 base64 内联**：座椅、白车身这类装配动辄几百 MB，base64 再涨三分之一，
塞进 JSON 请求体会同时压垮浏览器内存与 capability server 的 `readJsonBody`
（它是一次性 `Buffer.concat` 全量读入）。让 vektor3d 直接与 SDM 之间流式传输，
两端都不需要把整个文件放进内存。

> 若某些场景确实需要小文件内联，可以额外支持 `result.glbBase64`，SDM 侧会兼容；
> 但主路径请用 `uploadUrl`。

### 2.2 `geometry.inspect`（可选，次优先）

只读几何摘要，不产出模型文件。用于上传后立刻展示零件数、包围盒、单位、
以及是否存在开放边/自相交这类会影响后续网格划分的问题。

输入同上但**不需要 `uploadUrl`**；输出为 `2.1` 中除 `uploaded`/`format`/`bytes`
之外的那部分元数据，外加 `issues: [{level, code, message}]`。

### 2.3 `mesh.generate` / `mesh.check`（后续，不阻塞本次）

这两个是仿真流程真正需要的，但可以等 `geometry.convert` 落地之后再谈。
SDM 的编排引擎里已经预留了 `capability.invoke` 节点类型，能力一上线即可挂载，
DAG 定义只改一处。

---

## 3. 调用时序

```
用户在 SDM 页面选择本地 CAD 文件
        │
        ├─1─> 浏览器上传原始文件到 SDM         （建 sim_geometry_version，归档源文件）
        │
        ├─2─> 浏览器 POST localhost:23710/v1/jobs
        │        { capabilityId: "geometry.convert",
        │          input: { sourceUrl, uploadUrl, authToken, sourceName, options } }
        │     ← { jobId }
        │
        │     3─> vektor3d GET  sourceUrl        （直连 SDM 拉源文件）
        │     4─> vektor3d 转换 → GLB
        │     5─> vektor3d POST uploadUrl        （直连 SDM 推产物）
        │
        ├─6─> 浏览器订阅 /v1/jobs/{jobId}/events  （SSE 显示进度）
        │     ← 完成，result 含元数据
        │
        └─7─> 页面用 three.js GLTFLoader 渲染 SDM 上的 GLB
```

第 3、5 步是 vektor3d 与 SDM 直连，不经浏览器——这正是前面那个连通方向不对称
带来的好处：这个方向是通的。

`authToken` 由 SDM 签发、与该几何版本绑定、有效期以分钟计，且只授予
「读这一个源文件、写这一个产物」的权限。

---

## 4. 对既有机制的沿用

以下均**无需改动**，列出只为说明 SDM 侧的对接方式：

| 机制 | 说明 |
|---|---|
| `GET /v1/capabilities` | SDM 页面据此判断能力是否可用、展示预估耗时 |
| `POST /v1/jobs` | 提交转换任务，支持 `idempotencyKey` 幂等 |
| `GET /v1/jobs/{id}/events` | SSE 进度，用于展示转换进度条 |
| `POST /v1/jobs/{id}/cancel` | 用户取消 |
| `ready()` | 转换器未安装/未授权时提前拦截，不让用户白等 |
| Origin 白名单 + token | SDM 页面的源需要加入白名单 |

**需要 vektor3d 侧配合的一项配置**：把 SDM 页面的来源加入 capability server 的
Origin 白名单。生产地址为 `http://<集群主机>:8088`（IPv6 入口同理）。

---

## 5. SDM 侧的准备情况

- 数据模型已就位：`sim_geometry_version` 表含 `source_file_json`、`step_file`、
  `brep_file`、`lightweight_file`、`topo_summary_json` 等列。
- 编排引擎已就位：`capability.invoke` 节点会挂起等待浏览器代理完成，
  能力一上线即可编入流水线。
- 待能力可用后 SDM 侧补齐：文件上传/下载端点与短期令牌、浏览器代理客户端、
  three.js GLTF 预览。

---

## 6. 需要 vektor3d 团队确认的问题

1. `geometry.convert` 是否可行、预计排期？
2. 能否直接输出 glTF/GLB？若管线内部只能出 `.3dix`，能否在能力里加一步导出？
3. 转换器（DbitConvert）是否需要 CATIA 等原生环境？无环境时 `ready()` 应返回什么，
   以便 SDM 能给用户明确提示。
4. 装配体的处理：保留装配树（多 node 的 glTF）还是合并为单一网格？
   SDM 倾向保留，以便后续按零件着色与选择。
5. 单位与坐标系约定：是否统一输出 mm、Y-up？
