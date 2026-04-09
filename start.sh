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

echo "🔑 Clés monitoring chargées depuis OpenClaw :"
echo "   OpenAI Mon    : ${OPENAI_API_KEY_MONITORING:0:20}..."
echo "   OpenRouter Mon: ${OPENROUTER_API_KEY_MONITORING:0:20}..."
echo "   Anthropic Mon : ${ANTHROPIC_API_KEY_MONITORING:0:20}..."

docker compose up -d "$@"

echo ""
echo "✅ Dashboard : http://localhost:8888"
