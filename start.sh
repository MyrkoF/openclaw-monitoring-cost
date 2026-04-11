#!/usr/bin/env bash
# start.sh — Lance le container monitoring en injectant les clés depuis OpenClaw.
# Les clés sont dans ~/.openclaw/.env
# Jamais de clés en dur ici.

set -e

OPENCLAW_ENV="${HOME}/.openclaw/.env"

if [ ! -f "$OPENCLAW_ENV" ]; then
  echo "❌ ~/.openclaw/.env introuvable"
  exit 1
fi

# Charger les clés depuis le .env OpenClaw
set -a
source "$OPENCLAW_ENV"
set +a

# Mapping vers les noms attendus par docker-compose.yml
export OPENAI_API_KEY_MONITORING
export OPENROUTER_API_KEY_MONITORING
export ANTHROPIC_API_KEY_MONITORING
export GOOGLE_API_KEY="${GEMINI_API_KEY:-}"

export DB_PATH="/data/monitoring.db"
export OPENCLAW_LOGS_DIR="/openclaw-logs"
export OPENCLAW_SESSIONS_DIR="/openclaw-sessions"

# OpenClaw Gateway API token (live sessions/agents)
# Read from JSON config directly (CLI redacts sensitive values)
if [ -z "${OPENCLAW_GATEWAY_TOKEN:-}" ]; then
  OPENCLAW_JSON="${HOME}/.openclaw/openclaw.json"
  if [ -f "$OPENCLAW_JSON" ]; then
    OPENCLAW_GATEWAY_TOKEN=$(python3 -c "import json; print(json.load(open('$OPENCLAW_JSON')).get('gateway',{}).get('auth',{}).get('token',''))" 2>/dev/null || echo "")
  fi
fi
export OPENCLAW_GATEWAY_TOKEN
export OPENCLAW_GATEWAY_URL="https://host.docker.internal:18789"

echo "🔑 Clés monitoring chargées depuis OpenClaw :"
echo "   OpenAI Mon    : ${OPENAI_API_KEY_MONITORING:0:20}..."
echo "   OpenRouter Mon: ${OPENROUTER_API_KEY_MONITORING:0:20}..."
echo "   Anthropic Mon : ${ANTHROPIC_API_KEY_MONITORING:0:20}..."
echo "   OpenClaw GW   : ${OPENCLAW_GATEWAY_TOKEN:+configured}${OPENCLAW_GATEWAY_TOKEN:-not set}"

docker compose up -d "$@"

echo ""
echo "✅ Dashboard : http://localhost:8888"
