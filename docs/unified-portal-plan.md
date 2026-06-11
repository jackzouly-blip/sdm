# 统一门户方案：把 hpc-portal 并入 3dix-portal（作为 HPC agent）

## 结论
3dix-portal 本身就是 **「Django 门户 + 若干独立 agent」** 架构（AD 管理、Citrix 入口、监控、文件外发各由 agent 实现，
门户自身不绑 AD）。因此把 **hpc-portal 作为又一个 agent（HPC agent）** 接入，是最自然、最低风险的合并方式：
**不重写 hpc-portal**，只给它加 agent 鉴权，再在 3dix 里加一个 `hpc` Django app。

## 复用 3dix 现成的 agent 接入模式
- **Agent**：独立 FastAPI 服务，部署在有能力的机器上，**Bearer Token** 鉴权，暴露 `/health` + 业务接口。
  - 范例：`ad_agent/main.py`（FastAPI + `verify_token` Bearer）。
- **Django 侧**：
  - `Node` 模型存 agent 的 `endpoint_url` + `api_token` + `status` + `last_seen_at` + `allowed_manager_groups`（RBAC）。
    - 范例：`ad_management/models.py::ADAgentNode`。
  - client 类用 `Authorization: Bearer {node.api_token}` 调 agent。
    - 范例：`ad_management/services.py`、`storage/agent_client.py`。
  - 视图 + `templates/<app>/` 做 UI；`templates/base.html` 加导航。
- **登录**：Django accounts（`accounts/backends.py` + 可选 LDAP）。门户负责认证；agent 信任门户传来的用户名做 setuid。

## hpc-portal 与该模式的契合
hpc-portal 后端**本身就是 FastAPI**，几乎原样就是一个 agent：
- 已有 `/jobs`、`/fs/*`、`/d3plot/*`、`/stats/*`、`/extract/*`、`/ws/shell`(PTY)、`/ws/tasks` 等接口；
- 已有 `actas.py` 在头节点以 root setuid 执行特权操作。
- 只差：**加一个 agent 级 Bearer Token**（供 Django 内部调用），与现有按用户 JWT 并存。

## 目标架构
```
nginx（唯一入口）
  /            → Django 3dix（外发/AD/监控/cloudcad + 新增 hpc 页面，模板）
  /hpc/app/*   → Vue SPA（d3plot/终端/文件浏览，强交互页，由 Django 提供静态）
  /hpc/api/*   → Django hpc app → (Bearer) → HPC agent
  /hpc/ws/*    → 反代到 HPC agent 的 /ws/*（PTY 终端 / 任务进度）

Django 3dix（普通用户）
  └─ hpc app：HpcAgentNode 模型 + agent client + 视图/模板 + RBAC

HPC agent = 现有 hpc-portal FastAPI（root, 仅听 127.0.0.1）
  └─ 加 agent Bearer Token；其余不变
```

## 用户与权限
- 用户登录 3dix（Django accounts）。Django 用户需关联一个 **POSIX/AD 用户名**（= sAMAccountName）用于 setuid。
- Django 认证通过后，带 agent token + 目标用户名调 HPC agent；agent 据用户名 setuid 执行。
- HPC 菜单/操作用 `allowed_manager_groups` 式 RBAC 控制（沿用 3dix 模式）。
- HPC 操作可纳入 3dix 的 `audit` 审计。

## 分期计划
- **Phase 0 — 本地 spike（不碰生产）**
  - 本地把 3dix 跑起来（SQLite、本地超管、不接 AD），摸清 accounts/RBAC/base.html 导航与 admin。
  - 给 hpc-portal agent 加一个 Bearer `AGENT_API_TOKEN`（与按用户 JWT 并存）。
- **Phase 1 — `hpc` Django app 骨架**
  - 新建 `hpc` app：`HpcAgentNode`（仿 `ADAgentNode`）+ `services.py`（仿 `ad_management/services.py`）+ `/health` 轮询。
  - base.html 加 “HPC” 菜单（RBAC 门控）；后台可配置 agent 节点。
- **Phase 2 — 只读功能接入**
  - 作业列表/详情、机时统计：Django 视图/模板 → agent client。
- **Phase 3 — 强交互页（复用 Vue SPA）**
  - 把现有 Vue SPA 以 `/hpc/app/` 形式由 Django 提供；nginx 反代 `/hpc/ws/`、`/hpc/api/` 到 agent。
  - 文件浏览（含上传目录、暂停续传下载）、d3plot 预览、在线终端。
- **Phase 4 — 提交/写操作 + 审计**
  - 作业提交/终止、文件清理、提取规则；接 3dix 审计与 RBAC。
- **Phase 5 — 统一与切换**
  - 统一皮肤/导航；hpc-portal 对外端口下线（仅留内部 agent）；用户名映射与灰度切换。

## 上线 / 切换路径（并行运行 + 统一切换，蓝绿）
- **不动现有 hpc-portal**：线上 hpc-portal（:8088）保持运行，用户照常用，开发期不修改它。
- **服务器另起内部服务**：把集成了 HPC 的 3dix-portal 部署为**新的内部服务**（新端口，先仅内网/测试路径，不对外）；
  其 HPC agent 用**并行的 hpc-portal 实例**（独立端口/独立 state），避免触碰线上那个。
- **功能完善后统一切换**：3dix-portal 功能验收通过 → 把入口（nginx/域名）从旧 hpc-portal 切到 3dix-portal → 下线旧服务。
- 切换前两套并行；切换后保留旧服务一段时间以便回滚。

## 风险与注意
- **生产头节点**：装 PostgreSQL/Redis、调端口、起新服务，均**单独批准**；遵守铁律（绝不 `nmcli con up/reapply`）。
- DB：先 SQLite 跑通；Redis/RQ 是否引入头节点单独评估（可先用更轻的队列）。
- agent 仅 root 且只听 127.0.0.1；Django 不以 root 跑。
- agent token 与用户名映射是信任边界，需妥善保管 token、校验用户名白名单。

## 仓库策略
统一仓库以 **3dix-portal** 为主干，hpc-portal 作为其中的 agent（可作为子目录或保留独立 agent 仓库 + 在 3dix 加 `hpc` app）。
Phase 1 时确定（建议：3dix 仓库内加 `hpc` app，hpc agent 暂留独立仓库 `chiyueshi-hpc-portal`，后续按需并入）。
```
