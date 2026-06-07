# HPC 任务管理门户 —— 部署教程

本教程覆盖在 **Torque 集群的 head/login 节点**上以 **root** 部署后端服务，
重点讲解 **百度网盘授权**（最容易卡住的一步）。

> 前提：服务以 root 运行，才能用 PAM 校验系统账号、并在文件操作时降权到目标用户。

---

## 目录

1. [环境准备](#1-环境准备)
2. [获取代码与安装依赖](#2-获取代码与安装依赖)
3. [★ 百度网盘授权（重点）](#3--百度网盘授权重点)
4. [应用配置](#4-应用配置)
5. [本地启动验证](#5-本地启动验证)
6. [构建前端](#6-构建前端)
7. [systemd 守护进程](#7-systemd-守护进程)
8. [nginx 反向代理 + HTTPS](#8-nginx-反向代理--https)
9. [部署后验证](#9-部署后验证)
10. [常见问题（含已踩过的坑）](#10-常见问题含已踩过的坑)

---

## 1. 环境准备

在 head node 上确认：

```bash
# Python 3.9+（集群自带或用 conda/pyenv）
python3 --version

# Torque 客户端命令可用
which qstat qdel

# PAM 库存在（Linux 默认都有）
ls /lib64/libpam.so* /lib/x86_64-linux-gnu/libpam.so* 2>/dev/null

# 确认 accounting 日志可读（历史任务回填用，root 可读）
ls -l /var/spool/torque/server_priv/accounting/ | tail
```

需要放行的网络出站：`*.baidu.com`、`d.pcs.baidu.com`（上传分片走这个域名）。

---

## 2. 获取代码与安装依赖

```bash
# 假设部署到 /opt/hpc-portal
mkdir -p /opt/hpc-portal
# 将项目拷贝/克隆到此处，使 /opt/hpc-portal/backend 存在

cd /opt/hpc-portal/backend
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

---

## 3. ★ 百度网盘授权（重点）

门户的"打包上传"复用 `baidu_uploader`，它通过百度开放平台 OpenAPI 上传，
**必须先完成一次 OAuth2 授权**拿到 `access_token` + `refresh_token`。
之后 token 会自动续期，无需重复授权。

### 3.1 在开放平台创建应用

1. 打开 [百度网盘开放平台](https://pan.baidu.com/union)，注册成为开发者。
2. 创建应用，记下 **AppKey** 和 **SecretKey**。
3. 进入应用的 **权限管理**，确认已开通 **网盘（netdisk）** 权限。
4. 进入 **安全设置 → 授权回调页**，确认允许使用 **`oob`**（带外授权，
   即在网页上显示授权码让用户手动复制）。本教程用 oob 模式，因为服务跑在
   无浏览器的服务器上。

> ⚠️ 这两件事不做，第 3.3 步授权一定失败：① netdisk 权限未开通；② 回调页不允许 oob。

### 3.2 填写凭据到配置文件

复制示例配置并填入你的密钥：

```bash
cd /opt/hpc-portal/backend
cp baidu_uploader_config.example.yaml baidu_uploader_config.yaml
vim baidu_uploader_config.yaml
```

关键字段：

```yaml
app:
  app_id:     "你的AppID"
  app_key:    "你的AppKey"
  secret_key: "你的SecretKey"
  sign_key:   "你的SignKey"   # 可留空

auth:
  mode: "oob"                 # 服务器无浏览器，用 oob 手动粘码
  redirect_uri: "oob"
  scope: "basic,netdisk"
  token_file: "baidu.token.json"

upload:
  remote_dir: "/apps/HPC"     # 普通权限只能写 /apps/<你的应用名>/ 目录
  chunk_size: 4194304         # 4MB；超级会员可调大
  concurrency: 3
```

> `baidu_uploader_config.yaml` 与 `baidu.token.json` 都含敏感信息，已被
> `.gitignore` 忽略，**切勿提交到版本库**。

### 3.3 执行授权（headless 服务器的关键流程）

由于服务器没有浏览器，授权分三步走：

```bash
cd /opt/hpc-portal/backend
.venv/bin/python -m baidu_uploader.cli -c baidu_uploader_config.yaml auth
```

终端会打印一条授权 URL。**把这条 URL 复制到你本地电脑的浏览器**打开：

1. 登录你的百度账号 → 点击「同意授权」。
2. 页面会显示一串**授权码（code）**。
3. 回到服务器终端，把授权码粘贴进去回车。

成功后会看到账号信息和网盘容量，例如：

```
✓ 已获得 access_token
账号: your_name (uk=..., vip=2)
容量: 已用 1837.10 GB / 共 16399.00 GB
```

此时 token 已保存到 `backend/baidu.token.json`（权限自动设为 600）。

### 3.4 验证与自动续期

```bash
# 随时验证 token 是否有效
.venv/bin/python -m baidu_uploader.cli -c baidu_uploader_config.yaml whoami
```

- `access_token` 有效期约 30 天，程序会在过期前用 `refresh_token` **自动刷新**，
  无需人工干预。
- 只要 `baidu.token.json` 不被删除，授权就持续有效。
- 万一 `refresh_token` 也失效（长期未使用等），重新执行 3.3 即可。

> 💡 备份建议：把 `baidu.token.json` 纳入你的运维备份，避免误删后要重新授权。

---

## 4. 应用配置

后端用环境变量配置（前缀 `HPC_`）。创建 `/opt/hpc-portal/backend/.env`：

```ini
# 会话密钥：务必改成随机长字符串（≥32 字节），否则 JWT 不安全！
# 生成：openssl rand -hex 32
HPC_JWT_SECRET=在这里粘贴_openssl_rand_hex_32_的输出

# 服务监听
HPC_HOST=127.0.0.1
HPC_PORT=8000

# Torque 轮询间隔（秒）
HPC_PBS_POLL_INTERVAL=15
HPC_ACCOUNTING_DIR=/var/spool/torque/server_priv/accounting

# 文件浏览允许的根（冒号分隔，类似 PATH）
HPC_FS_ROOTS=/data

# 打包临时目录（需足够空间存放 tar）
HPC_SCRATCH_DIR=/data/.hpc-portal-scratch
```

> `HPC_SCRATCH_DIR` 建议放在与 `/data` 同一文件系统的大容量分区，避免打大包时
> 撑爆 `/tmp`。

---

## 5. 本地启动验证

```bash
cd /opt/hpc-portal/backend
set -a; source .env; set +a
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

另开终端：

```bash
# 健康检查，应显示 running_as_root: true
curl -s http://127.0.0.1:8000/health

# 用某个系统账号登录拿 token
curl -s -X POST http://127.0.0.1:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"user07","password":"该用户的系统密码"}'
```

确认无误后 Ctrl-C，进入 systemd 托管。

---

## 6. 构建前端

前端是 Vue3 + Vite，构建产物为纯静态文件，由 nginx 直接托管，API 同源转发给后端。

```bash
cd ~/hpc-portal/frontend

# Node 18+（推荐 20/22），用 nvm 或系统包均可
node -v

# 安装依赖（pnpm 或 npm 均可）
npm install        # 或 pnpm install

# 生产构建，产物输出到 frontend/dist/
npm run build      # 或 pnpm build
```

构建完成后 `frontend/dist/` 即为可部署的静态站点，nginx 的 `root` 指向它（见第 8 节）。

> 本地联调（可选）：`npm run dev` 启动 Vite（默认 5173），已在 `vite.config.ts`
> 中把 `/auth /jobs /fs /package /tasks /health /ws` 代理到 `http://127.0.0.1:8000`，
> 无需额外配置即可前后端联调。

---

## 7. systemd 守护进程

创建 `/etc/systemd/system/hpc-portal.service`：

```ini
[Unit]
Description=HPC Portal Backend
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/hpc-portal/backend
EnvironmentFile=/opt/hpc-portal/backend/.env
ExecStart=/opt/hpc-portal/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启用：

```bash
systemctl daemon-reload
systemctl enable --now hpc-portal
systemctl status hpc-portal
journalctl -u hpc-portal -f      # 看日志
```

> 服务以 root 运行是设计要求：PAM 认证与 act-as-user 降权都依赖 root。
> uvicorn 只监听 127.0.0.1，对外由 nginx 经 HTTPS 暴露。

---

## 8. nginx 反向代理 + HTTPS

nginx 负责两件事：托管前端静态产物（`frontend/dist/`），并把 API 路径同源转发给后端。

创建 `/etc/nginx/conf.d/hpc-portal.conf`：

```nginx
server {
    listen 443 ssl;
    server_name hpc.example.com;          # 改成你的域名

    ssl_certificate     /etc/nginx/ssl/hpc-portal.crt;
    ssl_certificate_key /etc/nginx/ssl/hpc-portal.key;

    # 上传大文件：放开请求体大小限制
    client_max_body_size 0;

    # 前端静态站点（Vite 构建产物）
    root /home/部署用户/hpc-portal/frontend/dist;   # 改成实际路径
    index index.html;

    # SPA 路由回退：非 API 路径一律交给 index.html
    location / {
        try_files $uri $uri/ /index.html;
    }

    # API 转发给后端：前端统一用 /api 前缀，nginx 去掉 /api 再转发
    # （后端路由本身不带 /api，故 proxy_pass 末尾带 / 以剥掉前缀）。
    # 用 /api 前缀是为了避免 /jobs、/tasks 等与前端 SPA 路由同名冲突。
    location /api/ {
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;          # 打包/上传是长任务
    }

    # WebSocket（任务进度推送）
    location /ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
    }
}

# HTTP 跳转 HTTPS
server {
    listen 80;
    server_name hpc.example.com;
    return 301 https://$host$request_uri;
}
```

```bash
nginx -t && systemctl reload nginx
```

> 没有正式证书可先用自签：
> `openssl req -x509 -newkey rsa:2048 -nodes -keyout hpc-portal.key -out hpc-portal.crt -days 365`

---

## 9. 部署后验证

```bash
# 1. 健康检查
curl -sk https://hpc.example.com/health

# 2. 登录
TOKEN=$(curl -sk -X POST https://hpc.example.com/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"user07","password":"***"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

# 3. 看任务列表（应只返回该用户的任务）
curl -sk https://hpc.example.com/jobs -H "Authorization: Bearer $TOKEN"

# 4. 浏览目录
curl -sk "https://hpc.example.com/fs/list?path=/data" -H "Authorization: Bearer $TOKEN"

# 5. 百度授权链路验证
cd /opt/hpc-portal/backend
.venv/bin/python -m baidu_uploader.cli -c baidu_uploader_config.yaml whoami
```

---

## 10. 常见问题（含已踩过的坑）

| 现象 | 原因 / 解决 |
|---|---|
| 授权 URL 打开后报错或拿不到 code | ① 应用未开通 **netdisk** 权限；② 回调页未允许 **oob**。回开放平台改。 |
| 调用网盘接口报 `errno 2 / unsupported api` | xpan 接口必须带 `User-Agent: pan.baidu.com`。本项目已内置，若自行扩展接口注意带上。 |
| 分享报 `errno 115 该文件禁止分享` | 百度强制要求提取码。本项目分享时若不指定提取码会**自动生成 4 位码**，正常现象。 |
| 上传报 `errno 31064 / 文件路径无权限` | `upload.remote_dir` 必须以 `/apps/<你的应用名>/` 开头，普通权限只能写应用目录。 |
| 启动日志报 JWT 密钥过短告警 | 未设置 `HPC_JWT_SECRET`。用 `openssl rand -hex 32` 生成并写入 `.env`。 |
| `/health` 显示 `running_as_root: false` | 服务没以 root 跑，PAM 登录与降权都会失败。检查 systemd `User=root`。 |
| 登录总是失败 | 确认输入的是**系统账号密码**；确认服务以 root 运行（PAM 校验需要）。 |
| 任务列表为空但集群在跑任务 | ① qstat 默认只显示 Q/R/E；完成任务靠轮询消失后标记历史，需等一个轮询周期。② 确认 head node 上 `qstat -f` 能列出任务。 |
| 历史任务（很久前完成的）看不到 | 当前版本历史靠"轮询期间见过"的任务留存；服务启动前就完成的任务需 accounting 日志回填（后续版本）。 |
| 打包大目录失败/磁盘满 | 调整 `HPC_SCRATCH_DIR` 到大容量分区；tar 需要先落盘再上传。 |
| token 失效需要重新授权 | 重新执行第 3.3 步即可。建议备份 `baidu.token.json`。 |

---

## 附：升级与维护

```bash
# 更新代码后
cd /opt/hpc-portal/backend
.venv/bin/pip install -r requirements.txt    # 如依赖有变
systemctl restart hpc-portal

# 备份（含授权 token 与数据库）
tar -czf hpc-portal-backup-$(date +%F).tar.gz \
    baidu.token.json baidu_uploader_config.yaml .env state/
```
