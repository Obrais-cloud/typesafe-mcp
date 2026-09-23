#!/bin/zsh
# Deploy the committed state of this repo to the Mac Mini, where the launchd
# jobs (triage, judge-queue, router-shadow, router-ready, fleet-weekly) run as
# `remotework`. The mini keeps a plain copy, not a git clone, so this script is
# the supported way to update it and the reason the two stopped drifting.
#
# - Refuses to run with uncommitted changes: what runs on the mini is a commit.
# - Backs the mini up first, into ~/typesafe-mcp-backups/.
# - Never touches the mini's .env, .venv, runtime output, or its curated
#   triage-levels.json. That cache is hand-tuned on the mini; to change it,
#   pull it into the repo (scp macmini:typesafe-mcp/triage-levels.json .).
# - Verifies imports and unit tests on the mini, then restarts router-shadow.
#
# Usage: zsh deploy/deploy-mini.sh [host]        (default host: macmini)
set -euo pipefail
HOST="${1:-macmini}"
cd "$(dirname "$0")/.."

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Uncommitted changes. Commit first: the mini only ever runs a commit." >&2
  exit 1
fi
REV=$(git rev-parse --short HEAD)
echo "Deploying $REV to $HOST"

ssh -o BatchMode=yes "$HOST" 'mkdir -p ~/typesafe-mcp-backups && tar -czf ~/typesafe-mcp-backups/typesafe-mcp-$(date +%Y%m%d-%H%M%S).tgz --exclude=.venv --exclude="*.log" -C ~ typesafe-mcp && ls -t ~/typesafe-mcp-backups | tail -n +8 | while read f; do rm -f ~/typesafe-mcp-backups/$f; done'

rsync -rlc --itemize-changes \
  --exclude='.git/' --exclude='.venv/' --exclude='.env' --exclude='__pycache__/' \
  --exclude='reports/' --exclude='digests/' --exclude='processed/' --exclude='queue/' \
  --exclude='*.log' --exclude='*.jsonl' --exclude='ROUTER-READY.*' \
  --exclude='triage-levels.json' --exclude='remotework@*' --exclude='DEPLOYED_REV' \
  ./ "$HOST":typesafe-mcp/ | grep -v '^\.d' || true

ssh -o BatchMode=yes "$HOST" "cd ~/typesafe-mcp && echo $REV > DEPLOYED_REV && \
  .venv/bin/python -c 'import core, jevkit, triage, router_ready, judge_queue, fleet_optimize; print(\"imports ok\")' && \
  .venv/bin/python -m unittest -q test_jevkit 2>&1 | tail -2 && \
  launchctl kickstart -k gui/\$(id -u)/com.remotework.typesafe-router-shadow && echo 'router-shadow restarted'"
echo "Deployed $REV to $HOST"
