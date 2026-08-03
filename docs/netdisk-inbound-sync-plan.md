# 网盘入站同步方案（客户数据经百度网盘进平台）

## 背景与目标

客户通过门户 HTTP 上传大文件太慢。百度网盘客户端有秒传/多线程加速，客户本就习惯用它传大文件。
因此希望：客户把数据放进网盘并分享出来 → 平台持续把新增数据拉到集群 → 客户在门户里把文件挪进自己的工作目录。

## 现状盘点：已有的地基

出站链路已经很完整，本方案**复用而非重写**：

| 已有 | 位置 | 复用点 |
|---|---|---|
| 分享转存全链路 POC | `backend/_poc_share_download.py` | **本方案的直接原型**：verify/list/transfer/dlink 五步都有了 |
| OAuth（token 持久化/刷新） | `baidu_uploader/auth/oauth.py` | 下载环节直接用 |
| 网盘 API 客户端 | `baidu_uploader/api/client.py` | `list_dir`/`get_fsid` 可用；需补 `filemetas(dlink)` + 下载 + 删除 |
| 内容 MD5 / 断点状态 | `baidu_uploader/state/store.py` | 增量判据同源 |
| 后台常驻扫描线程 | `app/netdisk/streamer.py` | 定时轮询线程的现成骨架 |
| 异步任务 + 进度推送 | `app/tasks/manager.py` + `/ws/tasks` | 同步任务挂上去，前端进度条免费 |
| 以用户身份落盘 | `app/privilege/actas.py` | 下载文件的属主/权限正确性 |
| 失败重试看门狗 | `app/netdisk/autoshare.py::retry_failed_uploads` | 退避+封顶的重试模式照搬 |

---

## 一处必须先纠正的认知

> "将用户共享出来的文件夹添加到我们的网盘空间里，后面用户再添加的内容我们也能识别到"

**转存（`share/transfer`）是一次性复制，不是挂载、不是同步。** 转存完成的那一刻，
我们账号里的副本和用户的原目录就是两棵独立的树，用户后续往原目录加文件，我们的副本**不会有任何变化**。
百度网盘个人版也**没有**"接受他人共享目录并挂载"的开放接口（开放平台只有文件增删查改、分享创建、秒传）。

**但分享链接本身是活的。** 分享一个文件夹时，分享的是该目录的引用；
访问分享时 `share/list` 是**实时列出该目录当前内容**的。因此"用户后续新增的内容我们能识别到"这件事成立，
只是识别的入口是**分享链接**，不是我们账号里那份副本。

### 由此修正的架构

```
用户的分享文件夹  ← 唯一的同步源，靠定时重新 share/list 发现新增
        │ share/transfer（只转存清单里没见过的 fs_id）
        ▼
我们账号 /apps/HPC/inbox/<user>/<share_id>/<批次时间戳>/   ← 中转区，按批次分目录
        │ OAuth filemetas → dlink 下载
        ▼
集群 <fs_root>/hpc-portal/netdisk-inbox/<user>/<源名>/
        ▼
用户在门户里 move 到自己的工作目录
```

**中转区保留、定期人工清理**（平台账号容量充足，已决策）。由此带来两点设计约束：

**① 中转区必须按批次分目录，否则 `ondup` 会咬人。**
中转区不清空时，"同名不同版本"会出事：客户把 `model.k` 换成新版本（**新 fs_id、同名**），
我们的清单看到新 fs_id 判定需要转存，但 `ondup=skip` 发现中转区已有同名文件 → 跳过转存 →
接着下载到的是**上一版的旧文件**，且状态显示成功。**静默拿错数据，极难发现。**
（换成 `ondup=newcopy` 也不行，会变成 `model(1).k`，我们按路径就找不到它了。）

对策：每批转存落在 `<share_id>/<批次时间戳>/` 独立目录下，同名永不冲突，
`ondup` 用哪个值都无所谓。清单里记 `batch_id`，下载时按批次目录定位。

**② 中转区顺带成了历史归档**，这是保留策略白捡的好处：服务器端文件若被误删，
可以直接从中转区重新下载，不用回头麻烦客户重新分享。清单里保留 `our_path` 即可支持。

---

## 端到端流程

```
① 用户在网盘里建文件夹、放数据、分享出来（拿到链接 + 提取码）

② 用户在门户「网盘同步」页粘贴链接 + 提取码，选择要同步的子目录、绑定服务器落点
      平台立刻做一次 verify + list 验证链接可用，并落库

③ 平台轮询（默认 10 分钟，可手动触发）
      share/verify（拿 BDCLND）→ share/list 递归列目录
      → 与本地清单按 fs_id 比对 → 得出"新增文件"

④ 增量转存（分批，每批 ≤ 100 个）
      为本轮生成 batch_id（时间戳），转存到
      /apps/HPC/inbox/<user>/<share_id>/<batch_id>/

⑤ 下载（OAuth 官方接口）
      list 该批次目录 → filemetas(dlink=1) → Range 分段下载
      → 写 <落点>/.hpc-part/xxx.part，校验 size/md5 后 rename
      → 以用户身份落盘（call_as_user，属主/权限正确）

⑥ 校验通过 → 清单标记 done（**中转副本保留**，供误删重取与人工清理）

⑦ 用户在文件浏览器里把文件 move 到工作目录
```

**落点约定**：`<fs_roots[0]>/hpc-portal/netdisk-inbox/<username>/<源名>/`，
与 `sim_workdir_base_dir` 同样的派生思路——不让用户手填集群绝对路径，且保证天然落在 `fs_roots` 白名单内。

---

## ⚠️ Phase 0：先实测五件事（**方案成败在此，1–2 天**）

这条路大量依赖百度**网页私有接口**，没有文档保证，全靠实测。写业务代码前必须做完：

1. **分享文件夹后新增文件，`share/list` 能否看到**（**最关键，决定"持续同步"是否成立**）
   - 分享一个文件夹 → 转存一次 → 往原文件夹里加新文件 → 重新 `share/list`
   - 确认新文件出现在列表里、fs_id 是新的
   - 同时确认：用户**删除并重建**同名文件夹后链接是否失效（预期会，需要有失效提示）

2. **转存落点能否指向 `/apps/HPC/`，以及 OAuth 能否读到**
   配置里 `remote_dir: /apps/HPC/backup` 说明当前是**普通权限应用**，OAuth 只保证能读写 `/apps/HPC/`。
   - `share/transfer` 的 `path` 参数指向 `/apps/HPC/inbox/...` 是否被接受
   - 若不被接受 → 只能转存到普通目录，那就必须确认 **OAuth 能否 `list`/`dlink` 应用目录之外的路径**
   - `_poc_share_download.py` 的 `BAIDU_TRANSFER_DIR` 默认 `/`，第 [6] 步用 OAuth 列的就是它。
     **这个 POC 当初实际跑通到第几步、`transfer_dir` 设的什么？** 如果第 [6] 步真跑通过且落点在 `/`，
     那全盘读取就是可行的，这一项直接结论。仓库里没留运行记录，需要你确认。

3. **dlink 实际下载吞吐**
   百度对 dlink 有账号等级相关限速。如果只有几百 KB/s，"网盘比 HTTP 上传快"的前提不成立，方案归零。
   - 在**生产服务器**上测，不要在本地测
   - 平台账号是否需要 SVIP？测出明确结论（这直接是一笔预算）
   - 顺带测 Range 是否支持、单账号并发连接数上限

4. **转存配额的实际数值**
   - 单次 `fsidlist` 文件数上限（社区常见说法 100，需实测）
   - 每日转存次数/容量限制
   - 触发限制时的 errno（已知 `-33` 转存数量超限、`12` 部分失败、`-6` 鉴权失败）
   - 决定分批大小和轮询节奏

5. **cookie 有效期**
   - BDUSS / STOKEN 能活多久？多久要人工换一次？
   - BDCLND（提取码凭证）有效期，是否每轮都要重新 `verify`
   - 这决定运维负担有多重

### 探针已就绪

`backend/_poc_pull.py` 已实现，且**直接调用生产模块**（`share_client.py` / `download.py`），
不是一次性脚本——跑通它等于跑通了后续要用的代码。

```bash
# Q1（上）：列出分享内容并存快照
HPC_FS_ROOTS=/data .venv/bin/python _poc_pull.py scan "<分享链接>" "<提取码>"
```
```bash
# …让客户往分享文件夹里再放一个文件…
# Q1（下）：重新列出并 diff —— 这是"持续同步"能否成立的判据
HPC_FS_ROOTS=/data .venv/bin/python _poc_pull.py rescan "<分享链接>" "<提取码>"
```
```bash
# Q2/Q3/Q4/Q5：转存到中转区 → OAuth 下载 → 逐文件计时 + md5 三方比对
HPC_FS_ROOTS=/data .venv/bin/python _poc_pull.py pull "<分享链接>" "<提取码>" \
    --user leiyou --dest /data/tmp/pull-probe --limit 3
```

需要先配 `HPC_NETDISK_BDUSS` / `HPC_NETDISK_STOKEN`（或环境变量 `BAIDU_BDUSS` / `BAIDU_STOKEN`）。
探针会把每一步的 errno 与原始返回打出来，失败时直接给出是链接问题、cookie 问题还是配额问题。

### 实测结论（2026-08-03，生产环境 71.100，平台账号为 SVIP v5）

| 问题 | 结论 |
|---|---|
| **Q1 分享新增可见** | ✅ **成立**。分享后往目录里新增 5 个文件，`share/list` 全部检出。「定时轮询 + 按 fs_id 增量转存」这个前提没问题。 |
| **Q2 转存落点 + OAuth 可读** | ✅ 转存到 `/apps/HPC/inbox/...` 成功，OAuth 能 `list` + 取 `dlink` + 下载。**但 `share/transfer` 不会创建目标目录**，落点不存在直接 `errno=2`，必须先 mkdir。 |
| **Q3 dlink 吞吐** | ⚠️ **未定论，需用 GB 级样本重测**。样本仅 17.5 MiB 且方差极大：11.0 MiB 跑出 280 KiB/s（40.3s），而 6.4 MiB 跑出 4.2 MiB/s（1.5s），相差 15 倍。整体 402.8 KiB/s，但小文件被每请求约 0.9s 的固定开销主导，这个数字没有代表性。 |
| **Q4 单次转存上限** | 5 个 fs_id 一批通过（0.9s）。真实上限未探到，`netdisk_pull_batch` 暂维持 100。 |
| **Q5 md5 是否可信** | ❌ **不可信**。`filemetas` 与 `share/list` 返回的是**同一个混淆串**，含 g–v 等非十六进制字符（如 `cbd02d4d0vd262cd69aa1cd072c3ac38`），与本地实算 md5 全部不符。 |

**Q5 的后果值得单独记一笔**：md5 原本是防"拿到中转区旧版本"的第二道闸。它失效后，
**批次目录隔离成了唯一防线**——`remote_batch_dir()` 的正确性因此变得更关键，不能为了
"整洁"把批次目录合并掉。代码里 `is_real_md5()` 会识别非法 md5 并降级为只校验 size，
若百度将来改回真 md5，校验会自动重新生效，无需改代码。

**仍待完成**：Q3 用一个 GB 级文件重测；Q4 用 100+ 文件试探真实上限。

---

## 实现设计

### 目录与文件

```
backend/app/netdisk/
  engine.py          # 已有，出站
  streamer.py        # 已有，出站流式
  autoshare.py       # 已有，出站
  share_client.py    # 新增：网页私有接口封装（bdstoken/verify/list/transfer），由 POC 提炼
  download.py        # 新增：filemetas(dlink) + Range 断点续传下载器
  puller.py          # 新增：@register_task("netdisk_pull") 轮询→增量转存→下载→删副本
  sync_db.py         # 新增：分享源 / 文件清单表
  scanner.py         # 新增：定时轮询线程（仿 streamer.py 骨架）
  router.py          # 新增：/netdisk/* 接口
```

`baidu_uploader/api/client.py` 需补：`filemetas(fsids, dlink=1)`、`delete(paths)`、递归 `list_all(dir)`
（`method=listall` 支持递归，比自己递归 `list` 省很多请求，需实测普通应用是否可用）。

### 数据模型（sqlite，`state/portal.db` 新表）

```sql
-- 分享源：一个分享链接 ↔ 一个服务器落点
netdisk_shares(
  id PK, owner, share_url, surl, pwd, shareid, uk,
  sub_dir,                    -- 只同步分享内的某个子目录，'' = 根
  local_dir,                  -- 服务器落点
  transfer_root,              -- 我们账号里的中转目录
  enabled, poll_interval,     -- 0 = 仅手动
  include_glob, exclude_glob,
  link_state,                 -- ok / invalid / auth_failed
  last_poll_at, last_status, last_error
)

-- 文件级清单：增量判据 + 断点续传 + 前端进度表 + 幂等 + 清理依据
netdisk_share_files(
  share_id, share_path, fs_id, md5, size, isdir,
  batch_id,                   -- 所属转存批次
  state,                      -- seen/transferring/transferred/downloading/done/failed
  our_path,                   -- 中转区路径（保留，供误删重取）
  local_path, bytes_done, error, updated_at,
  PRIMARY KEY(share_id, fs_id)
)
```

**增量判据：`fs_id`**（分享侧唯一且稳定）。清单里没有的 fs_id 才转存。
`md5`/`size` 由 `share/list` 直接返回，用于下载后校验完整性。

**`batch_id` 是同名不同版本的解药**：中转区不清空，同名文件必然相撞，
`ondup=skip` 会导致下载到旧版本且状态显示成功（见上文架构约束 ①）。
按批次分目录后，`ondup` 取值就不再影响正确性——保守起见仍用 `skip`。

### API（新 router `/netdisk`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/netdisk/shares/preview` | 提交链接+提取码，立刻 verify+list，返回目录树供选子目录 |
| GET/POST/PATCH/DELETE | `/netdisk/shares` | 分享源 CRUD |
| POST | `/netdisk/shares/{id}/sync` | 触发一次同步，返回 `task_id` |
| GET | `/netdisk/shares/{id}/files` | 文件清单与逐个状态 |
| POST | `/netdisk/shares/{id}/files/{fs_id}/redownload` | 从中转区重新下载（服务器文件误删时用） |
| GET | `/netdisk/health` | 平台账号 cookie/容量健康度（运维用） |
| GET | `/netdisk/batches` | **清理助手**：按分享源列出中转批次、占用体积、是否已全部下载完成 |
| DELETE | `/netdisk/batches/{batch_id}` | 删除一个已完成批次的网盘副本（人工清理入口） |

清理接口限 `admin_users` 白名单（沿用 `Settings.is_admin`）。
有了它，"定期人工清理"就是在门户里看一眼水位、勾几个已完成的旧批次点删除，
而不是去百度网盘网页里翻目录猜哪些能删。

### 需要补的 fs 能力

第 ⑦ 步用户要"移动到工作目录"，但现有 `fs/rename` 明确**只允许同目录改名**
（`browser.py:342` 注释："不跨目录"）。需要新增：

- `POST /fs/move`：`{src: [...], dst_dir}`，同 `fs_roots` 内跨目录移动
- 同分区用 `os.rename`（瞬时）；跨分区退化为 copy+unlink，走 TaskManager 异步 + 进度
- 沿用 `normalize_under_roots` + `call_as_user` 的既有安全模型

前端 `FileBrowser.vue` 加"移动到…"（目录选择器复用现有组件）。

### 前端

- 新增 `views/NetdiskSyncView.vue`：分享源列表 + 粘贴链接的新建向导 + 手动同步按钮 + 文件进度表
  （进度订阅 `/ws/tasks`，与现有任务进度完全同构）
- 链接失效/cookie 失效要有**显眼的红色状态**，并给出"重新提交链接"的入口
- `FilesView.vue` 收藏栏给 inbox 目录一个固定入口

### 定时轮询

仿 `NetdiskStreamer` 起 `NetdiskScanner` 常驻线程，按 `poll_interval` 到期的源逐个
`tm.submit("netdisk_pull", ...)`。用与 `try_begin_netdisk` 同样的 CAS 占位思路防止重复派发。

**轮询节奏要克制**：私有接口高频调用有风控风险。建议间隔 ≥ 5 分钟并加随机抖动，
复用同一个 `httpx.Client` 保持 cookie 与连接。

---

## 分期

| 阶段 | 内容 | 交付判据 |
|---|---|---|
| **0** | 五项实测 | 结论写回本文档；第 1 项或第 3 项不通则方案要改 |
| **1** | 提交链接 → verify/list → 目录树预览（只读，不转存） | 门户里能看到分享内容 |
| **2** | 手动同步一次：按批次转存 → 下载 → 校验 | 点按钮能把文件拉到服务器，属主正确 |
| **3** | 增量（fs_id 清单）+ 断点续传 + 逐文件进度 + 失败重试 | 用户加新文件后再点同步，只拉新增的；**同名换版本能拿到新版** |
| **4** | 定时轮询 + `fs/move` + 前端移动到工作目录 | 端到端闭环，用户不碰命令行 |
| **5** | 中转区清理助手（批次列表/体积/一键删）+ 集群侧 inbox 保留期 + cookie 健康告警 | 人工清理有称手工具；`/data` 不被撑爆；cookie 失效有人知道 |

Phase 1–3 是最小可用闭环（第 3 阶段才真正兑现"用户后续新增能识别到"），
建议做到这里先给一个客户试用。

---

## 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| **dlink 限速导致下载比 HTTP 上传还慢** | 方案归零 | Phase 0 必测；不达标只能转对象存储直传 |
| **中转区同名不同版本被 `ondup` 跳过** | **静默下载到旧版本，状态还显示成功** | 按 `batch_id` 分目录隔离（架构约束 ①）。⚠️ 原计划的第二道闸「md5 校验」已被实测否决（Q5：百度返回混淆串），**批次隔离现在是唯一防线** |
| 平台账号容量增长 | 长期堆积 | 已决策：容量充足，定期人工清理。提供批次清理助手 + 水位展示，别让人工清理靠猜 |
| **BDUSS/STOKEN 过期或被风控** | 全体同步中断 | 状态置 `auth_failed` + 前端红标 + 运维告警（`netdisk/notify` 有骨架）；绝不静默吞异常；轮询加抖动、间隔 ≥5 分钟 |
| **网页私有接口漂移** | 同步中断 | 每步都记录 errno 与原始返回（POC 已是这个风格）；接口封装集中在 `share_client.py` 一处，改起来只动一个文件 |
| 转存配额触顶（errno -33） | 大批量投递失败 | 分批 ≤100；触顶后退避重试，沿用 `retry_failed_uploads` 的退避+封顶模式 |
| 用户取消分享/改提取码/删重建文件夹 | 该源静默失效 | `link_state` 置 `invalid`，前端提示用户重新提交链接 |
| 用户投递到一半就触发轮询，拉到半截文件 | 数据损坏 | 下载后按 `share/list` 给的 size/md5 校验；`.part` 临时文件 + 校验通过才 rename |
| 客户数据经平台账号中转 | 合规/隔离 | 中转区按用户隔离目录；下载后立即删除；平台账号不做他用；在服务协议里写明 |
| 文件名含路径穿越/非法字符 | 越权写入 | 复用 `_safe_name` + `normalize_under_roots` 双重校验 |

## 备选通道（记录备查，暂不实现）

- **用户 OAuth 授权 + 应用目录投递**：用户授权后平台拿到**其本人**的 token，读他自己网盘的
  `我的应用数据/HPC/`。全官方接口、无 cookie、数据不进平台账号、无容量问题。
  代价是用户要走一次授权、投递路径固定。**如果分享链接这条路的运维负担超出预期，这是首选退路。**
- **对象存储直传**（OSS/S3 预签名分片）：最稳、速度最可控，但要花钱且客户要改工具习惯。
  如果 Phase 0 测出 dlink 吞吐不达标，这是唯一的出路。

## 一个反问

值得先确认：**"上传慢"的瓶颈到底是带宽，还是门户当前的单流 HTTP 上传实现？**
如果是后者（没有分片并发/断点续传），直接改造上传通道成本低得多，也不引入任何第三方依赖和运维负担。
建议先拿一个真实客户的样本量一下实际速率再定。
