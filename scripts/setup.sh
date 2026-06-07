#!/usr/bin/env bash
# OpenSwarm first-time setup (inspired by OpenClaw install flow).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> OpenSwarm setup"
echo "    Project: $ROOT"

if [[ ! -d .venv ]]; then
  echo "==> Creating Python venv"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Installing OpenSwarm"
pip install -q -U pip
pip install -q -e ".[dev,telegram]"

if [[ ! -f .env ]]; then
  echo "==> Creating .env from .env.example"
  cp .env.example .env
fi

if [[ ! -f config/openswarm.toml ]]; then
  echo "==> Creating config/openswarm.toml"
  cp config/openswarm.example.toml config/openswarm.toml
fi

# Migrate legacy secrets from config/user.yaml if present
if [[ -f config/user.yaml ]]; then
  echo "==> Importing keys from config/user.yaml into .env"
  EXA_KEY=$(grep -E '^\s*exa:' config/user.yaml | head -1 | sed 's/.*: *"\?\([^"]*\)"\?.*/\1/' || true)
  if [[ -n "${EXA_KEY:-}" ]] && ! grep -q '^EXA_API_KEY=.' .env; then
    echo "EXA_API_KEY=$EXA_KEY" >> .env
  fi
fi

# Prompt for essentials if still empty
prompt_if_empty() {
  local key="$1" prompt="$2"
  if ! grep -q "^${key}=." .env 2>/dev/null; then
    read -r -p "$prompt: " val || val=""
    if [[ -n "$val" ]]; then
      echo "${key}=${val}" >> .env
    fi
  fi
}

if [[ -t 0 ]]; then
  prompt_if_empty TELEGRAM_BOT_TOKEN "Telegram bot token (optional)"
  prompt_if_empty NVIDIA_API_KEY "NVIDIA API key for NIM (optional)"
  prompt_if_empty OLLAMA_MODEL "Ollama model name (default llama3.2)"
fi

echo "==> Preparing agent workspace"
mkdir -p workspaces/agent data workspaces
if [[ -d workspaces/default && ! -f workspaces/agent/SOUL.md ]]; then
  echo "    Migrating workspaces/default → workspaces/agent"
  cp -R workspaces/default/. workspaces/agent/
fi

python -c "from workspace.taskboard import ensure_agent_workspace; ensure_agent_workspace('$ROOT')"

echo "==> Running openswarm init"
openswarm init 2>/dev/null || true

echo ""
echo "Done. Next steps:"
echo "  1. Edit .env with your LLM + Telegram keys"
echo "  2. source .venv/bin/activate"
echo "  3. openswarm start"
echo "  4. Open http://127.0.0.1:8000/ui/ and message your Telegram bot"
echo ""
echo "Profiles: OPENSWARM_LLM_PROFILE=nim (24/7) or ollama (local fast tests)"
