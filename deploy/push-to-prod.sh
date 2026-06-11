#!/usr/bin/env bash
# 一键部署到生产 71.100（rsync 免密）。在仓库根目录执行：bash deploy/push-to-prod.sh
# 仅更新生产 hpc-portal(:8000) 的后端代码 + 前端 dist，再重启。
#
# 已按生产现状校准：
#   - 代码目录 /opt/hpc-portal/backend，服务 hpc-portal，uvicorn 固定 --port 8000
#   - 环境文件 /opt/hpc-portal/deploy/hpc-portal.env（HPC_NETDISK_AUTO_USERS=user07 已配）
#   - 数据库 backend/state/portal.db（已含 netdisk_* 列，迁移幂等）—— 刻意不碰 state/
#   - 8001 的 hpc-agent 是独立实例(/opt/hpc-portal-agent)，本脚本完全不触碰
# 刻意不碰：state/(数据库)、deploy/*.env、baidu.token.json、baidu_uploader_config.yaml
set -euo pipefail

HOST="${HOST:-root@192.168.71.100}"
REMOTE="${REMOTE:-/opt/hpc-portal}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> 目标：$HOST:$REMOTE （仅生产 hpc-portal:8000，agent:8001 不动）"

# 0) 服务器侧快照备份：当前 app/ 与数据库，便于秒级回滚
echo "==> 备份服务器现有 app/ 与数据库"
ssh "$HOST" 'set -e; TS=$(date +%Y%m%d-%H%M%S); B=/opt/hpc-portal/.deploy-bak/$TS; mkdir -p "$B";
  cp -a /opt/hpc-portal/backend/app "$B/app";
  cp -a /opt/hpc-portal/backend/state/portal.db "$B/portal.db" 2>/dev/null || true;
  echo "   备份到 $B"'

# 1) 后端：同步整个 app/（含新增 streamer.py、更新版 autoshare.py、新 main.py）
#    + 改动的 baidu client。app/ 子树用 --delete 保持与仓库一致；排除运行期产物。
echo "==> 同步后端代码"
rsync -az --delete \
  --exclude='__pycache__/' --exclude='*.pyc' \
  "$ROOT/backend/app/" "$HOST:$REMOTE/backend/app/"

rsync -az "$ROOT/backend/baidu_uploader/api/client.py" \
  "$HOST:$REMOTE/backend/baidu_uploader/api/client.py"

# 2) 清理旧字节码，避免陈旧 .pyc（如缺失模块的旧缓存）干扰
ssh "$HOST" 'find /opt/hpc-portal/backend/app -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true'

# 3) 前端：本地已 build，直接推 dist
echo "==> 同步前端 dist（已在本地构建）"
rsync -az --delete "$ROOT/frontend/dist/" "$HOST:$REMOTE/frontend/dist/"

# 3.5) 重启前导入自检：新代码 import 失败就中止，绝不让服务重启即崩
echo "==> 导入自检（失败则中止、不重启）"
if ! ssh "$HOST" 'cd /opt/hpc-portal/backend && /opt/miniconda3/bin/python -c "import app.main" 2>&1'; then
  echo "!! 导入失败：已中止，未重启。运行中的旧服务不受影响。请修复或回滚 .deploy-bak。"
  exit 1
fi
echo "   导入 OK"

# 4) 重启生产后端并核对状态（仅 hpc-portal，不动 hpc-agent）
echo "==> 重启 hpc-portal"
ssh "$HOST" 'systemctl restart hpc-portal && sleep 3 && echo "hpc-portal=$(systemctl is-active hpc-portal)  hpc-agent=$(systemctl is-active hpc-agent)"'

echo "==> 启动日志（最近 40 行，关注 streamer 启动 / 数据库自愈 / 报错）"
ssh "$HOST" 'journalctl -u hpc-portal -n 40 --no-pager'

echo "==> 健康检查（生产 uvicorn 固定监听 127.0.0.1:8000）"
ssh "$HOST" 'curl -s http://127.0.0.1:8000/health || echo "(health 未响应，看上面日志)"'

echo
echo "==> 完成。浏览器开门户验证任务详情页网盘分享 UI；"
echo "    白名单用户 user07 的任务完成后应自动上传并生成分享链接。"
echo "    回滚：服务器 /opt/hpc-portal/.deploy-bak/<时间戳>/ 下有 app 与 portal.db 快照。"
