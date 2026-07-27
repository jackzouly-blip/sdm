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
| 场景图层级（node 树） | `bom.json` 的 `asmList`（边：`fromFilePath` → `toFilePath`） |
| node 的稳定零件标识 | `bom.json` `fileList[].PartNumber`（**不是 `partKey`**，见下方 ⚠️ 更正一） |
| node 的 transform | `bom.json` `asmList[].matrix`（**不是 `model.3dix.transform`**，见下方 ⚠️ 更正二） |
| mesh 的 position / index | `faceShapes[].vertices` / `vertexindices` |
| 材质颜色 | `faceShapes[].color` |
| 相同零件复用 mesh | 同一源文件的多个实例引用同一 mesh，各带自己 transform |

#### ⚠️ 更正一：`partKey` 不稳定，不能作为零件标识

实测两次转换的同一个文件（同一 `fileMd5=d6e191…`），`partKey` 分别是
`L-EC996598435A` 与 `L-BA454BFA5DFE`；`bom-pipeline.js` 里还专门有
`_canonicalizePartKeys()` 用 `fileName`/`partNo` 覆写它。它是每次运行的临时键。

因此实现取 **`PartNumber`**（CATIA 侧真实零件号）作为 `extras.partId`，
并附 `extras.sourceMd5` 供 SDM 判断内容是否变过；原始 `partKey` 仅作诊断字段
`extras.converterPartKey` 保留。

#### ⚠️ 更正二：实例变换在 `asmList` 的边上，不在 `model.3dix` 里

`asmList` 每条边形如：

```jsonc
{ "fromFilePath": "…/BG-Dumper.CATProduct",
  "toFilePath":   "…/2780-Black.CATPart",
  "matrix": "1,0,0,-396, 0,-1,0,-139.438827, 0,0,-1,478.922491",  // 3x4 行主序
  "instanceName": "2780-Black.1" }
```

这比契约设想的更好：几何按零件存一份、变换挂在边上，**"相同零件复用 mesh" 是数据
结构自带的**，不需要额外去重。实测样本 27 个零件 / 261 条装配边，复用比接近 10:1。

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

#### ⚠ 转换时**绝不能跨 faceShape 焊接顶点**

这是最容易踩、且后果最严重的一个坑。

`.3dix` 的 `faceShapes` 是按 **CAD 面** 切分的（`idpath = [bodyId, faceId]`），
每个面自带独立的 `vertices` 数组。两个面交界处的顶点分属不同 faceShape，
**位置相同但是两份**——硬边正是靠这个顶点重复来表达的。
现有 `meshBuilder.worker.ts` 的合并是纯拼接加索引偏移
（`shape.vertexindices[i] + vertOffset`），刻意没有做焊接，正是这个原因。

写转换器时看到"同一位置存在重复顶点"，很自然会想做去重优化——**一旦去重，
所有硬边被抹平**：倒角、棱线、加强筋全变成连续曲面。这个错误在简单零件上不明显，
到复杂装配才暴露，届时很难定位。

关于法线：样本中 `normals` 为空数组。这本身不是问题——只要顶点不被焊接，
按合并后的缓冲区计算逐顶点法线（即现有 `computeVertexNormals` 的做法），
边界上的两份顶点各自只累加本面的三角面法线，自然得到"面内平滑、面间起棱"。

> 建议在转换时算好法线并写入 glTF，而不是留空让渲染端各自计算——避免不同渲染端
> 实现不一致。但无论哪种，**前提都是顶点不被合并**。

另请注意：`normals` 是**逐顶点**的，长度与 `vertices` 对齐（而非与三角面数对齐）。

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

**接口已经部署上线，可以直接对接联调**，不是待建状态。

### 已就绪

| 项 | 状态 |
|---|---|
| 数据模型 | `sim_geometry_version` 表含 `source_file_json` / `step_file` / `brep_file` / `lightweight_file` / `topo_summary_json` |
| 源文件下载（对应契约的 `sourceUrl`） | `GET /api/sim/geometries/{gid}/download` 已上线 |
| 产物回传（对应契约的 `uploadUrl`） | `POST /api/sim/geometries/{gid}/lightweight` 已上线，multipart，字段 `file` + 可选 `meta`（JSON 字符串，对应 2.1 的输出 schema） |
| 网页渲染 | three.js + `GLTFLoader` 已接好，产物一回传即可预览 |
| 编排 | `capability.invoke` 节点会挂起等待，能力上线即可编入流水线 |

产物回传接口**会拒绝非 `.glb` / `.gltf` 的文件**并返回本文链接——这是刻意的，
避免私有格式被无意中引入。

预览器还会给出验收读数：装配层级深度、带零件标识的 node 数、去重后的 mesh 数；
层级被拍平或零件无标识时直接告警。因此第一个 GLB 回传后，
2.1 中那三条装配要求是否落实，打开即可判断。

### 已补齐（原「待补」两项）

- **短期令牌已实现**：`POST /api/sim/geometries/{gid}/convert-ticket` 签发
  `scope=geometry.convert`、绑定单个 `gid`、默认 30 分钟过期的受限令牌，
  契约中的 `authToken` 传的就是它。该令牌**在门户其余接口上一律 401**
  （默认拒绝，不是逐个接口排除），换个 `gid` 则 403，也不能用它再签新票据。
- **浏览器代理客户端已接入**：几何面板对 CAD 原生格式给出「轻量化」按钮，
  流程为 探测 vektor3d → 申请票据 → `POST /v1/jobs` → 轮询进度 → 刷新并可预览。
  用轮询而非 SSE：`GET /v1/jobs/{id}` 返回的 `progress` 是全量数组，信息与订阅
  等价，而 `EventSource` 设不了 `X-Vektor-Token` 头（配对令牌一启用就用不了）。

`sourceUrl` / `uploadUrl` 由**浏览器**用 `window.location.origin` 拼出，不由后端生成
——后端看到的 base_url（开发期 127.0.0.1:8000、生产期 nginx 反代后的内网地址）
都不等于浏览器与桌面真正能访问到的地址。

### ⚠️ 联调前必看：浏览器的「私有网络访问」会拦截这条链

Chrome 对**非安全上下文（http）的公网页面 → 127.0.0.1** 的请求有 PNA 限制，
表现与"没装 vektor3d"一模一样（fetch 直接抛错）。vektor3d 侧已回
`Access-Control-Allow-Private-Network: true`，但那只解决预检、不解决不安全上下文。

处理办法按推荐度：

1. **门户挂 HTTPS**（推荐）。Chrome 视 `http://127.0.0.1` 为可信来源，
   https 页面调它不算混合内容，PNA 也随之放行。
2. 企业策略 `InsecurePrivateNetworkRequestsAllowedForUrls` 放行门户地址。
3. 临时验证可开 `chrome://flags/#block-insecure-private-network-requests`。

几何面板在 http 访问时会直接把这条提示显示出来，不必靠猜。

### 联调环境

生产地址 `http://<集群主机>:8088`。需要 vektor3d 侧把该源加入 capability server
的 Origin 白名单（IPv6 入口同理）。具体主机与账号请联系 SDM 侧。

---

## 6. 需要 vektor3d 团队确认的问题 —— 答复见第 7 节

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

---

## 7. vektor3d 侧答复与实现状态

**`geometry.convert` 与 `geometry.inspect` 已实现并注册在既有 `/v1` 上，可以对接联调。**
自测：`pnpm run test:geometry`（装配导出 10 组 + 传输链 13 组断言）。
⚠️ **尚未在真机转换器上跑过整装配**，联调第一件事就是这个。

### 7.1 逐条答复

**① 可行性与排期** —— 已落地。三块能力仓内本就有，只是没串起来：
`.3dix → 合并网格`（`storage-service/file-cache.js` 的 mesh shard，XR 通道在用）、
`网格 → GLB 容器`（`xr-gateway/glb-exporter.js`）、
`矩阵规范化`（`cad-pipeline/assembly-transform.js`）。本次新增的是装配场景图、
在线收发链、以及把上面三块提成两条链共用的实现。

**② 有没有你们没看到的坑** —— 有三个：

- `partKey` 不稳定（更正一）。若按契约原文用它做 `partId`，SDM 侧零件级选择会在
  每次重新转换后全部失效——正是契约最担心的那个后果，只是原因不同。
- 装配的实例变换在 `asmList` 上（更正二），不在 `.3dix` 里。
- 桌面主链路转装配时会加 `--root-only`（只转根节点几何，子件各自有独立转换任务）。
  在线链**不能**沿用这条，否则产出的 GLB 只有根节点那点几何。已在能力实现里显式
  置 `rootOnly: false`。

顶点焊接那个坑不会踩：现有合并实现（`shared/tdix-mesh.js`）本来就是纯拼接 + 索引
偏移，逐顶点法线也依赖顶点不合并；自测里有一条断言专门守着它。

**③ 原生环境与 `ready()`** —— `ready()` 三态：

| 情况 | `ready` | `notReadyReason` |
|---|---|---|
| 已配置转换器且文件存在 | `true` | 无 |
| 未配置转换器 | `true` | 「未配置本地 CAD 转换器：仅支持 STEP/STP 输入（进程内 WASM 转换）；CATIA/NX/SolidWorks 等需在系统设置配置 DbitConvert」 |
| 配置了但文件不存在 | `false` | 「转换器不存在: <路径>」 |

即：**STEP/STP 不需要任何外部环境**（走进程内 WASM，跨平台），其余格式需要本机
DbitConvert。请把 `notReadyReason` 直接透给用户——它已经是可操作的人话。
注意第二行 `ready=true` 但带 reason，表示"受限可用"。

**④ 装配三条硬要求** —— 全部满足，且每条都有自测守着：

```jsonc
"extras": {
  "partId":       "SEAT-FRAME-001",          // = CAD 的 PartNumber，重新转换不变
  "instancePath": "Seat.CATProduct/Frame.1/Bracket_L.1",
  "sourceFile":   "Bracket.CATPart",
  "sourceMd5":    "d6e191397b4d80415649af8697b8531a",  // 内容指纹，判断是否真的变了
  "converterPartKey": "L-BA454BFA5DFE"       // 仅诊断用，每次转换都会变，勿依赖
}
```

- 层级保留，不拍平；`result.assemblyDepth` 给出深度供你们的预览器校验
- 相同零件复用 mesh；`result.partCount`（去重后的 mesh 数）与
  `result.instanceCount`（node 数）之比即复用比
- `result.identifiedNodeCount` = 带 `partId` 的 node 数，与 `instanceCount` 不等
  就说明有零件缺零件号，`geometry.inspect` 会以 `PART_ID_MISSING` 报出来

**⑤ 单位与坐标系** —— 输出 **mm**，场景 **Y-up**，但请注意实现方式：

几何数值保持源 CAD 坐标（**mm、Z-up**）**不做任何缩放或旋转**，Z-up → Y-up 的差异由
**根节点上的一次旋转矩阵**吸收。这样 GLB 里的顶点数值与 STEP/BREP、与
`result.boundingBox` 完全一致，SDM 后续赋材料、网格划分、尺寸标注都不必换算。
`result.unit` / `result.upAxis` 与 GLB 的 `asset.extras` 都会声明这一点。
`result.boundingBox` 报的是**源坐标系（Z-up, mm）**下的值，含实例变换。

**⑥ Draco / meshopt** —— **本期未实现**，仓内没有任何减面/压缩实现，需新引依赖。
`options.maxTriangles` 同理：传了不会报错，但只会在 `warnings` 里提示"未减面，
已按原始精度输出"，**不会静默减面**。若大装配的传输/加载确实撑不住，
按契约 2.1 的优先级排序，下一步就做 Draco；请在联调后给出真实的体积与加载耗时，
以此决定是否立项。当前缓解手段：GLB 本身是二进制（相对 `.3dix` 约 1/2.5），
且 ≥150MB 的源文件会自动降精度转换（`--quality low`）。

### 7.2 SDM 侧需要配合的两件事

**① 装配必须以 zip 上传。** `.CATProduct` 只是引用壳，外部 `.CATPart` 不在其中；
单个 `sourceUrl` 传一个 CATProduct 只会转出一个空装配。请打包为 zip
（`sourceName` 以 `.zip` 结尾，或直接靠文件头识别，两者都支持）。

入口文件的选择规则是确定的，**选不出来就报错并列出候选，绝不猜**：

1. 传了 `options.entryFile`（zip 内相对路径）→ 用它
2. 否则包内装配文件（`.CATProduct`/`.SLDASM`/`.asm`/`.iam`/`.3dxml`）恰好一个 → 用它
3. 否则 CAD 文件恰好一个 → 用它
4. 否则报错并列出候选，请求指定 `entryFile`

zip 解包会丢弃目录穿越条目，文件名支持 UTF-8 与 GBK（Windows 中文环境打的包）。

**② Origin 白名单。** 把 SDM 页面地址加进「系统 → 能力服务」的白名单，
默认拒绝一切浏览器跨源调用。

### 7.3 已知缺口（联调不阻塞，但请知悉）

- **取消暂不能中断转换**：`POST /v1/jobs/{id}/cancel` 会把作业标记为已取消，
  但正在跑的转换器子进程不会被打断（现有 AI 能力也是同样的限制）。
- **STEP 装配会被拍平**：STEP 走进程内 WASM 转换，产出单一零件、无装配树。
  需要装配树的 STEP 请改传原生格式，或等这条路径接上原生转换器。
- **未做真机整装配验证**：合成产物自测通过，真实 CATIA 装配的表现待联调确认。

### 7.4 实现落点

| | 落点 |
|---|---|
| 在线链（下载 → 解包 → 转换 → 回传） | `packages/app-electron/src/geometry-capability.js` |
| 装配 → glTF | `packages/cad-pipeline/src/gltf-assembly-exporter.js` |
| GLB 容器写出（与 XR 通道共用） | `packages/shared/src/glb-writer.js` |
| 3dix → 合并网格（与桌面渲染共用） | `packages/shared/src/tdix-mesh.js` |
| 无状态转换执行（与桌面主链路共用） | `packages/cad-pipeline/src/convert-runner.js` |
| 能力注册 | `packages/app-electron/src/capability-server/index.js` |
| 自测 | `pnpm run test:geometry` |
