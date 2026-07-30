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

### 2.3 `mesh.generate`（已上线）

CAD 原文件 → 本机 BETA CAE ANSA 无头批处理 → 求解器网格文件，产物直传回 SDM。
调用通路、鉴权、文件收发方向与 `geometry.convert` 完全一致；作业跑在独立的
算法队列上，不会与 AI 能力(需求分析等)互相排队。

三条与几何能力不同的约定：

1. **输入用 CAD 原文件**（.CATPart/.stp/.igs 等，zip 装配包同 2.1 规则）：
   ANSA 自带 CAD 转换器直读，不经 vektor3d 的转换链二次转换。
2. **网格参数与质量准则是文档**：SDM 质量卡以 `.ansa_mpar`（网格参数）/
   `.ansa_qual`（质量准则）附件形态透传给 ANSA，vektor3d 不解析其内容。
   都不给时按 ANSA 默认参数（可用 `options.elementLength` 给目标单元尺寸）。
3. **质量违例不算失败**：`violationsRemain: true` 表示仍有质量违例或未网格化的
   宏面，网格文件照常回传，由 SDM 决定是否接受。

输入（完整 schema 见 `GET /v1/capabilities`）：

```jsonc
{
  "sourceUrl":          "…/download",        // CAD 原文件（必填）
  "authToken":          "…",                 // 短期票据（必填）
  "sourceName":         "xxx.CATPart",       // 原始文件名（必填）
  "uploadUrl":          "…/mesh-file",       // 求解器网格文件回传（必填）
  "ansaUploadUrl":      "…/mesh-ansa",       // 强烈建议：ANSA 原生库（.ansa，网格正本）
  "previewUploadUrl":   "…/mesh-preview",    // 可选：网格预览 GLB（见下）
  "reportUploadUrl":    "…/mesh-report",     // 可选：质量统计报告（HTML）
  "meshParamsUrl":      "…",                 // 可选：.ansa_mpar 文档
  "qualityCriteriaUrl": "…",                 // 可选：.ansa_qual 文档
  "options": {
    "solverFormat": "nastran",               // nastran（默认）| abaqus
    "elementLength": 5,                      // 目标单元尺寸 mm（无 mpar 时生效）
    "meshType": "surface",                   // surface（默认）| volume（几何须构成封闭体）
    "timeoutSeconds": 3600                   // 默认 3600，上限 14400
  }
}
```

`meshType: "volume"` 在导入期直接从 CAD 实体建体（CATIA/NX/SolidWorks/STEP 等
经 CT 转换器），随后四面体填充（TETRA RAPID，失败自动改试 TETRA FEM）。
三条体网格特有约束：

- 几何必须是封闭实体；钣金/开放曲面会失败并说明原因。
- 网格参数/质量准则文档是面网格会话专属，体网格模式暂不套用（warnings 里会标注）；
  体网格的质量结论请用 `mesh.check`。
- 细小特征（如螺纹）可能因边界面网格自相交而失败，错误信息会说明；
  此时应简化几何或等参数文档细化能力（后续阶段）。

`meshType: "midsurface"` 做**中面抽取**（MidSurfAuto）：从实体皮面几何直接生成
**带厚度的中面壳网格**（厚度赋在属性上），是薄壁件壳分析的工程实践形态。
两个专用选项：`options.minThickness`（实体最小壁厚 mm，默认 1.0，与实际不符会
识别失败）、`options.exactMiddle`（精确中面，默认关）。适用边界（真机实证）：

- **适用**：壁厚均匀的薄壁件（钣金/折弯/铸件薄壁区）——铜巴件 4 秒出带厚度中面；
- **不适用**：厚实体为主的机加件——自动中面算法可能数小时不收敛
  （相机支架 26MB 实测 60 分钟未收敛），超时错误里会给出改用
  surface/volume 的建议。这类零件的壳模型依赖工程师手工中面，不在自动化范围。

关于**边界层/包面**（CFD 前处理）：无头模式基础设施已验证可行（Wrap/Layers
会话与场景创建、运行均稳定），但默认参数下产不出有意义的网格——包面长度、
层定义等必须来自真实 CFD 需求。当前不提供独立的 meshType；有 CFD 场景时，
经 `meshParamsUrl` 传入含 wrap/layers 参数段的完整 `.ansa_mpar` 即可落地。

输出要点：`engine/engineVersion`（ANSA 版本，供追溯）、`elementCount/shellCount/
solidCount/nodeCount/partCount`、`violationsRemain`、
`meshParamsApplied/qualityCriteriaApplied`（文档是否真的套用成功）、`warnings`。

就绪前提：该桌面节点装有 ANSA 且许可证可达（`ready()` 会如实报告），
运行一次作业占用一个 ANSA 许可席位。取消作业会真正终止 ANSA 进程。

### 2.4 `mesh.check`（已上线）

对既有网格按质量卡做独立质量检查，闭合「生成 → 审核」环。只回统计结论，
不产网格文件。输入：`meshUrl / meshName / authToken`（必填），
`qualityCriteriaUrl`（可选，`.ansa_qual` 质量卡；不传则按 ANSA 默认准则检查，
并在 `qualityCriteriaApplied: false` 与 warnings 里如实标注），
`options.meshFormat`（nastran 默认 | abaqus）。

输出要点：`offCounts`（`{shells: {准则名: 违例数}, solids: {…}}` 逐准则统计）、
`violationCount`（违例单元总数）、`passed`（违例为 0）、
`elementCount/shellCount/solidCount/nodeCount`。

**网格预览是"审查级"的**：GLB 从 Nastran 派生文件构建——四边形单元保持
四边形观感、含**真实单元边线**（LINES 图元）、按 PID 分组上色，用现有 glTF
浏览链直接渲染。超大网格（默认 >50 万面）只出面片不出边线并在 warnings 标注；
`.nas` 解析失败时回退 STL 三角面片预览（`previewSource` 字段如实标注来源）。

**`.ansa` 是网格的正本**：几何+网格+属性+厚度全量保留。SDM 落库之后，
手工微调（工程师用 ANSA 打开无损）、装配合并、按需导出任意求解器格式
（`mesh.export`）都以它为源；`uploadUrl` 回传的求解器文件只是派生产物。
每次生成都应传 `ansaUploadUrl` 保存正本。

### 2.5 `mesh.params.derive`（已上线）

把**人读的质量卡文档**（.docx/.xlsx/.pdf 等）解析成 ANSA 能直接吃的参数文件，
打通「SDM 质量卡是普通文档」的真实场景。AI（agents/cae-mesh-quality-analyst.md）
只负责从文档抽结构化参数；`.ansa_mpar`/`.ansa_qual` 的文件结构由确定性渲染器保证
（模板取自 ANSA 导出的默认文件，只补丁文档明确给出的值）。

输入：`projectId / sourceUrl / sourceName`（必填，原件归档进对应工作区），
`authToken / hint` 可选。属 AI 能力（`kind: ai-skill`），走 AI 队列，需引擎就绪。

输出要点：`ansaMpar / ansaQual`（渲染好的文件**文本**，SDM 落库为附件后，
经 `meshParamsUrl / qualityCriteriaUrl` 喂给 `mesh.generate`，或喂给 `mesh.check`；
无可套用内容时为 null，绝不返回"全默认值"文件冒充质量卡配置）、
`meshParams / qualityCriteria`（结构化抽取结果，供 SDM 展示与人工核对）、
`unmapped`（映射不进 ANSA 准则表的条目，原文保留）、
`renderWarnings / notes`（渲染丢弃项与 AI 的疑问，逐条可读）。

推荐编排：质量卡上传 → `mesh.params.derive` → 工程师核对结构化结果 →
`mesh.generate`（带派生文件）→ `mesh.check`（复核）。

### 2.6 `mesh.export`（已上线）

把 `.ansa` 原生库（网格正本）按需导出为求解器格式——同一份网格反复导出
不同格式，**不重新划网格**。输入：`ansaUrl / uploadUrl / authToken`（必填），
`options.solverFormat`：`nastran`（默认，.nas）| `abaqus`（.inp）|
`lsdyna`（.k）| `ansys`（.cdb）| `optistruct`（.fem）。

可选 `previewUploadUrl`：顺带产出网格预览 GLB(带真实单元边线)——
这是 **.ansa 手工修改检入后重新出预览**的通道；主格式非 nastran 时
ANSA 会话内会顺带导一份 Nastran 作为渲染源。

输出要点：`solverFormat / solverFileBytes / elementCount / shellCount /
solidCount / nodeCount`（与生成时的正本自洽）、`previewUploaded`。
注意 `.ansa` 有版本性：由更高版本 ANSA 保存的库低版本打不开，错误信息会说明。

### 2.7 `mesh.classify`（已上线）

逐零件形态分类 → 网格策略建议，供 SDM 编排「逐零件网格」时选策略。
输入与 `geometry.inspect` 相同（`sourceUrl / sourceName`，zip 装配包同规则），
可选 `options.thresholds` 覆盖阈值。

分类信号（从转换产物的三角网格确定性计算，不经 AI）：
**水密性**（边界边占比）、**平均壁厚**（2V/A）、**尺度比**（面内尺度/壁厚）、
**实心度**（V/包围盒体积）。规则与真实锚点件对齐：

| 形态 | 建议 meshType | fallback |
|---|---|---|
| 不水密 / 无体积（曲面模型） | `surface` | — |
| 封闭 + 薄壁均匀 | `midsurface` | `volume` |
| 封闭 + 厚实 | `volume` | `surface` |
| 封闭但实心度极低（薄壁结构外皮/框架，等效壁厚失真） | `surface` + **needsReview** | `volume` |

**二级复核（BREP 壁厚分布）**：一级是网格统计（毫秒级、均值口径），对变厚度件与
空心包络会失真。`options.refine`（`auto` 默认/`always`/`never`）控制是否用
python 特征提取的**壁厚分布**（主导壁厚/占比/离散度）复核：均匀薄壁 → midsurface
坐实并给出 `recommendedMinThickness`（直接喂中面抽取）；变厚度件 → 强制人工；
均匀厚壁 → volume 坐实。约束：仅 STEP 源可用、分布是文件级的（只修正低置信的件，
不覆盖一级高置信判定）、本机需 PythonOCC（不可用时优雅降级并留痕）。

输出：`parts[]`（逐零件/逐体 `meshType / fallback / confidence / needsReview /
refined? / recommendedMinThickness? / reasons[] / signals{}`）与
`summary`（各策略计数 + 待人工数）。
**合并 STEP（整装配一个文件）会自动做连通域拆体**：一个"零件"里多个不相连的体
各自独立分类，`partId` 形如 `xxx#body-2`，`signals.bbox`（全局坐标）用于与
`mesh.inventory` 的零件清单对齐。`needsReview: true` 的零件**建议进人工桶**。

### 2.8 `mesh.inventory`（已上线）

用 **ANSA 产品树**列出 CAD 文件的零件清单——合并 STEP 逐零件编排的权威拆解口径
（ANSA 认出的结构就是工程师在 GUI 里看到的结构）。只导入不划网格，秒级到十秒级。
输入同 `mesh.classify`（`sourceUrl / authToken / sourceName`）。

输出：`parts[]`（`{index, id, name, moduleId, faceCount,
bbox:[minx,miny,minz,maxx,maxy,maxz]}`）。`name` 直接作为 `mesh.generate` 的
`options.partFilter`；`bbox` 与 `mesh.classify` 拆体结果按包围盒对齐。

### 2.9 `mesh.merge`（已上线）

把多份**逐零件的 `.ansa` 网格正本**合并回装成一个装配模型——总体策略的回装环节。
零件由 `mesh.generate(partFilter)` 生成时已保持装配全局坐标，合并只拼库不摆位；
节点/属性/材料/集合的 ID 冲突一律 offset 错开（逐零件各自从 1 号编起是常态，
keep-old/keep-new 都会静默丢数据）。

输入：`ansaUrls[]`（≥2，第一个为合并基底）、`uploadUrl`（merged.ansa 回传，必填）、
可选 `solverUploadUrl + options.solverFormat`（顺带导出合并后的求解器文件）、
可选 `previewUploadUrl`（合并网格预览 GLB）。

输出要点：`mergedCount / partCount / elementCount / nodeCount`（应等于各零件之和，
逐零件网格不共节点）、`ansaFileBytes`。合并正本仍是 `.ansa`，
后续手工微调 / `mesh.export` / `mesh.check` 都照常适用。
**注意**：合并只负责几何回装，零件间的连接（螺栓 BEAM/焊点）不在本能力范围，
由工程师在 ANSA 中处理或等后续连接能力。

### 2.10 `mesh.checkout`（已上线）

人工回路的**取出端**：把 SDM 上的 `.ansa` 正本下载到本机受管工作副本区
（`<dataDir>/mesh-checkouts/<checkoutId>/`），并**启动本机 ANSA GUI 打开它**。
这是能力服务里刻意的有状态例外——本地保存的是工作副本，**正本仍在 SDM**；
副本与校验和记录在 manifest，跨应用重启可找回。

输入：`ansaUrl / authToken`（必填）、`fileName`、`meta`（业务标注，原样保存）。
输出：**`checkoutId`（SDM 必须保存，检入靠它）**、`localPath / sha256 / launched`。

### 2.11 `mesh.checkin`（已上线）

人工回路的**提交端**：工程师在 ANSA 里改完、保存到原位后，SDM 发起检入——
按 `checkoutId` 找回工作副本，hash 比对判断是否真有改动（未改动**如实标注但不拦截**），
作为新版本上传回 SDM；可选 `previewUploadUrl` 经 ANSA 重出修改后的预览 GLB。
**显式动作，绝不做文件监听自动上传**——半成品被自动同步的风险远大于多点一次按钮。

输入：`checkoutId / uploadUrl / authToken`（必填）、`previewUploadUrl`（可选）。
输出：`changed / uploaded / sha256 / previewUploaded / elementCount`（预览重出时
顺带清点，供 SDM 展示改动后的规模）。

**合并 STEP 的完整编排**：

```
上传 → mesh.inventory(零件清单,ANSA 口径)
     + mesh.classify (逐体策略建议,含 bbox)
     → SDM 按 bbox 对齐两份清单,得到「零件 → 策略」表
     → 逐零件 mesh.generate(partFilter=零件名, meshType=建议值, ansaUploadUrl=…)
       ——零件保持装配全局坐标,每件出独立 .ansa 正本
     → 失败/needsReview 的零件进人工桶(工程师在 ANSA 处理)
     → mesh.merge(逐零件 .ansa → 装配正本,可顺带出求解器文件与预览)
     → 连接建模/手工微调:mesh.checkout(本机 ANSA 打开)
        → 工程师修改保存 → mesh.checkin(新版本 + 新预览回 SDM)
     → mesh.check 复核 → mesh.export 交付
```

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

**几何链（2.1 / 2.2）**

| 项 | 状态 |
|---|---|
| 数据模型 | `sim_geometry_version` 表含 `source_file_json` / `step_file` / `brep_file` / `lightweight_file` / `topo_summary_json`，另加 `part_inventory_json`（2.8 产出）与 `mesh_strategy_json`（2.7 产出） |
| 源文件下载（对应契约的 `sourceUrl`） | `GET /api/sim/geometries/{gid}/download` 已上线（同时接受几何票据与网格票据） |
| 产物回传（对应契约的 `uploadUrl`） | `POST /api/sim/geometries/{gid}/lightweight` 已上线，multipart，字段 `file` + 可选 `meta`（JSON 字符串，对应 2.1 的输出 schema） |
| 网页渲染 | three.js + `GLTFLoader` 已接好，产物一回传即可预览 |
| 编排 | `capability.invoke` 节点会挂起等待；**浏览器代理循环尚未接线**，故网格链先走页面按钮直调（与几何链同一模式） |

**网格链（2.3~2.11）—— 逐字段落点**

| 契约字段 | SDM 端点 / 落库列 |
|---|---|
| 网格票据（绑 gid，默认 2h，上限 8h） | `POST /api/sim/geometries/{gid}/mesh-ticket` → `{token, source_path_suffix, mesh_path_prefix}` |
| `sourceUrl`（生成/清点/分类的输入） | `GET /api/sim/geometries/{gid}/download` |
| `ansaUploadUrl` → **正本** | `POST …/meshes/{mid}/artifact/ansa` → `sim_mesh_version.ansa_file` |
| `uploadUrl`（求解器文件） | `POST …/meshes/{mid}/artifact/solver` → `solver_file` + `solver_format`（取自 `meta.solverFormat`） |
| `previewUploadUrl` | `POST …/meshes/{mid}/artifact/preview` → `preview_file`（只收 .glb/.gltf） |
| `reportUploadUrl` | `POST …/meshes/{mid}/artifact/report` → `report_file`（只收 .html） |
| `ansaUrl`（export/check/merge/checkout 的输入） | `GET …/meshes/{mid}/artifact/ansa` |
| 2.7 `mesh.classify` 产出 | `PUT /api/sim/geometries/{gid}/analysis` → `mesh_strategy_json` |
| 2.8 `mesh.inventory` 产出 | 同上 → `part_inventory_json`；`parts[].name` 直接作为 `options.partFilter` |
| 2.9 `mesh.merge` 溯源 | `sim_mesh_version.source_mesh_ids_json` |
| 2.10 `checkoutId` | `POST …/meshes/{mid}/checkout` → `checkout_id` / `checkout_by`（**排他**：被他人占用时 409） |
| 2.11 检入 | `POST …/meshes/{mid}/checkin` 释放占用；新版本文件仍走 `artifact/ansa` |
| 作业状态回写 | `PATCH …/meshes/{mid}` → `status`（generating/ready/failed/checked-out）+ `quality` |

网格作业先以 `status='generating'` 登记一行再跑——作业动辄几十分钟，
页面必须先有行才能显示进度；失败原因写进 `quality.summary`（页面本就在读它）。
前端入口在 `MeshPanel.vue`（嵌在几何版本行下：网格隶属几何版本）。

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

### ⚠ 待 vektor3d 侧落实：进度必须分阶段回报

`progress` 数组当前**实际上是空的**——作业从提交到终态之间没有任何回报，
页面只能显示"转换中"。这在失败时代价很高：2026-07-30 一次 26 MB STEP 的转换以
`socket hang up` 告终，而门户侧证据显示源文件已被完整取走（nginx 记满
26,787,727 字节、200，与磁盘大小一致）、产物回传请求从未到达、nginx 错误日志为空
——即"故障发生在 vektor3d 拿到文件之后"，但**具体死在哪一步只能靠排除法推断**。
有分阶段回报，这类问题第一眼就能定位。

请在 `POST /v1/jobs` 之后、终态之前，至少按下列节点各追加一条 `progress`
（`step` 用固定英文枚举便于判定，`detail` 放人话与数量）：

| `step` | 何时追加 | `detail` 建议 |
|---|---|---|
| `accepted` | 作业进入队列/开始执行 | 队列位置 |
| `downloading` | 开始拉 `sourceUrl` | 已收字节 / 总字节，**分段刷新**（大文件尤需） |
| `downloaded` | 源文件落地 | 实际字节数、落地路径 |
| `parsing` | 开始解析 CAD | 格式、零件数（可用时） |
| `converting` | 开始生成 glTF/GLB | 阶段内进度 |
| `uploading` | 开始 POST `uploadUrl` | 产物字节数 |

要求两条：

1. **失败时 `error` 要带上失败阶段与底层原因**，而不是只有一句网络层措辞。
   `socket hang up` 这类 Node 原文若不指明是"拉源文件时"还是"回传产物时"，
   在跨三方链路里无法定位。
2. **长阶段要持续刷新**（`downloading` / `converting` 建议 ≥1 次/2 秒，与门户轮询同频）。
   静默超过一分钟的阶段，用户只能理解为"卡死"。

门户侧已按上述格式展示：阶段时间线（含每阶段耗时）在转换期间实时刷新，
失败时保留现场并标出断点；`step` 名未在上表中也会原样展示，不必等门户改代码。

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
