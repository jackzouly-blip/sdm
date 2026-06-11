# HPC Agent 部署（方案 A：与生产并行，端口 8001）

把 hpc-portal 作为 **3dix-portal 的 HPC agent** 接入。本文部署一个**全新的并行实例**
`hpc-agent`，与 71.100 上正在跑的生产 `hpc-portal`（:8000）**完全隔离、互不影响**。

> 为什么不直接复用正在跑的那个？因为它是旧代码，没有 `HPC_AGENT_API_TOKEN` 支持，
> 而 3dix 只能走 agent token 路径（拿不到各用户的 PAM 密码，无法走旧的按用户 JWT）。
> 并行实例跑在同一头节点，看到的是**同一套 Torque/作业/文件**——资源天然共享，不复制数据。

## 隔离要点(与生产的区别)

| 项 | 生产 hpc-portal | 新 hpc-agent |
|---|---|---|
| 代码目录 | `/opt/hpc-portal` | `/opt/hpc-portal-agent`（新代码） |
| 端口 | 8000 | **8001** |
| systemd 单元 | `hpc-portal` | `hpc-agent` |
| 环境文件 | `…/hpc-portal.env` | `…/deploy/hpc-agent.env` |
| 状态/缓存 | `/var/lib/hpc-portal` | `/var/lib/hpc-portal-agent` |
| Agent token | 无 | **有**（`HPC_AGENT_API_TOKEN`） |

两实例各跑各的 qstat 轮询，独立 `state.db`，互不写对方文件。

---

## 1. 拉新代码到独立目录

```bash
# 新代码 = 含 Phase 0 agent token 改动的 hpc-portal 仓库
mkdir -p /opt/hpc-portal-agent
git clone <hpc-portal 仓库地址> /opt/hpc-portal-agent
# 或：把本仓库当前分支的内容拷到 /opt/hpc-portal-agent
# 确认 /opt/hpc-portal-agent/backend/app/auth/session.py 里有 _resolve_principal / HPC_AGENT_API_TOKEN
```

依赖与生产相同。复用现有 conda base 即可（无需重装），如需隔离可单建 conda env。

```bash
/opt/miniconda3/bin/python -m pip install -r /opt/hpc-portal-agent/backend/requirements.txt
```

## 2. 建独立数据目录

```bash
mkdir -p /var/lib/hpc-portal-agent/{scratch,d3plot,office,state}
```

## 3. 配置环境文件

```bash
cp /opt/hpc-portal-agent/deploy/hpc-agent.env.example \
   /opt/hpc-portal-agent/deploy/hpc-agent.env

# 生成并填入 agent token（记下它，3dix 侧要填同一个）
openssl rand -hex 32        # → 填到 HPC_AGENT_API_TOKEN
openssl rand -hex 32        # → 填到 HPC_JWT_SECRET

vim /opt/hpc-portal-agent/deploy/hpc-agent.env
# 重点核对：HPC_AGENT_API_TOKEN、HPC_FS_ROOTS、HPC_ADMIN_USERS、HPC_DB_PATH(=…-agent/…)
```

## 4. 本地起一下验证(可选)

```bash
cd /opt/hpc-portal-agent/backend
set -a; source /opt/hpc-portal-agent/deploy/hpc-agent.env; set +a
/opt/miniconda3/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

另开终端，验证两条路径：

```bash
# health：应 running_as_root: true（注意 8001，别动 8000）
curl -s http://127.0.0.1:8001/health

# agent token + act-as：应返回 user07 的作业（而非 401）
curl -s http://127.0.0.1:8001/jobs \
  -H "Authorization: Bearer <你的 HPC_AGENT_API_TOKEN>" \
  -H "X-Act-As-User: user07"

# 反例：不带 X-Act-As-User，应 401（缺 X-Act-As-User 头）
curl -s http://127.0.0.1:8001/jobs -H "Authorization: Bearer <token>"
```

确认无误后 Ctrl-C，转 systemd。

## 5. systemd 托管

```bash
cp /opt/hpc-portal-agent/deploy/hpc-agent.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hpc-agent
systemctl status hpc-agent
journalctl -u hpc-agent -f
```

> 生产 `hpc-portal` 不受影响，无需重启。

---

## 6. 3dix-portal 侧配置

1. 以管理员登录 3dix → 左侧「HPC 节点管理」→「添加节点」。
2. 填写：
   - **名称**：如 `head01`
   - **端点 URL**：`http://127.0.0.1:8001`（3dix 与 agent 同机时）
     - 若 3dix 不在 71.100 上，需让 agent 可达：要么 3dix 经内网访问 `http://71.100:8001`
       并把 agent 监听改为内网网卡（注意仅限可信内网），要么用 SSH 隧道/ nginx 内部转发。
   - **Agent API Token**：与 `HPC_AGENT_API_TOKEN` 完全一致。
   - **允许使用的用户组**：选定可使用该节点的用户组（RBAC）。
3. 保存后点「测试连接」，状态应变「正常」并显示版本。
4. 普通用户登录 3dix → 左侧「HPC 作业」即可看到自己的作业；
   机时统计需该用户名在 agent 的 `HPC_ADMIN_USERS` 列表内。

> 用户名映射：3dix 以登录用户名作为 `X-Act-As-User` 传给 agent，agent 据此 setuid。
> 因此 3dix 用户名需等于头节点上的 POSIX 用户名（经 AD/LDAP 同步通常已一致）。

---

## 7. 切换 / 回滚(后续)

- 验收期：两实例并行，用户照常用旧前端，3dix 走新 agent，互不影响。
- 正式切换：3dix 验收通过后，把入口（nginx/域名）从旧 hpc-portal 切到 3dix；
  旧实例保留一段时间以便回滚，确认稳定后再下线。
- 回滚：3dix 出问题不影响旧 hpc-portal（它一直在跑）；停用 3dix 的 HPC 入口即可。

## 注意事项

- 在生产头节点装服务/改端口/起新进程，按运维铁律需**单独批准**再操作。
- agent 仅 root 且仅监听 127.0.0.1；token 与用户名映射是信任边界，妥善保管 token。
- 强交互页（在线终端 / 任务进度 WebSocket，Phase 3）需要 nginx 把 `/hpc/ws/` 反代到
  agent 的 `/ws/`；只读功能（Phase 2）由 3dix 服务端直连 agent，无需额外 nginx。
