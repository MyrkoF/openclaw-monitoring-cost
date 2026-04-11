# AI Monitoring Dashboard

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-orange?logo=buy-me-a-coffee)](https://buymeacoffee.com/myrko.f)

Self-hosted Streamlit dashboard for tracking AI provider costs, usage per model, and VPS health. Runs in Docker with host networking, designed for personal VPS behind a VPN.

<!-- Screenshot placeholder -->

## Features

### AI Costs tab

- **OpenRouter** -- remaining credits, total spend, cost per model
- **OpenAI** -- usage per model via Admin API (`/organization/costs`)
- **Anthropic (merged card)** -- API billing (prepaid credits) + Claude Code local stats (sessions, messages, tokens) in a single card
- **Google Gemini** -- estimated cost from logs, optional real GCP billing via service account
- **OpenClaw Gateway** -- live session/agent/cron data from the gateway API (port 18789), real-time cost per model with provider badges
- **Provider mapping** -- shows which provider routes each model (derived from live API data, not config files)
- **Period selector** -- 1d / 7d / 30d filtering across all cards

### System Health tab

- **Live charts** -- CPU, RAM, Network I/O with 10s auto-refresh (SQLite-backed)
- **System** -- uptime, CPU/RAM %, multiple disks, network stats (via `/proc`)
- **Docker** -- running containers, top-5 CPU stats
- **Watchtower** -- update sessions with image names
- **Fail2ban** -- active jails, banned IPs
- **UFW** -- blocks/hour, top blocked IPs, filtered to external attacks only (hides Docker/internal IPs), "Full log" expander for complete data
- **WireGuard** -- interfaces, connected peers, handshakes, traffic
- **Services** -- systemd unit status
- **DevTools** -- GitHub CLI auth, tmux sessions
- **APT** -- recent upgrades, upgradable count, auto-upgrade timer

### OpenClaw card (System Health tab)

- **Version check** -- installed vs latest stable release detected via GitHub Atom feed; red badge when update available (skips beta/alpha/rc)
- **Doctor (structured)** -- Matrix status, agents, heartbeat, sessions store, plugin errors, skills blocked, memory plugin
- **Security (structured)** -- summary (critical/warn/info), warnings with suggested fix, attack surface
- Raw detail expanders for debugging

### General

- Global status badge aggregated from all sections
- UI auto-refresh every 10s via `streamlit-autorefresh` (no full page reload)
- Backend collect interval configurable from 30s to 12h (controls worker thread frequency)
- Persistent cache -- data displayed even between refreshes
- Background threads -- system metrics (5 min), Webmin (30s), live collector (10s)

## Quick Start

```bash
# 1. Clone
git clone <repo-url>
cd monitoring

# 2a. With OpenClaw installed (recommended) -- keys injected automatically
./start.sh

# 2b. Without OpenClaw -- manual .env
cp .env.example .env
# Edit .env with your API keys
docker compose up -d --build

# 3. Set up host sidecar (see Host Sidecar Setup below)

# 4. Open dashboard
# http://localhost:8888
```

## Configuration

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY_MONITORING` | Yes | OpenAI admin key (scope `api.usage.read`) |
| `OPENROUTER_API_KEY_MONITORING` | Yes | OpenRouter API key |
| `ANTHROPIC_API_KEY_MONITORING` | Yes | Anthropic API key |
| `GOOGLE_API_KEY` | No | Gemini API key (direct usage, not via OpenRouter) |
| `ANTHROPIC_CONSOLE_API_KEY` | No | Anthropic Console key for prepaid credit balance |
| `OPENCLAW_GATEWAY_URL` | No | OpenClaw gateway endpoint (default `https://127.0.0.1:18789`) |
| `OPENCLAW_GATEWAY_TOKEN` | No | Auth token for the OpenClaw gateway API |
| `WATCHTOWER_API_URL` | No | Watchtower HTTP API URL (default `http://127.0.0.1:8080`) |
| `WATCHTOWER_API_TOKEN` | No | Watchtower API token (empty = fallback to sidecar) |
| `WEBMIN_URL` | No | Webmin XML-RPC endpoint |
| `WEBMIN_USER` / `WEBMIN_PASSWORD` | No | Webmin credentials |
| `GOOGLE_SA_KEY_PATH` | No | Path to GCP service account JSON inside container |

### Volumes

| Host path | Container path | Purpose |
|---|---|---|
| `./data` | `/data` | SQLite DB, health cache, sidecar JSON |
| `~/.openclaw/logs` | `/openclaw-logs` | OpenClaw logs (read-only) |
| `~/.openclaw/agents` | `/openclaw-sessions` | OpenClaw agent sessions (read-only) |
| `~/.openclaw/cron/runs` | `/openclaw-cron` | OpenClaw cron runs (read-only) |
| `~/.claude` | `/claude-home` | Claude Code CLI config -- **opt-in**, uncomment in `docker-compose.yml` |
| `~/google-sa-key.json` | `/google-sa-key.json` | GCP service account -- **opt-in**, uncomment in `docker-compose.yml` |

### Host networking

The container runs with `network_mode: host`. No port mapping is needed -- the dashboard listens on port 8888 directly on the host. This also gives the container access to `127.0.0.1` services (Watchtower, OpenClaw Gateway, Webmin) without `host.docker.internal` workarounds.

## Architecture

```
HOST (cron every 10 min)
  daily-health-check.py
    writes --> ./data/host-health.json
      (Docker, Watchtower, APT, OpenClaw doctor/security,
       UFW, Fail2ban, WireGuard, Services, DevTools)

DOCKER CONTAINER (network_mode: host)
  app.py (Streamlit on :8888)
    reads  <-- /data/host-health.json    (shared volume ./data:/data)
    reads  <-- /proc/*                   (live CPU/RAM/disk/network)
    reads  <-- /openclaw-logs, sessions, cron   (OpenClaw usage data)
    calls  --> OpenRouter API            (credits, usage)
    calls  --> OpenAI Admin API          (cost per model)
    calls  --> Anthropic API             (billing, optional console)
    calls  --> Google Cloud Billing API  (optional, via service account)
    calls  --> OpenClaw Gateway :18789   (live sessions/agents/cron)
    calls  --> Watchtower :8080          (optional, image updates)
    stores --> /data/monitoring.db       (SQLite -- time-series cache)
```

The container does **not** mount the Docker socket. All host-level data flows through the sidecar JSON file.

## Host Sidecar Setup

`daily-health-check.py` runs on the **host** (not inside Docker) and collects data for the Docker, Watchtower, APT, OpenClaw, UFW, Fail2ban, WireGuard, and Services sections.

```bash
# Test manually
cd ~/monitoring
python3 daily-health-check.py

# Verify output
python3 -m json.tool data/host-health.json

# Add to crontab (every 10 minutes)
crontab -e
# */10 * * * * cd ~/monitoring && python3 daily-health-check.py >/dev/null 2>&1
```

## Optional: Google Cloud Billing

To display real GCP spend (not just log-based estimates):

1. Go to [GCP Console](https://console.cloud.google.com) > IAM > Service Accounts
2. Select the service account linked to Gemini usage
3. Keys tab > Add Key > JSON > download
4. Copy to host: `cp key.json ~/google-sa-key.json`
5. Ensure the account has the `Billing Account Viewer` role
6. Uncomment the volume line in `docker-compose.yml`:
   ```yaml
   - ~/google-sa-key.json:/google-sa-key.json:ro
   ```

## Optional: Anthropic Console Billing

To display Anthropic prepaid credit balance:

- **Option A** -- Dedicated Console key:
  Create a billing-scoped key at [console.anthropic.com](https://console.anthropic.com).
  Set `ANTHROPIC_CONSOLE_API_KEY=sk-ant-...` in `.env`.

- **Option B** -- Claude Code CLI token:
  Uncomment the volume in `docker-compose.yml`:
  ```yaml
  - ${HOME}/.claude:/claude-home:ro
  ```
  The dashboard reads the CLI auth token automatically.

## Project Structure

```
monitoring/
  app/
    app.py                # Streamlit dashboard
    collectors.py         # API collectors (OpenRouter, OpenAI, Anthropic, Google, OpenClaw Gateway)
    health_collector.py   # System metrics (via /proc + sidecar JSON)
    requirements.txt
  scripts/                # Utility scripts
  daily-health-check.py   # Host sidecar cron job --> data/host-health.json
  data/                   # SQLite + health cache (gitignored)
  Dockerfile
  docker-compose.yml
  start.sh                # Injects OpenClaw keys and runs docker compose
  .env.example
  README.md
```

## Tech Stack

- **Python 3.12** + **Streamlit** -- web dashboard
- **streamlit-autorefresh** -- 10s UI refresh without page reload
- **httpx** -- async HTTP client for API calls
- **plotly** -- real-time charts (CPU, RAM, Network)
- **psutil** -- live system metrics inside the container
- **pandas** -- data manipulation
- **cryptography** -- JWT signing for GCP service account auth
- **SQLite** -- time-series cache for charts
- **Docker** -- deployment with host networking

## License

MIT
