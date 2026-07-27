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
SDM 不关心中间过程。**这一步基本是机械映射**，见下。

#### `.3dix → glTF` 的映射（说明这不是新开发）

现有转换器的产物已经具备组装 glTF 所需的全部信息：

```
out/<job>/
├── bom.json        装配结构：fileList / asmList
├── manifest.json   零件清单：partKey / partNo / name / tdixPath / faceCount
└── parts/<partKey>/model.3dix   每零件一个：box / transform / faceShapes / pmi
```

对应关系：

| glTF 需要的 | 现有产物里的来源 |
|---|---|
| 场景图层级（node 树） | `bom.json` 的 `asmList` |
| node 的稳定零件标识 | `manifest.json` 的 `partNo` / `partKey`（来自 CAD，天然稳定） |
| node 的 transform | `model.3dix` 的 `transform` |
| mesh 的 position / index | `faceShapes[].vertices` / `vertexindices` |
| 材质颜色 | `faceShapes[].color` |
| 相同零件复用 mesh | 同一 `partKey` 的多个实例引用同一 mesh，各带自己 transform |

#### 为什么这一步必须在 vektor3d 侧完成，而不是把 `.3dix` 交给 SDM

`.3dix` 是 UTF-8 JSON。实测一个零件样本：1077 三角面 / 63,834 字节，
**约 59 字节每三角面**（且该样本 `normals` 为空、`vertices` 不跨 faceShape 共享，
真实情况更大）。对比：

| 格式 | 每三角面 | 5M 三角面的装配 |
|---|---|---|
| `.3dix`（JSON） | ~59 B | ~295 MB |
| glTF/GLB（二进制） | ~24 B | ~120 MB |
| GLB + Draco | ~3–5 B | **~20 MB** |

若把 `.3dix` 交给 SDM 自行转换，那 ~300 MB 的 JSON 就要跨网传输并 `JSON.parse`，
浏览器侧几乎必然失败；且需按零件拉 N 个文件而非 1 个。在源头转换，
网络与浏览器只承受最终那 ~20 MB。

**这不是"依赖外部团队"**——转换器由你们自己实现即可，只是应当部署在持有原始产物的
那一侧运行。

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

#### 装配体：整个装配输出为**一个** glTF，保留装配树

不要拆成"每零件一个文件 + 一份装配结构描述"。拆分意味着要另定义一套装配树格式，
而 glTF 2.0 的场景图本来就是干这件事的——为此新造一套自有格式，是把接口面平白扩大。

拆分想解决的问题，单文件内都有标准解法：相同零件（如 200 颗同规格螺栓）应由多个
node **引用同一个 mesh**、各带自己的 transform，几何只存一份；体积问题用 Draco 或
meshopt 压缩加 `maxTriangles` 预算解决。

但装配树要真正可用，以下三条是硬要求：

1. **每个 node 必须带稳定的零件标识**，不能只有显示名。SDM 后续要按零件赋材料、
   圈定分析范围、做网格划分设置，靠 `"零件_1"` 这类名字对不上。请在 node 的
   `extras` 中给出 CAD 侧的 part number 或实例路径，例如：

   ```jsonc
   "extras": {
     "partId":       "SEAT-FRAME-001",      // 稳定标识，重新转换后不变
     "instancePath": "Seat/Frame/Bracket_L", // 装配中的实例路径
     "sourceFile":   "Bracket.CATPart"
   }
   ```

   **「重新转换后不变」是关键**：用户在 v1 上做的零件级选择，若 v2 的标识变了就
   全部失效。请勿使用转换过程中生成的随机 id 或序号。

2. **相同零件复用 mesh**，不要展开成各自独立的几何。

3. **保留装配层级**，不要拍平成一层。座椅这类有明确子系统划分（骨架/滑轨/头枕/
   面套），拍平后无法按子系统操作。

> **何时重新考虑拆分**：单个装配的 GLB 超过约 200 MB，或浏览器加载出现明显卡顿。
> 届时的优先级是：先启用 Draco/meshopt 压缩 → 再做三角面预算分级 → 最后才考虑
> 按子系统拆文件。拆文件是最后手段，因为 SDM 侧还需相应新增零件表，
> 是数据模型层面的改动。

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
2. `.3dix + bom.json + manifest.json → 单个 GLB` 的封装步骤（见 2.1 映射表），
   工作量评估如何？从产物结构看应是机械映射，若有我们没看到的坑请指出。
3. 转换器（DbitConvert）是否需要 CATIA 等原生环境？无环境时 `ready()` 应返回什么，
   以便 SDM 能给用户明确提示。
4. 装配体（见 2.1 的装配小节）：能否保留装配层级、复用相同零件的 mesh、
   并在 node `extras` 中给出**重新转换后保持不变**的零件标识？
   最后一条是 SDM 做零件级操作的前提，也是最容易被忽略的一条。
5. 单位与坐标系约定：是否统一输出 mm、Y-up？
6. 是否支持 Draco / meshopt 压缩？大装配的传输与加载都依赖它。
