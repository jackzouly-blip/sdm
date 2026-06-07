# HPC 门户部署手册（CentOS 7，含 d3plot/Office/百度上传）

面向集群 head/login 节点，**后端以 root 运行**（act-as-user 降权 + PAM 登录都需 root）。
约定路径：仓库 `/opt/hpc-portal`，Miniconda `/opt/miniconda3`，conda 环境 `hpc-portal`，数据目录 `/var/lib/hpc-portal`。按实际调整。

## 0. 前提
- CentOS 7，root 权限；head 节点可用 `qstat`/`qdel`、能访问共享文件系统。
- 能联网或有内网 PyPI/npm 镜像。
- 一个用于测试的**真实系统账号**（PAM 登录用）。

## 1. 取代码
```bash
mkdir -p /opt && git clone <repo> /opt/hpc-portal   # 或上传代码到 /opt/hpc-portal
cd /opt/hpc-portal
```

## 2. Python 环境（Miniconda + Python 3.10，后端与 lasso 同环境）
```bash
# 装 Miniconda（若没有）
curl -fsSLO https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p /opt/miniconda3
# 一键装依赖 + 建目录 + 构建前端
bash deploy/install.sh
```
> CentOS 7 自带 Python 3.6 太旧（pydantic v2 / lasso 需 3.8+），故用 conda 3.10。
> 离线环境：在能联网的同架构机器 `pip download` 出 wheel、`pnpm build` 出 dist，拷过来离线安装。

## 3. 系统组件（按需）
```bash
# Office 在线预览（推荐装中文字体）
yum install -y libreoffice libreoffice-langpack-zh-Hans wqy-zenhei-fonts
which soffice    # 确认路径，填到 env 的 HPC_OFFICE_SOFFICE

# nginx
yum install -y nginx
```
> d3plot 三维查看**无需** LS-DYNA/LS-PrePost —— 用 lasso-python 直接解析（已随 conda 环境装好）。

## 4. 配置
```bash
cp deploy/hpc-portal.env.example deploy/hpc-portal.env   # install.sh 已自动生成
vi deploy/hpc-portal.env
```
**务必填好**：
- `HPC_JWT_SECRET`：`openssl rand -hex 32` 生成。
- `HPC_FS_ROOTS`：任务工作目录/数据实际根（如 `/home:/data:/scratch`）。
- `HPC_ADMIN_USERS`：可用在线终端+管规则的人（勿留空）。
- `HPC_ACCOUNTING_DIR`：Torque accounting 日志路径（完成任务历史）。
- `HPC_OFFICE_SOFFICE`：soffice 路径。
- `HPC_D3PLOT_PYTHON`：留空（用后端 conda 解释器，已含 lasso）。

百度上传（可选）：编辑 `backend/baidu_uploader_config.yaml`，并在该 conda 环境下完成一次 OAuth 授权拿到 `baidu.token.json`（参考 `backend/baidu_uploader/cli.py`）。

## 5. 启动后端（systemd）
```bash
cp deploy/hpc-portal.service /etc/systemd/system/
# 按实际核对 service 里的 conda python 路径与 WorkingDirectory
systemctl daemon-reload
systemctl enable --now hpc-portal
systemctl status hpc-portal
journalctl -u hpc-portal -f      # 看日志；应有 "以 root 运行，act-as-user 已就绪"
curl -s 127.0.0.1:8000/health
```

## 6. 前端 + nginx
```bash
# 若 install.sh 已构建，frontend/dist 就绪；否则在有 pnpm 的机器构建后拷过来
cp deploy/nginx-hpc-portal.conf /etc/nginx/conf.d/hpc-portal.conf
vi /etc/nginx/conf.d/hpc-portal.conf      # 改 server_name / root / (启用 HTTPS)
nginx -t && systemctl enable --now nginx
```
> 强烈建议配 HTTPS（证书 + 443）。

## 7. 部署后冒烟
- 浏览器打开门户 → 用**真实系统账号**登录（PAM）。
- 任务列表能看到 `qstat` 的真实任务；点进详情看工作目录。
- 文件浏览：进入、预览（文本/图片/PDF/Office）、上传、新建目录、打包下载。
- 打包上传到百度（若配）→ 任务记录出现分享链接。
- d3plot：找一个有 d3plot 的任务目录 → 「3D 查看结果」→ 动画/云图/剖切/测距正常。
- 在线终端（管理员账号）。

## 关键注意
- **后端必须 root**：非 root 时 act-as-user 不可用、文件操作不隔离。
- **JWT 密钥**：改掉默认值，否则任意人可伪造登录态。
- **磁盘**：`/var/lib/hpc-portal` 下 scratch/d3plot/office 缓存会增长，注意清理或挂大盘。
- **首次 d3plot/Office** 较慢（解析/转换），之后命中缓存秒开。
