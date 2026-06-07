#!/usr/bin/env bash
# HPC 门户安装脚本（CentOS 7+，需联网或内网镜像）。
# 用法：在仓库根目录执行  bash deploy/install.sh
# 幂等：重复执行会复用已存在的 conda 环境。systemd/nginx/LibreOffice/百度凭据见 docs/DEPLOY.md（需手动）。
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
CONDA_DIR="${CONDA_DIR:-/opt/miniconda3}"
ENV_NAME="${ENV_NAME:-hpc-portal}"
PYVER="${PYVER:-3.10}"
DATA_DIR="${DATA_DIR:-/var/lib/hpc-portal}"

echo "==> 仓库目录: $APP_DIR"

# 1) Miniconda（缺失则提示安装）
if [ ! -x "$CONDA_DIR/bin/conda" ]; then
  echo "未发现 $CONDA_DIR/bin/conda。请先安装 Miniconda，例如："
  echo "  curl -fsSLO https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh"
  echo "  bash Miniconda3-latest-Linux-x86_64.sh -b -p $CONDA_DIR"
  exit 1
fi
source "$CONDA_DIR/etc/profile.d/conda.sh"

# 2) 创建/复用 conda 环境（后端 + lasso 同一环境）
if ! conda env list | grep -q "/$ENV_NAME$"; then
  echo "==> 创建 conda 环境 $ENV_NAME (python=$PYVER)"
  conda create -y -n "$ENV_NAME" "python=$PYVER"
fi
conda activate "$ENV_NAME"

# 3) 后端依赖（含 lasso-python）
echo "==> 安装后端依赖"
pip install --upgrade pip
pip install -r "$APP_DIR/backend/requirements.txt"

# 4) 前端构建（有 node/pnpm 则就地构建；否则提示在别处构建后拷 dist）
if command -v pnpm >/dev/null 2>&1; then
  echo "==> 构建前端"
  (cd "$APP_DIR/frontend" && pnpm install && pnpm build)
else
  echo "!! 未发现 pnpm，跳过前端构建。请在有 Node18+/pnpm 的机器上 'pnpm install && pnpm build'，把 frontend/dist 拷到本机。"
fi

# 5) 运行所需目录
echo "==> 创建数据/缓存目录: $DATA_DIR"
mkdir -p "$DATA_DIR"/{scratch,d3plot,office,state}

# 6) 环境变量文件
if [ ! -f "$APP_DIR/deploy/hpc-portal.env" ]; then
  cp "$APP_DIR/deploy/hpc-portal.env.example" "$APP_DIR/deploy/hpc-portal.env"
  echo "==> 已生成 deploy/hpc-portal.env，请编辑填写（尤其 HPC_JWT_SECRET、HPC_FS_ROOTS、HPC_ADMIN_USERS）"
fi

echo
echo "==> 依赖与构建完成。接下来（见 docs/DEPLOY.md）："
echo "   1) 编辑 deploy/hpc-portal.env"
echo "   2) (可选) yum install -y libreoffice  # Office 预览"
echo "   3) (可选) 配置 baidu_uploader_config.yaml 并完成一次 OAuth 授权  # 网盘上传"
echo "   4) 安装 systemd 服务与 nginx 配置，启动"
echo "   conda 环境 python: $CONDA_DIR/envs/$ENV_NAME/bin/python"
