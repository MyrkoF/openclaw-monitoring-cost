# AI Monitoring Dashboard

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/MyrkoF/openclaw-monitoring-cost)](https://github.com/MyrkoF/openclaw-monitoring-cost/releases)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-orange?logo=buy-me-a-coffee)](https://buymeacoffee.com/myrko.f)

Self-hosted Streamlit dashboard for tracking AI provider costs, usage per model, and VPS health. Runs in Docker with host networking, designed for personal VPS behind a VPN.

<!-- Screenshot placeholder -->

## What's new in v2.1

**Stability refactor** — eliminates the OpenClaw freeze/lock issues from v1.x:
- Sidecar atomic writes, frequency windows (heavy collectors 1×/day, light 30min)
- Container: process-level singleton thread instead of per-tab → no more gateway flooding when multiple browser tabs open
- JSONL parsing: single pass with mtime cache instead of recursive glob × providers
- Removed periodic `openclaw doctor`/`security audit` calls (was the root cause of locks)

**New OpenClaw tab** — dedicated security/observability surface:
- Interactive warnings table with **Tolerated** checkbox (per-user decision, persisted in `/data/security-decisions.json`)
- Native severity (`critical` / `warn` / `info`) preserved from OpenClaw audit, counters per level
- Cron history with success/failed counts over the selected period
- Doctor structured display, gateway live sessions, Claude subprocess sessions

**Period selector now global** — `1d / 7d / 30d` affects:
- AI costs (existing)
- UFW external attacks
- Fail2ban bans
- Cron history

**Security enrichments**:
- UFW: GeoIP for blocked IPs (country + ISP via `ip-api.com` batch, no API key needed)
- WireGuard: peer summary (active / recent / stale)
- AdGuard DNS: new card with global stats + blocked queries per VPN client
- Fail2ban: ban counts over period (parses log rotation `.log.1`, `.log.2.gz`, etc.)

**UX**:
- Refresh button writes `/data/.refresh-requested` flag → next sidecar run executes the heavy audit
- Times displayed in server timezone (`TZ` env var, default `America/Cancun`) instead of fixed UTC
- ChatGPT Plus OAuth section always visible (was hidden when token expired)
- OpenAI cost: filter API buckets by date (was aggregating lifetime usage as last-30d)

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

- **Live charts** -- CPU, RAM, Network I/O (SQLite-backed, 10s collection, non-blocking psutil)
- **System** -- uptime, CPU/RAM %, multiple disks, network stats (via `/proc`)
- **Docker** -- running containers, top-5 CPU stats
- **Watchtower** -- update sessions with image names (collected on-demand or 1×/day)
- **Fail2ban** -- active jails + ban counts over selected period (1d/7d/30d)
- **UFW** -- external attacks over selected period, GeoIP enrichment (country + ISP), full log expander
- **WireGuard** -- interfaces, connected peers, handshakes, traffic, peer_summary (active/recent/stale)
- **AdGuard DNS** -- query stats, block rate, blocked domains per VPN client
- **Services** -- systemd unit status
- **DevTools** -- GitHub CLI auth, tmux sessions
- **APT** -- recent upgrades, upgradable count, auto-upgrade timer
- **OpenClaw summary card** -- compact version + counters (critical/warn/info/tolerated) + live sessions, links to OpenClaw tab

### OpenClaw tab (new in v2.1)

Dedicated tab for OpenClaw observability and security:

- **Version check** -- installed vs latest stable release via GitHub Atom feed (cached 1h)
- **Doctor (structured)** -- Matrix/Mattermost status, agents, heartbeat, sessions store, plugin errors
- **Security counters** -- per native severity from OpenClaw audit:
  - 🔴 critical / 🟡 warn / 🔵 info / ✅ tolerated
  - **Tolerated** = warnings the user has reviewed and explicitly accepted (cosmetic, false positives)
  - Decisions persisted across rebuilds/reboots in `/data/security-decisions.json`
- **Interactive warnings table** -- single checkbox per warning, default unchecked (= "not yet reviewed = treated as danger")
- **Gateway live sessions** -- active sessions, total cost/tokens, by channel
- **Cron jobs** -- jobs config + success/failed counts over period (from JSONL run history)
- **Claude subprocess sessions** -- per agent: provider, model, runtime, tokens, cost (or "included" for claude-cli)

### General

- Backend collect interval configurable from 2min to 12h (minimum 2min to avoid gateway flooding)
- Page updates on user interaction (period change, refresh button, tab switch)
- **2 background threads total** (was 4 per browser tab in v1.x)
- Persistent cache -- data displayed even between refreshes
- Times in server timezone (`TZ` env var)

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
| `ADGUARD_URL` | No | AdGuard Home admin URL (default `http://10.8.0.1:3000`) |
| `ADGUARD_USER` / `ADGUARD_PASSWORD` | No | AdGuard Home admin credentials (for DNS stats card) |
| `TZ` | No | Container timezone (default falls back to UTC, recommended: `America/Cancun` etc.) |

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
HOST (cron every 30 min)
  daily-health-check.py
    Light collectors (every run):
      meta, resources, docker, services, wireguard, fail2ban,
      ufw (with GeoIP), tmux, claude_code, adguard, cron_history
    Heavy collectors (1×/day OR when /data/.refresh-requested flag exists):
      openclaw doctor, security audit (with severity parsing),
      openclaw version, network, github cli, watchtower logs
    writes --> ./data/host-health.json   (atomic write via tempfile + os.replace)

DOCKER CONTAINER (bridge network, port :8888)
  app.py (Streamlit)
    threads = 2 process-level singletons:
      live_collector  (10s) — psutil → SQLite (non-blocking)
      unified_worker  (≥120s, configurable) — gateway + system health + webmin
    reads  <-- /data/host-health.json   (atomic write from sidecar)
    reads  <-- /proc/*                  (live CPU/RAM/disk)
    reads  <-- /openclaw-sessions/*/sessions/sessions.json  (provider routing)
    reads  <-- /openclaw-cron-jobs.json  (cron schedules)
    calls  --> OpenClaw Gateway :18789  (1× per worker cycle, with backoff)
    calls  --> Provider APIs            (cached 5min: OpenRouter, OpenAI, Anthropic, Google)
    writes --> /data/security-decisions.json  (user warning tolerance)
    stores --> /data/metrics.db         (SQLite time-series, persistent connection)
```

No Docker socket mounted. All host data flows through the sidecar JSON.

User-action **Refresh button** writes `/data/.refresh-requested` flag → next sidecar run
(within 30min) executes the heavy audit collectors.

## Host Sidecar Setup

```bash
# Source the .env for secrets (AdGuard, etc.) before running
. /home/myrko/.openclaw/.env
HEALTH_SIDECAR=/path/to/data/host-health.json python3 daily-health-check.py
python3 -m json.tool data/host-health.json

# Add to crontab (every 30 minutes is plenty)
crontab -e
# */30 * * * * . /home/myrko/.openclaw/.env && HEALTH_SIDECAR=/path/to/data/host-health.json /usr/bin/python3 /path/to/daily-health-check.py >/dev/null 2>&1
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
