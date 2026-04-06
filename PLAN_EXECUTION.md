# PLAN_EXECUTION.md — Handoff for Claude Code CLI

> Ce fichier sert de contexte de reprise pour une nouvelle session Claude Code CLI.
> Lis-le entièrement avant de toucher quoi que ce soit.

---

## Ce qu'est ce projet

Dashboard Streamlit de monitoring des coûts IA + santé VPS.
- Tourne en Docker sur le VPS host (port 8888, VPN only)
- Lit les logs OpenClaw pour estimer les coûts par modèle (OpenRouter, OpenAI, Anthropic, Google)
- Affiche les métriques système via `/proc` (pas de `procps` dans le container)
- Les données host (Docker, Watchtower, APT, OpenClaw doctor/security, Services) viennent d'un sidecar JSON

## Architecture sidecar (push-from-host)

```
HOST (cron */10min)
  └─ daily-health-check.py
       └─ écrit → ./data/host-health.json

DOCKER CONTAINER
  └─ app/health_collector.py
       └─ lit ← /data/host-health.json (via volume ./data:/data)
       └─ lit ← /proc/* (CPU, RAM, disque — natif dans le container)
```

**Pas de docker.sock monté** — sécurité pour partage public.
**Pas d'API OpenClaw pour doctor/audit** — l'API gateway (localhost:18789) n'expose pas ces endpoints.

---

## État du travail — branche `main` (mergée depuis `claude/add-watchtower-logs-FPWFc`)

### Fichiers implémentés

| Fichier | État | Description |
|---|---|---|
| `docker-compose.yml` | ✅ | `${HOME}` paths, sans docker.sock, `~/.claude` + `~/google-sa-key.json` opt-in |
| `start.sh` | ✅ | Lit clés depuis `~/.openclaw/openclaw.json`, lance docker compose |
| `.env.example` | ✅ | Template pour déploiement sans OpenClaw |
| `README.md` | ✅ | Option A (start.sh) / Option B (.env), architecture sidecar documentée |
| `daily-health-check.py` | ✅ | Cron host — Docker, Watchtower, APT, Services, OpenClaw doctor/security |
| `app/health_collector.py` | ✅ | /proc metrics + lecture sidecar, backward-compat |
| `app/app.py` | ✅ | Cards : Système, APT, Docker (compteurs), Watchtower (erreurs), Services |

### Cards actuelles dans System Health tab

| Card | Données | Source |
|---|---|---|
| 🖥️ Système | uptime, load, CPU%, RAM, disque | `/proc` (natif container) |
| 📦 APT | updates 24h, upgradable count | sidecar |
| 🐳 Docker | containers, running▲/stopped▼/total | sidecar |
| 🔄 Watchtower | images mises à jour, erreurs, source badge | sidecar ou HTTP API |
| ⚙️ Services | docker, caddy, nginx, ssh, ufw, fail2ban | sidecar |
| 🔒 OpenClaw Doctor | output commande | sidecar (expander) |
| 🛡️ Security Audit | output commande | sidecar (expander) |

---

## ⏳ PROCHAINE TÂCHE — VPN + DevTools monitoring

### Objectif

Ajouter 3 nouveaux blocs dans le sidecar et 2 nouvelles cards dans le dashboard :
- **🔒 WireGuard** : interfaces wg0 + wg-mikrotik, peers connectés, handshakes, trafic
- **🛠️ DevTools** : GitHub CLI auth (compte MyrkoF) + sessions tmux actives

### Contexte VPS connu

- WireGuard : 2 interfaces actives — `wg0` (3 peers) et `wg-mikrotik` (2 peers)
- GitHub CLI : authentifié via `GITHUB_TOKEN`, compte `MyrkoF`
- tmux : 1 session `monitoring` active

---

### Fichier 1 — `daily-health-check.py`

Ajouter `import re` en haut (s'il n'y est pas), puis 3 nouvelles fonctions :

#### `collect_wireguard()`

```python
def collect_wireguard():
    """Parse `wg show all dump` (tab-separated). Tente sans sudo puis avec."""
    out, err, rc = run("wg show all dump 2>/dev/null || sudo wg show all dump 2>/dev/null")
    if not out:
        return {"interfaces": [], "total_peers": 0, "connected_peers": 0, "status": "unavailable"}

    import re as _re
    interfaces = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 5:             # ligne interface
            iface = parts[0]
            interfaces[iface] = {"port": parts[3], "peers": []}
        elif len(parts) == 9:           # ligne peer
            iface, pubkey, _, endpoint, allowed_ips, last_hs, rx, tx, _ = parts
            if iface not in interfaces:
                interfaces[iface] = {"port": "?", "peers": []}
            hs = int(last_hs)
            hs_str = ("never" if hs == 0
                      else f"{hs}s ago" if hs < 180
                      else f"{hs//60}min ago" if hs < 3600
                      else f"{hs//3600}h ago")
            interfaces[iface]["peers"].append({
                "pubkey_short": pubkey[:8] + "…",
                "endpoint":     endpoint if endpoint != "(none)" else None,
                "allowed_ips":  allowed_ips,
                "handshake":    hs_str,
                "rx_mb":        round(int(rx) / 1_048_576, 1),
                "tx_mb":        round(int(tx) / 1_048_576, 1),
                "connected":    0 < hs < 300,
            })

    iface_list = []
    for name, d in interfaces.items():
        connected = sum(1 for p in d["peers"] if p["connected"])
        iface_list.append({
            "name":            name,
            "port":            d["port"],
            "peers_total":     len(d["peers"]),
            "peers_connected": connected,
            "peers":           d["peers"],
        })

    return {
        "interfaces":      iface_list,
        "total_peers":     sum(i["peers_total"]     for i in iface_list),
        "connected_peers": sum(i["peers_connected"] for i in iface_list),
        "status":          "ok" if iface_list else "unavailable",
    }
```

#### `collect_github_auth()`

```python
def collect_github_auth():
    out, err, rc = run("gh auth status 2>&1", timeout=10)
    combined = (out + "\n" + err).strip()
    import re as _re
    account, token_src = "", ""
    for line in combined.splitlines():
        m = _re.search(r'account (\S+)', line)
        if m:
            account = m.group(1).strip("()")
        m2 = _re.search(r'\(([A-Z_]+)\)', line)
        if m2:
            token_src = m2.group(1)
    return {
        "authenticated": rc == 0,
        "account":       account,
        "token_source":  token_src,
        "status":        "ok" if rc == 0 else "error",
    }
```

#### `collect_tmux()`

```python
def collect_tmux():
    out, err, rc = run("tmux ls 2>/dev/null")
    if rc != 0 or not out:
        return {"sessions": [], "count": 0, "status": "ok"}
    import re as _re
    sessions = []
    for line in out.splitlines():
        if ":" not in line:
            continue
        name = line.split(":")[0].strip()
        m = _re.search(r'(\d+) windows?', line)
        windows = int(m.group(1)) if m else 0
        sessions.append({"name": name, "windows": windows, "attached": "(attached)" in line})
    return {"sessions": sessions, "count": len(sessions), "status": "ok"}
```

#### Dans `build_report()` — ajouter dans le dict retourné :

```python
"wireguard": collect_wireguard(),
"github_cli": collect_github_auth(),
"tmux":       collect_tmux(),
```

Et dans le calcul `global_status`, inclure `wireguard["status"]`.

---

### Fichier 2 — `app/health_collector.py`

Dans `collect_system()`, après la lecture du sidecar, ajouter :

```python
wireguard  = sidecar.get("wireguard", {})
github_cli = sidecar.get("github_cli", {})
tmux       = sidecar.get("tmux", {})
```

Ajouter dans le `result` dict :

```python
"wireguard":  wireguard,
"github_cli": github_cli,
"tmux":       tmux,
```

---

### Fichier 3 — `app/app.py`

Layout cible de la System Health tab :

```
col1                       col2
─────────────────────      ─────────────────────
🖥️ Système                🐳 Docker
📦 APT                    🔄 Watchtower
🔒 WireGuard (NEW)        ⚙️ Services
                           🛠️ DevTools (NEW)
```

#### Card WireGuard — ajouter dans `col1`, après le bloc APT

```python
wg = health.get("wireguard", {})
if wg.get("interfaces"):
    total_c = wg.get("connected_peers", 0)
    total_p = wg.get("total_peers", 0)
    wg_clr  = "green" if total_c == total_p and total_p > 0 else ("yellow" if total_c > 0 else "red")
    iface_rows = ""
    for iface in wg["interfaces"]:
        c = iface["peers_connected"]; t = iface["peers_total"]
        badge_clr = "badge-green" if c == t and t > 0 else "badge"
        iface_rows += (
            f'<div class="model-row">'
            f'<span><b>{iface["name"]}</b> :{iface["port"]}</span>'
            f'<span class="badge {badge_clr}">{c}/{t} peers</span>'
            f'</div>'
        )
        for p in iface["peers"]:
            rx = f'{p["rx_mb"]}MB↓' if p.get("rx_mb", 0) > 0 else ""
            tx = f'{p["tx_mb"]}MB↑' if p.get("tx_mb", 0) > 0 else ""
            ep = p.get("endpoint") or "no endpoint"
            dot = "🟢" if p.get("connected") else "🔴"
            iface_rows += (
                f'<div class="sub-num" style="margin-left:8px;margin-top:2px">'
                f'{dot} {p["pubkey_short"]} · {ep} · {p["handshake"]} · {rx} {tx}'
                f'</div>'
            )
    st.markdown(
        f'<div class="card">'
        f'<div class="card-header">🔒 WireGuard — {total_c}/{total_p} actifs</div>'
        f'{iface_rows}'
        f'</div>',
        unsafe_allow_html=True,
    )
```

#### Card DevTools — ajouter dans `col2`, après le bloc Services

```python
gh  = health.get("github_cli", {})
tmx = health.get("tmux", {})
if gh or tmx:
    dev_rows = ""
    if gh:
        gh_clr   = "badge-green" if gh.get("authenticated") else "badge"
        gh_label = gh.get("account") or "non authentifié"
        gh_src   = f' · {gh["token_source"]}' if gh.get("token_source") else ""
        dev_rows += (
            f'<div class="model-row">'
            f'<span>GitHub CLI</span>'
            f'<span class="badge {gh_clr}">{gh_label}{gh_src}</span>'
            f'</div>'
        )
    if tmx:
        sessions = tmx.get("sessions", [])
        tmx_label = f'{len(sessions)} session(s)' if sessions else "aucune"
        dev_rows += (
            f'<div class="model-row">'
            f'<span>tmux</span>'
            f'<span class="badge">{tmx_label}</span>'
            f'</div>'
        )
        for s in sessions:
            att = " · attached" if s.get("attached") else ""
            dev_rows += (
                f'<div class="sub-num" style="margin-top:2px">'
                f'• {s["name"]} · {s["windows"]} fenêtre(s){att}'
                f'</div>'
            )
    st.markdown(
        f'<div class="card"><div class="card-header">🛠️ DevTools</div>{dev_rows}</div>',
        unsafe_allow_html=True,
    )
```

---

### Note sudo wg

Si `wg show` échoue sans sudo, ajouter dans `/etc/sudoers.d/monitoring` :
```
<username> ALL=(ALL) NOPASSWD: /usr/bin/wg
```
Le script tente d'abord sans sudo, puis avec — les deux cas sont couverts.

---

### Vérification après implémentation

```bash
# 1. Tester le sidecar
python3 daily-health-check.py
cat data/host-health.json | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(json.dumps({k: d.get(k) for k in ['wireguard','github_cli','tmux']}, indent=2))
"

# 2. Rebuild et vérifier les nouvelles cards
docker compose up -d --build
# Ouvrir http://localhost:8888 → System Health tab
```

---

## Structure JSON du sidecar `./data/host-health.json` (complète)

```json
{
  "meta": {
    "collected_at": "2026-04-05T06:25:00+00:00",
    "hostname": "vps.domain.com",
    "uptime": "up 2 days, 3 hours",
    "kernel": "6.1.0-28-amd64",
    "global_status": "ok"
  },
  "resources": {
    "load_1m": 0.12, "load_5m": 0.08, "load_15m": 0.06,
    "ram_total_mb": 15986, "ram_used_mb": 2634, "ram_free_mb": 9523, "ram_pct": 16,
    "disk_total": "50G", "disk_used": "12G", "disk_avail": "38G", "disk_pct": "24%"
  },
  "docker": {
    "containers": [{"name": "...", "image": "...", "state": "running", "status": "Up 2h", "ports": ""}],
    "total": 3, "running": 2, "stopped": 1
  },
  "watchtower": {
    "raw_last50": ["..."],
    "updates": ["Updated nginx:latest → sha256:..."],
    "errors": []
  },
  "apt": {
    "recent_lines": ["2026-04-04 06:25:21 install curl ..."],
    "install_count": 5, "upgrade_count": 3,
    "upgradable": ["curl/stable 8.14.1 amd64 [upgradable from: 8.14.0]"],
    "upgradable_count": 2
  },
  "openclaw_doctor": {"output": ["✅ All clear"], "exit_code": 0, "status": "ok"},
  "openclaw_security": {"output": ["✅ All clear"], "issues": [], "exit_code": 0, "status": "ok"},
  "services": {
    "docker": "active", "caddy": "inactive", "nginx": "active",
    "ssh": "active", "ufw": "active", "fail2ban": "active"
  },
  "wireguard": {
    "interfaces": [
      {
        "name": "wg0", "port": "51820",
        "peers_total": 3, "peers_connected": 3,
        "peers": [
          {"pubkey_short": "abc12345…", "endpoint": "1.2.3.4:51820",
           "allowed_ips": "10.0.0.2/32", "handshake": "2min ago",
           "rx_mb": 280.5, "tx_mb": 45.2, "connected": true}
        ]
      },
      {
        "name": "wg-mikrotik", "port": "51821",
        "peers_total": 2, "peers_connected": 2,
        "peers": []
      }
    ],
    "total_peers": 5, "connected_peers": 5, "status": "ok"
  },
  "github_cli": {
    "authenticated": true, "account": "MyrkoF",
    "token_source": "GITHUB_TOKEN", "status": "ok"
  },
  "tmux": {
    "sessions": [{"name": "monitoring", "windows": 1, "attached": false}],
    "count": 1, "status": "ok"
  }
}
```

---

## Déploiement sur le VPS

```bash
# Tirer main
git checkout main && git pull origin main

# Lancer (Option A — OpenClaw)
./start.sh

# Cron job (si pas encore configuré)
crontab -e
# */10 * * * * cd ~/openclaw-monitoring-cost && python3 daily-health-check.py >/dev/null 2>&1
```

---

## Variables d'environnement

| Variable | Usage |
|---|---|
| `OPENAI_API_KEY_MONITORING` | Admin key OpenAI (scope api.usage.read) |
| `OPENROUTER_API_KEY_MONITORING` | Clé API OpenRouter |
| `ANTHROPIC_API_KEY_MONITORING` | Clé API Anthropic (logs) |
| `ANTHROPIC_CONSOLE_API_KEY` | Optionnel — crédits restants Anthropic |
| `GOOGLE_API_KEY` | Optionnel — Gemini direct |
| `WATCHTOWER_API_TOKEN` | Optionnel — HTTP API Watchtower |
| `WATCHTOWER_API_URL` | Défaut: `http://host.docker.internal:8080` |
| `GOOGLE_SA_KEY_PATH` | Optionnel — billing GCP réel |

---

## Notes techniques importantes

- `python:3.12-slim` n'a pas `procps` → lire `/proc` directement
- `df -h /` dans le container reflète les métriques HOST
- Sidecar considéré "stale" après 4h (`_is_stale()` dans health_collector.py)
- Compat ancienne/nouvelle structure sidecar dans health_collector.py
- F-strings HTML dans app.py = une seule ligne (pas de lignes vides avant `</div>`) — bug Streamlit
