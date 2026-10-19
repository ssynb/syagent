#!/bin/bash
# Container supervisor: seeds /agent on first start, then keeps the agent process running.
set -u

if [ ! -d /agent/.git ]; then
  cp -a /opt/syagent/seed/. /agent/
  git -C /agent init -q
  git -C /agent add -A
  git -C /agent commit -qm "seed"
fi
mkdir -p /run/syagent /inbox /outbox

while :; do
  python /agent/main.py
  echo "syagent: agent exited with status $?, restarting" >&2
  sleep 1
done
