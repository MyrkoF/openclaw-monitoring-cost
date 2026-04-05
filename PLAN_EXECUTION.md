# PLAN_EXECUTION.md — Handoff for Claude Code CLI

> Ce fichier sert de contexte de reprise pour une nouvelle session Claude Code CLI.
> Lis-le entièrement avant de toucher quoi que ce soit.

---

## Ce qu'est ce projet

Dashboard Streamlit de monitoring des coûts IA + santé VPS.
- Tourne en Docker sur le VPS host (port 8888, VPN only)
- Lit les logs OpenClaw pour estimer les coûts par modèle (OpenRouter, OpenAI, Anthropic, Google)
- Affiche les métriques système via `/proc` (pas de `procps` dans le container)
- Les données host (Docker, Watchtower, APT, OpenClaw doctor/security) viennent d'un sidecar JSON

## Architecture sidecar (push-from-host)

```
HOST (cron */10min)
  └─ daily-health-check.py
       └─ écrit → ./data/host-health.json

DOCKER CONTAINER
  └─ app/health_collector.py
       └─ lit ← /data/host-health.json (via volume ./data:/data)
```

**Pas de docker.sock monté** — sécurité pour partage public.
**Pas d'API OpenClaw pour doctor/audit** — l'API gateway (localhost:18789) n'expose pas ces endpoints.

---

## État du travail (branche `claude/add-watchtower-logs-FPWFc`)

### Fichiers modifiés / réécrits

| Fichier | État | Description |
|---|---|---|
| `docker-compose.yml` | ✅ Terminé | `${HOME}` paths, sans docker.sock, `~/.claude` + `~/google-sa-key.json` en opt-in commenté |
| `start.sh` | ✅ Terminé | `${HOME}` au lieu de `/home/myrko/` |
| `.env.example` | ✅ Terminé | Chemins universels, sans OPENCLAW_API_URL |
| `README.md` | ✅ Terminé | Chemins `~/`, sans docker.sock, opt-in labelling |
| `daily-health-check.py` | ✅ Terminé | Réécriture complète — collecte exhaustive + JSON structuré |
| `app/health_collector.py` | ✅ Terminé | Sans docker.sock, lit nouvelle structure sidecar + compat ancienne |
| `app/app.py` | ✅ Terminé | Services card, Docker counters, APT upgradable, Watchtower errors, global status |

### Ce qui est FAIT dans app.py (System Health tab)

- Caption : badge global status (✅/⚠️/❌) + "sidecar OK/⚠️ expiré (timestamp)"
- Docker card : header avec compteurs `N▲ M▼ / T total`
- APT card : badge `X upgradable` (rouge si > 10)
- Watchtower card : badge source (API/sidecar) + badge erreurs rouge si présent
- Services card (⚙️) : nouvel affichage systemd par service (active=vert, autre=rouge)
- Doctor/Audit : expanders — placeholder masqué si daily-health-check non encore lancé

---

## Structure JSON du sidecar `./data/host-health.json`

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
  "openclaw_doctor": {
    "output": ["✅ All clear"], "exit_code": 0, "status": "ok"
  },
  "openclaw_security": {
    "output": ["✅ All clear"], "issues": [], "exit_code": 0, "status": "ok"
  },
  "services": {
    "docker": "active", "caddy": "inactive", "nginx": "active",
    "ssh": "active", "ufw": "active", "fail2ban": "active"
  }
}
```

---

## Déploiement sur le VPS

```bash
# 1. Tirer la branche
git fetch origin
git checkout claude/add-watchtower-logs-FPWFc

# 2. Configurer les variables
cp .env.example .env
# Éditer .env avec les vraies clés API

# 3. Lancer le dashboard
./start.sh
# ou : docker compose up -d --build

# 4. Configurer le cron job (sur le HOST, pas dans Docker)
crontab -e
# Ajouter : */10 * * * * cd ~/openclaw-monitoring-cost && python3 daily-health-check.py >/dev/null 2>&1

# 5. Tester le sidecar manuellement
python3 daily-health-check.py
cat data/host-health.json | python3 -m json.tool
```

---

## Variables d'environnement importantes

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

## Tâches restantes potentielles

- [ ] Merge de la branche `claude/add-watchtower-logs-FPWFc` vers `main` (quand testé sur le VPS)
- [ ] Tester le sidecar sur le VPS réel (`python3 daily-health-check.py`)
- [ ] Vérifier que toutes les sections s'affichent correctement dans le dashboard

---

## Notes techniques importantes

- `python:3.12-slim` n'a pas `procps` → `uptime`, `free` échouent silencieusement → on lit `/proc` directement
- `df -h /` fonctionne dans le container et reflète les métriques HOST (pas du container)
- Le sidecar est considéré "stale" après 4h (`_is_stale()` dans health_collector.py)
- Compatibilité ancienne/nouvelle structure sidecar maintenue dans health_collector.py
- Les f-strings HTML dans app.py doivent être sur une seule ligne (pas de lignes vides avant `</div>`) — bug Streamlit markdown parser
