#!/usr/bin/env bash
# Force default-bridge containers through FORWARD drop, so they cannot
# reach the internet except via the host-side pred allowlist proxy (INPUT).
# Host clone/pull is unaffected. Evaluation must not call this.
set -euo pipefail

CHAIN=SWEEPER_PRED
CHAIN6=SWEEPER_PRED6

bridge_iface() {
  local name
  name="$(docker network inspect bridge --format '{{index .Options "com.docker.network.bridge.name"}}' 2>/dev/null || true)"
  if [ -n "${name:-}" ]; then
    printf '%s\n' "$name"
    return
  fi
  printf 'docker0\n'
}

drop_jumps() {
  local hook="$1"
  local target="$2"
  local cmd="$3"
  if ! "$cmd" -w 10 -nL "$hook" >/dev/null 2>&1; then
    return 0
  fi
  while "$cmd" -w 10 -L "$hook" -n --line-numbers 2>/dev/null | grep -q "$target"; do
    local num
    num="$("$cmd" -w 10 -L "$hook" -n --line-numbers | awk -v t="$target" '$0 ~ t {print $1; exit}')"
    [ -n "$num" ] || break
    "$cmd" -w 10 -D "$hook" "$num" || break
  done
}

unlock_family() {
  local cmd="$1"
  local hook="$2"
  local chain="$3"
  drop_jumps "$hook" "$chain" "$cmd"
  "$cmd" -w 10 -F "$chain" 2>/dev/null || true
  "$cmd" -w 10 -X "$chain" 2>/dev/null || true
}

unlock() {
  unlock_family iptables DOCKER-USER "$CHAIN"
  if command -v ip6tables >/dev/null 2>&1; then
    unlock_family ip6tables DOCKER-USER "$CHAIN6"
    unlock_family ip6tables FORWARD "$CHAIN6"
  fi
  echo "pred net unlock ok" >&2
}

lock() {
  unlock
  if ! iptables -w 10 -nL DOCKER-USER >/dev/null 2>&1; then
    echo "DOCKER-USER missing; is dockerd up?" >&2
    exit 1
  fi
  local iface
  iface="$(bridge_iface)"
  if ! ip link show "$iface" >/dev/null 2>&1; then
    echo "bridge iface missing: $iface" >&2
    exit 1
  fi
  iptables -w 10 -N "$CHAIN"
  iptables -w 10 -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  iptables -w 10 -A "$CHAIN" -j REJECT --reject-with icmp-port-unreachable
  iptables -w 10 -I DOCKER-USER 1 -i "$iface" -j "$CHAIN"
  if command -v ip6tables >/dev/null 2>&1; then
    if ip6tables -w 10 -nL DOCKER-USER >/dev/null 2>&1; then
      ip6tables -w 10 -N "$CHAIN6" 2>/dev/null || ip6tables -w 10 -F "$CHAIN6"
      ip6tables -w 10 -A "$CHAIN6" -j REJECT
      ip6tables -w 10 -I DOCKER-USER 1 -i "$iface" -j "$CHAIN6"
    elif ip6tables -w 10 -nL FORWARD >/dev/null 2>&1; then
      ip6tables -w 10 -N "$CHAIN6" 2>/dev/null || ip6tables -w 10 -F "$CHAIN6"
      ip6tables -w 10 -A "$CHAIN6" -j REJECT
      ip6tables -w 10 -I FORWARD 1 -i "$iface" -j "$CHAIN6"
    fi
  fi
  echo "pred net lock ok iface=$iface (FORWARD from bridge dropped; proxy is host INPUT)" >&2
}

case "${1:-}" in
  lock) lock ;;
  unlock) unlock ;;
  *)
    echo "usage: pred_net.sh lock|unlock" >&2
    exit 2
    ;;
esac
