#!/usr/bin/env bash
# IPv6 自愈：ISP 下发的前缀变化时，自动把 <当前前缀>::SUFFIX 配到网卡上，
# 删除失效的旧前缀同后缀地址，并校验公网 v6 出口连通性。
#
# 设计原则：
#   - 幂等：地址已正确则什么都不做，可被定时器反复调用；
#   - 安全：只操作 IFACE 上"以 ::SUFFIX 结尾"的全局 IPv6 地址，
#     绝不触碰 IPv4 / SSH / 其它地址；默认路由交给内核的 RA 维护；
#   - 可观测：所有动作写 syslog(tag=ipv6-autoheal) 和 stdout。
#
# 用法：  IFACE=em1 SUFFIX=::100 bash ipv6-autoheal.sh
# 退出码：0=无需处理/已修复  2=检测不到前缀  3=修复后仍不通
set -u

IFACE="${IFACE:-em1}"
SUFFIX="${SUFFIX:-::100}"     # 服务器固定主机部分（希望对外稳定的尾巴）
PLEN="${PLEN:-64}"
PROBE="${PROBE:-240c::6666}"  # 公网 v6 探测目标（电信公共 DNS）
PY="$(command -v python3 || echo /opt/miniconda3/bin/python)"

log() { logger -t ipv6-autoheal -- "$*" 2>/dev/null; echo "$(date '+%F %T') $*"; }

# --- 1) 探测当前应使用的 /64 前缀（多来源，取第一个全局 2000::/3 段）---
detect_prefix_cidr() {
  local p
  # a) 内核从 RA 学到的 on-link 前缀（最贴近内核现状）
  p=$(ip -6 route show dev "$IFACE" proto ra 2>/dev/null \
        | grep -oiE '^[23][0-9a-f:]+/64' | head -1)
  [ -n "$p" ] && { echo "$p"; return 0; }
  # b) 主动发 Router Solicitation 取 RA 前缀
  if command -v rdisc6 >/dev/null 2>&1; then
    p=$(rdisc6 -1 -w 3000 "$IFACE" 2>/dev/null \
          | awk '/Prefix/{print $3}' | grep -iE '/64$' | head -1)
    [ -n "$p" ] && { echo "$p"; return 0; }
  fi
  # c) 退而求其次：网卡上已有的全局 SLAAC 地址所在 /64
  p=$(ip -6 addr show dev "$IFACE" scope global 2>/dev/null \
        | awk '/inet6 [23]/{print $2}' | head -1)
  [ -n "$p" ] && { echo "$p"; return 0; }
  return 1
}

# --- 2) 由前缀 CIDR + 后缀算出目标地址（用 python 保证正确性）---
desired_addr() {
  local cidr="$1"
  PFX="$cidr" SFX="$SUFFIX" "$PY" - <<'PYEOF' 2>/dev/null
import os, ipaddress
net = ipaddress.IPv6Network(os.environ['PFX'], strict=False)
host = int(ipaddress.IPv6Address(os.environ['SFX']))
print(ipaddress.IPv6Address(int(net.network_address) | host))
PYEOF
}

cidr=$(detect_prefix_cidr) || { log "未探测到全局 IPv6 前缀（$IFACE），上游可能无 RA/无 v6"; exit 2; }
desired=$(desired_addr "$cidr")
[ -n "$desired" ] || { log "前缀解析失败: $cidr"; exit 2; }

# --- 3) 删除失效的同后缀旧地址（仅匹配 ::SUFFIX 结尾，绝不误删其它）---
sfx_tail="${SUFFIX##*:}"          # ::100 -> 100
for a in $(ip -6 addr show dev "$IFACE" scope global 2>/dev/null | awk '/inet6/{print $2}'); do
  case "$a" in
    *:"$sfx_tail"/"$PLEN")
      if [ "$a" != "${desired}/${PLEN}" ]; then
        ip -6 addr del "$a" dev "$IFACE" 2>/dev/null && log "删除失效旧地址 $a"
      fi
      ;;
  esac
done

# --- 4) 确保目标地址存在 ---
changed=0
if ! ip -6 addr show dev "$IFACE" | grep -qw "$desired"; then
  if ip -6 addr add "${desired}/${PLEN}" dev "$IFACE" 2>/dev/null; then
    log "配置新地址 ${desired}/${PLEN}（前缀 $cidr）"
    changed=1
  else
    log "添加地址失败 ${desired}/${PLEN}"
  fi
fi

# --- 5) 默认路由：正常由内核 RA 维护；缺失则尽力补一条 ---
if ! ip -6 route show default dev "$IFACE" 2>/dev/null | grep -q .; then
  gw=""
  command -v rdisc6 >/dev/null 2>&1 && \
    gw=$(rdisc6 -1 -w 3000 "$IFACE" 2>/dev/null | awk '/from fe80/{print $2}' | head -1)
  if [ -n "$gw" ]; then
    ip -6 route add default via "$gw" dev "$IFACE" 2>/dev/null \
      && log "补默认路由 via $gw"
  else
    log "警告：无 v6 默认路由且未取得网关"
  fi
fi

# --- 6) 校验公网 v6 出口 ---
if ping6 -c2 -W2 -I "$desired" "$PROBE" >/dev/null 2>&1; then
  [ "$changed" = 1 ] && log "已自愈：$desired 出口正常"
  exit 0
else
  log "修复后公网 v6 仍不通（$desired -> $PROBE），疑上游/ISP 问题"
  exit 3
fi
