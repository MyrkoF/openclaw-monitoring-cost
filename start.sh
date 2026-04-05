#!/usr/bin/env bash
# start.sh — Lance le container monitoring en injectant les clés depuis OpenClaw.
# Les clés monitoring sont dans openclaw.json env.vars avec suffixe _MONITORING.
# Jamais de clés en dur ici.

set -e

OPENCLAW_JSON="/home/myrko/.openclaw/openclaw.json"

if [ ! -f "$OPENCLAW_JSON" ]; then
  echo "❌ openclaw.json introuvable"
  exit 1
fi

# Clés dédiées monitoring (distinctes des clés prod)
export OPENAI_API_KEY_MONITORING=$(python3 -c "
import json
d = json.load(open('$OPENCLAW_JSON'))
print(d['env']['vars'].get('OPENAI_API_KEY_MONITORING', ''))
" 2>/dev/null)

export OPENROUTER_API_KEY_MONITORING=$(python3 -c "
import json
d = json.load(open('$OPENCLAW_JSON'))
print(d['env']['vars'].get('OPENROUTER_API_KEY_MONITORING', ''))
" 2>/dev/null)

export ANTHROPIC_API_KEY_MONITORING=$(python3 -c "
import json
d = json.load(open('$OPENCLAW_JSON'))
print(d['env']['vars'].get('ANTHROPIC_API_KEY_MONITORING', ''))
" 2>/dev/null)

export GOOGLE_API_KEY=$(python3 -c "
import json
d = json.load(open('$OPENCLAW_JSON'))
print(d['env']['vars'].get('GOOGLE_PLACES_API_KEY', ''))
" 2>/dev/null)

export DB_PATH="/data/monitoring.db"
export OPENCLAW_LOGS_DIR="/openclaw-logs"
export OPENCLAW_SESSIONS_DIR="/openclaw-sessions"

echo "🔑 Clés monitoring chargées depuis OpenClaw :"
echo "   OpenAI Admin  : ${OPENAI_API_KEY_MONITORING:0:20}..."
echo "   OpenRouter Mon: ${OPENROUTER_API_KEY_MONITORING:0:20}..."
echo "   Anthropic Mon : ${ANTHROPIC_API_KEY_MONITORING:0:20}..."

docker compose up -d "$@"

echo ""
echo "✅ Dashboard : http://localhost:8888"
