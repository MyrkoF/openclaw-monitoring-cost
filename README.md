# AI Monitoring Dashboard

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/MyrkoF/openclaw-monitoring-cost)](https://github.com/MyrkoF/openclaw-monitoring-cost/releases)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-orange?logo=buy-me-a-coffee)](https://buymeacoffee.com/myrko.f)

Self-hosted Streamlit dashboard for tracking AI provider costs, usage per model, and VPS health. Runs in Docker with host networking, designed for personal VPS behind a VPN.

<!-- Screenshot placeholder -->

## Features

### AI Costs tab

- **OpenClaw Sessions (live)** -- real-time cost per model from gateway API with provider badges (`claude-cli`, `openrouter`, `openai`, `anthropic`)
- **OpenRouter** -- remaining credits, total spend, cost per model
- **OpenAI (merged card)** -- Admin API usage + ChatGPT Plus OAuth rate limits (requests/tokens progress bars) in a single card
- **Anthropic (merged card)** -- API billing + Claude Code stats + Claude-cli rate limit bar (Max subscription ~80 msg/h sonnet) + subprocess session tracking
- **Google Gemini** -- estimated cost from logs, optional real GCP billing via service account
- **Provider routing** -- detects `claude-cli` (Max subscription, included), `anthropic` (API pay-as-you-go), `openai-codex` (ChatGPT Plus), `openrouter` from live session data
- **Period selector** -- 1d / 7d / 30d filtering

### System Health tab

- **Live charts** -- CPU, RAM, Network I/O (SQLite-backed, 10s collection)
- **System** -- uptime, CPU/RAM %, multiple disks, network stats (via `/proc`)
- **Docker** -- running containers, top-5 CPU stats
- **Watchtower** -- update sessions with image names
- **Fail2ban** -- active jails, banned IPs
- **UFW** -- external attacks only (filters Docker/internal IPs), "Full log" expander
- **WireGuard** -- interfaces, connected peers, handshakes, traffic
- **Services** -- systemd unit status
- **DevTools** -- GitHub CLI auth, tmux sessions
- **APT** -- recent upgrades, upgradable count, auto-upgrade timer

### OpenClaw card (System Health tab)

- **Version check** -- installed vs latest stable release via GitHub Atom feed (skips beta/alpha/rc)
- **Doctor (structured)** -- Matrix status, agents, heartbeat, sessions store, plugin errors
- **Security (classified)** -- warnings evaluated against actual config conditions, not generic alerts
  - Protection conditions: `loopback`, `allowlist`, `single user`, `comm deny`, `web deny`
  - Classified as: danger (always visible), warning (expander), silenced (closed "Baseline warnings")
- **Cron jobs** -- schedule, last/next run, status, duration (from `jobs.json`, no HTTP tool exposure)
- **Claude subprocess sessions** -- all agents, provider, model, runtime, tokens

### General

- Backend collect interval configurable from 30s to 12h
- Page updates on user interaction (period change, refresh button, tab switch)
- Background worker threads for gateway, health, metrics collection
- Persistent cache -- data displayed even between refreshes

## Quick Start

```bash
# 1. Clone
git clone https://github.com/MyrkoF/openclaw-monitoring-cost.git
cd openclaw-monitoring-cost

# 2a. With OpenClaw installed (recommended) -- keys injected automatically
./start.sh

# 2b. Without OpenClaw -- manual .env
cp .env.example .env
# Edit .env with your API keys
docker compose up -d --build

# 3. Set up host sidecar (see below)

# 4. Open http://localhost:8888
```

## Configuration

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY_MONITORING` | Yes | OpenAI admin key (scope `api.usage.read`) |
| `OPENROUTER_API_KEY_MONITORING` | Yes | OpenRouter API key |
| `ANTHROPIC_API_KEY_MONITORING` | Yes | Anthropic API key |
| `GOOGLE_API_KEY` | No | Gemini API key (direct, not via OpenRouter) |
| `OPENCLAW_GATEWAY_URL` | No | Gateway endpoint (default `https://127.0.0.1:18789`) |
| `OPENCLAW_GATEWAY_TOKEN` | No | Auth token for OpenClaw gateway API |
| `CHATGPT_OAUTH_TOKEN` | No | ChatGPT Plus OAuth token (rate limits, plan info) |
| `ANTHROPIC_CONSOLE_API_KEY` | No | Anthropic Console key for prepaid credit balance |
| `WATCHTOWER_API_URL` | No | Watchtower HTTP API (default `http://127.0.0.1:8080`) |
| `WATCHTOWER_API_TOKEN` | No | Watchtower token (empty = fallback to sidecar) |
| `WEBMIN_URL` | No | Webmin XML-RPC endpoint |
| `WEBMIN_USER` / `WEBMIN_PASSWORD` | No | Webmin credentials |
| `GOOGLE_SA_KEY_PATH` | No | GCP service account JSON path inside container |

### Volumes

| Host path | Container path | Purpose |
|---|---|---|
| `./data` | `/data` | SQLite DB, health cache, sidecar JSON |
| `~/.openclaw/logs` | `/openclaw-logs` | OpenClaw logs (read-only) |
| `~/.openclaw/agents` | `/openclaw-sessions` | Agent sessions + `sessions.json` (read-only) |
| `~/.openclaw/cron/runs` | `/openclaw-cron` | Cron runs (read-only) |
| `~/.openclaw/cron/jobs.json` | `/openclaw-cron-jobs.json` | Cron job definitions (read-only) |
| `~/.claude` | `/claude-home` | Claude Code CLI -- **opt-in** |
| `~/google-sa-key.json` | `/google-sa-key.json` | GCP service account -- **opt-in** |

### Host networking

The container runs with `network_mode: host`. No port mapping needed -- dashboard listens on `:8888`. Direct access to `127.0.0.1` services (OpenClaw Gateway, Watchtower, Webmin).

## Architecture

```
HOST (cron every 10 min)
  daily-health-check.py
    reads  <-- ~/.openclaw/openclaw.json        (config conditions)
    reads  <-- ~/.openclaw/exec-approvals.json  (exec security)
    runs   --> openclaw doctor / security audit  (once per 24h, cached)
    writes --> ./data/host-health.json           (classified warnings + health data)

DOCKER CONTAINER (network_mode: host)
  app.py (Streamlit on :8888)
    reads  <-- /data/host-health.json    (sidecar)
    reads  <-- /proc/*                   (live CPU/RAM/disk)
    reads  <-- /openclaw-sessions/*/sessions/sessions.json  (provider routing)
    reads  <-- /openclaw-cron-jobs.json  (cron schedules)
    calls  --> OpenClaw Gateway :18789   (sessions, agents, status)
    calls  --> Provider APIs             (OpenRouter, OpenAI, Anthropic, Google)
    stores --> /data/monitoring.db       (SQLite time-series)
```

No Docker socket mounted. All host data flows through the sidecar JSON.

## Host Sidecar Setup

```bash
# Test manually
python3 daily-health-check.py
python3 -m json.tool data/host-health.json

# Add to crontab (every 10 minutes)
crontab -e
# */10 * * * * cd ~/monitoring && python3 daily-health-check.py >/dev/null 2>&1
```

## Project Structure

```
monitoring/
  app/
    app.py                # Streamlit dashboard
    collectors.py         # Provider API collectors + ChatGPT Plus OAuth
    health_collector.py   # System metrics + OpenClaw gateway + claude-cli tracking
    requirements.txt
  daily-health-check.py   # Host sidecar (cron) + security classification
  data/                   # Runtime data (gitignored)
  Dockerfile
  docker-compose.yml
  start.sh                # Key injection from OpenClaw + docker compose up
  .env.example
  SECURITY.md
  LICENSE
  README.md
```

## Tech Stack

- **Python 3.12** + **Streamlit 1.43** -- web dashboard
- **httpx** -- HTTP client for provider APIs
- **plotly** -- real-time charts
- **psutil** + **pandas** -- metrics and data
- **cryptography** -- JWT for GCP service account
- **SQLite** -- time-series cache
- **Docker** -- host networking deployment

## License

MIT
