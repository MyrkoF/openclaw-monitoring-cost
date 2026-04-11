# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 1.0.x   | :white_check_mark: |

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly:

1. **Do not** open a public issue
2. Email: **myrko@federico.pro**
3. Include: description, steps to reproduce, and potential impact

You should receive a response within 72 hours.

## Security Considerations

This dashboard is designed for **self-hosted, single-user** deployments behind a VPN. It is **not** intended to be exposed to the public internet.

### API Keys

- API keys are injected via environment variables at runtime
- Keys are **never** hardcoded or committed to the repository
- `start.sh` reads keys from the OpenClaw keystore (`~/.openclaw/.env`)
- The `.env` file is gitignored

### Network

- The container runs with `network_mode: host` to access local services (OpenClaw gateway on `127.0.0.1:18789`)
- The dashboard listens on port `8888` -- restrict access via firewall (UFW) or VPN
- The OpenClaw gateway uses self-signed TLS; certificate verification is disabled for localhost connections only

### Data

- No data is sent to external services beyond the configured AI provider APIs
- Health data (host metrics, Docker stats) stays local in `data/host-health.json`
- SQLite metrics database is local and gitignored

### Secrets in Gateway Token

- The OpenClaw gateway token (`OPENCLAW_GATEWAY_TOKEN`) is read from `~/.openclaw/openclaw.json` at startup
- It is passed to the container via environment variable, never stored in files inside the container
