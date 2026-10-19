#!/bin/sh
# Runs privileged with --net=host: installs syagent egress rules in the Docker daemon's netns.
# Usage: firewall.sh <bridge-interface>
set -eu
BRIDGE=$1

# Use the backend whose ruleset holds Docker's DOCKER-USER chain.
if iptables-nft -S DOCKER-USER >/dev/null 2>&1; then
  V4=iptables-nft V6=ip6tables-nft
elif iptables-legacy -S DOCKER-USER >/dev/null 2>&1; then
  V4=iptables-legacy V6=ip6tables-legacy
else
  echo "firewall: DOCKER-USER chain not found in nft or legacy iptables" >&2
  exit 1
fi

# Declaring a chain in a --noflush restore flushes only that chain; the commit is atomic,
# so re-applying never leaves a window without rules.
{
  echo '*filter'
  echo ':SYAGENT-EGRESS - [0:0]'
  echo ':SYAGENT-INPUT - [0:0]'
  echo '-A SYAGENT-EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN'
  # 198.18.0.0/15 stays open: TUN proxies on the owner's machine hand out fake IPs from it.
  for net in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 \
             192.168.0.0/16 224.0.0.0/4 240.0.0.0/4; do
    echo "-A SYAGENT-EGRESS -d $net -j DROP"
  done
  echo '-A SYAGENT-INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN'
  echo '-A SYAGENT-INPUT -j DROP'
  echo 'COMMIT'
} | ${V4}-restore --noflush

$V4 -C DOCKER-USER -i "$BRIDGE" -j SYAGENT-EGRESS 2>/dev/null || $V4 -I DOCKER-USER 1 -i "$BRIDGE" -j SYAGENT-EGRESS
$V4 -C INPUT -i "$BRIDGE" -j SYAGENT-INPUT 2>/dev/null || $V4 -I INPUT 1 -i "$BRIDGE" -j SYAGENT-INPUT

# The network is IPv4-only; drop any IPv6 (e.g. link-local) that still leaves the bridge.
for chain in FORWARD INPUT; do
  $V6 -C $chain -i "$BRIDGE" -j DROP 2>/dev/null || $V6 -I $chain 1 -i "$BRIDGE" -j DROP
done
echo "firewall: rules applied on $BRIDGE ($V4)"
