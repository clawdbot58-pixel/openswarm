#!/usr/bin/env bash
# Smoke test: start swarm, probe health, stop.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source .venv/bin/activate

openswarm stop 2>/dev/null || true
openswarm start

KERNEL_URL="$(python -c "import json; print(json.load(open('data/state.json'))['kernel_url'])")"
DASH_URL="$(python -c "import json; print(json.load(open('data/state.json')).get('dashboard_url','http://127.0.0.1:8000'))")"

echo "Kernel:    $KERNEL_URL/health"
curl -sf "$KERNEL_URL/health" | head -c 200; echo
echo "Dashboard: $DASH_URL/health"
curl -sf "$DASH_URL/health" | head -c 200; echo
echo "Agents:"
curl -sf "$DASH_URL/api/agents" | head -c 400; echo

openswarm run "Say hello and confirm the swarm is wired" || true
sleep 2
openswarm status

echo "Leave running with: openswarm status"
echo "Stop with: openswarm stop"
