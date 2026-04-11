# openclaw-monitoring-cost

Dashboard local de monitoring des coûts et usage des fournisseurs AI + santé du VPS.  
Tourne sur Docker, accessible uniquement via VPN (usage personnel).

---

## Prérequis sur le VPS host

| Prérequis | Requis | Usage |
|---|---|---|
| **Docker + docker-compose** | ✅ Obligatoire | Lancer le container |
| **Python 3** | ✅ Obligatoire | Lancer `daily-health-check.py` sur le host |
| **OpenClaw installé** (`openclaw`) | ✅ Obligatoire | Logs de sessions pour coûts Anthropic/Google |
| **Dossier `~/.openclaw/logs`** | ✅ Obligatoire | Monté en lecture seule — logs d'usage |
| **Dossier `~/.openclaw/agents`** | ✅ Obligatoire | Monté en lecture seule — sessions tokens |
| **Dossier `~/.openclaw/cron/runs`** | ✅ Obligatoire | Monté en lecture seule — usage Claude API |
| **blogwatcher** | Optionnel | Comparaison version OpenClaw installée vs dernière release |
| **Claude Code CLI** (`~/.claude/`) | Optionnel | Billing Anthropic via token CLI — opt-in |
| **Service account GCP** JSON key | Optionnel | Billing Google Cloud réel — opt-in |
| **Token Watchtower HTTP API** | Optionnel | Métriques Watchtower via API (fallback : sidecar) |

---

## Fournisseurs supportés

| Fournisseur | Données | Méthode |
|---|---|---|
| **OpenRouter** | Crédits restants, usage total, coût par modèle | API `/credits` + logs OpenClaw |
| **OpenAI** | Usage par modèle, coût estimé | Admin API `/organization/costs` + `/usage/completions` |
| **Anthropic** | Coût estimé par modèle + crédits restants (optionnel) | Logs OpenClaw + Console API ou CLI token |
| **Google Gemini** | Coût estimé par modèle + coût réel GCP (optionnel) | Logs OpenClaw + Cloud Billing API |

> **Note OpenAI** : L'API OpenAI ne permet pas d'accéder au solde prépayé via clé API serveur (nécessite une session navigateur). Seul l'usage par modèle est affiché.

---

## Stack

- **Python 3.12** + **Streamlit** — dashboard web
- **httpx** — appels API REST
- **plotly** — graphiques temps réel (CPU, RAM, Network)
- **psutil** — métriques live dans le container
- **cryptography** — signature JWT pour service account Google
- **Docker + docker-compose** — déploiement

---

## Architecture

```
HOST (cron */10 min)
  └─ daily-health-check.py
       └─ écrit → ./data/host-health.json

DOCKER CONTAINER
  └─ app/health_collector.py
       └─ lit ← /data/host-health.json  (volume partagé ./data:/data)
       └─ lit ← /proc/*                 (métriques CPU/RAM/disque natives)
       └─ appelle API providers         (OpenRouter, OpenAI, Anthropic, Google)
```

Le container n'a **pas** accès au Docker socket — toutes les données host passent par le fichier sidecar JSON.

---

## Installation

### 1. Cloner le dépôt

```bash
git clone <repo>
cd openclaw-monitoring-cost
```

### 2. Configurer les clés API

**Option A — Avec OpenClaw (recommandé sur ce VPS)**

Les clés sont lues automatiquement depuis `~/.openclaw/openclaw.json` :

```bash
./start.sh    # Injecte les clés OpenClaw + lance docker compose
```

Variables extraites depuis OpenClaw :

| Variable OpenClaw | Variable injectée |
|---|---|
| `OPENAI_API_KEY_MONITORING` | `OPENAI_API_KEY` |
| `OPENROUTER_API_KEY_MONITORING` | `OPENROUTER_API_KEY` |
| `ANTHROPIC_API_KEY_MONITORING` | `ANTHROPIC_API_KEY` |

**Option B — Sans OpenClaw (déploiement universel)**

```bash
cp .env.example .env
# Éditer .env avec vos clés API
docker compose up -d --build
```

Voir `.env.example` pour la liste complète et les instructions de création de clés.

### 3. (Optionnel) Billing Google Cloud

Si tu veux le coût GCP réel (pas seulement l'estimation logs) :

1. Aller sur [GCP Console](https://console.cloud.google.com) > IAM → Service Accounts
2. Sélectionner ton service account lié à l'API Gemini
3. Onglet **Clés** → **Ajouter une clé** → JSON → télécharger
4. Copier le fichier sur le host : `cp key.json ~/google-sa-key.json`
5. S'assurer que le compte a le rôle `Billing Account Viewer`
6. Décommenter la ligne de volume dans `docker-compose.yml` :
   ```yaml
   - ~/google-sa-key.json:/google-sa-key.json:ro
   ```

### 4. (Optionnel) Billing Anthropic Console

Si tu as des crédits prépayés Anthropic :

- **Option A** — Clé Console dédiée :
  Créer sur [console.anthropic.com](https://console.anthropic.com) une clé avec accès billing.
  Ajouter dans `.env` : `ANTHROPIC_CONSOLE_API_KEY=sk-ant-...`

- **Option B** — Token Claude Code CLI :
  Décommenter la ligne de volume dans `docker-compose.yml` :
  ```yaml
  - ${HOME}/.claude:/claude-home:ro
  ```
  Le dashboard lira alors le token d'authentification CLI automatiquement.

### 5. Configurer le cron job `daily-health-check.py`

Ce script tourne sur le **host** (pas dans Docker) et alimente les sections **Docker**, **Watchtower**, **APT**, **OpenClaw Doctor**, **Security Audit**, **WireGuard**, **Fail2ban**, **UFW** et **Services** du dashboard.

```bash
# Tester manuellement (depuis le répertoire du projet)
cd ~/openclaw-monitoring-cost
python3 daily-health-check.py

# Vérifier le résultat
cat data/host-health.json | python3 -m json.tool

# Ajouter en cron (toutes les 10 minutes)
crontab -e
# Ajouter la ligne :
# */10 * * * * cd ~/openclaw-monitoring-cost && python3 daily-health-check.py >/dev/null 2>&1
```

Le script écrit `data/host-health.json` lu par le container via le volume partagé `./data:/data`.

### 6. Lancer

```bash
# Via start.sh (Option A — injecte les clés depuis openclaw.json)
./start.sh

# Ou directement (Option B — nécessite un fichier .env)
docker compose up -d --build
```

Dashboard disponible sur `http://localhost:8888`

---

## Variables d'environnement

Voir `.env.example` pour la liste complète.

| Variable | Requis | Description |
|---|---|---|
| `OPENAI_API_KEY_MONITORING` | Oui | Admin key OpenAI (scope `api.usage.read`) |
| `OPENROUTER_API_KEY_MONITORING` | Oui | Clé API OpenRouter |
| `ANTHROPIC_API_KEY_MONITORING` | Oui | Clé API Anthropic (lecture logs) |
| `GOOGLE_API_KEY` | Optionnel | Clé API Gemini direct |
| `ANTHROPIC_CONSOLE_API_KEY` | Optionnel | Clé Console billing Anthropic |
| `WATCHTOWER_API_URL` | Optionnel | URL API Watchtower (défaut : `http://host.docker.internal:8080`) |
| `WATCHTOWER_API_TOKEN` | Optionnel | Token API Watchtower (vide = fallback sidecar) |
| `GOOGLE_SA_KEY_PATH` | Optionnel | Chemin JSON service account GCP |
| `BLOGWATCHER_BIN` | Optionnel | Chemin vers le binaire blogwatcher (défaut : `~/go/bin/blogwatcher`) |

---

## Volumes Docker

| Volume local | Volume container | Usage |
|---|---|---|
| `./data` | `/data` | SQLite + health cache + sidecar hôte |
| `~/.openclaw/logs` | `/openclaw-logs` | Logs OpenClaw (lecture seule) |
| `~/.openclaw/agents` | `/openclaw-sessions` | Sessions OpenClaw (lecture seule) |
| `~/.openclaw/cron/runs` | `/openclaw-cron` | Cron runs OpenClaw — usage Claude API (lecture seule) |
| `~/.claude` | `/claude-home` | Claude Code CLI config — **opt-in**, décommenter dans `docker-compose.yml` |
| `~/google-sa-key.json` | `/google-sa-key.json` | Service account GCP — **opt-in**, décommenter dans `docker-compose.yml` |

---

## Structure

```
├── app/
│   ├── app.py              # Dashboard Streamlit
│   ├── collectors.py       # Collecte API par fournisseur
│   ├── health_collector.py # Métriques système Linux (via /proc + sidecar)
│   └── requirements.txt
├── daily-health-check.py   # Cron job host — collecte exhaustive → data/host-health.json
├── data/                   # SQLite + health cache (gitignored)
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## Features

### AI Costs (Tab 1)
- 📊 Cards par fournisseur avec crédits restants/consommés et usage
- 💰 Coût USD par modèle sur OpenRouter, OpenAI, Anthropic, Google
- 🏦 Double vue Anthropic : billing API (si configuré) + estimation logs
- 🤖 Claude Code : stats locales (sessions, messages, tokens par modèle)
- 📅 Sélecteur de période : 1j / 7j / 30j

### System Health (Tab 2)
- 📈 Graphiques live : CPU & RAM %, Network I/O (10s refresh via SQLite)
- 🖥️ Système : uptime, CPU%, RAM, disques multiples, réseau (via `/proc`)
- 🐳 Docker : containers + stats live top 5 CPU
- 🔄 Watchtower : sessions + **noms des images mises à jour**
- 🛡️ Fail2ban : jails actives, IPs bannies
- 🔥 UFW : blocks/heure, top IPs bloquées, détails blocks, auth failures
- 🔒 WireGuard : interfaces, peers connectés, handshakes, trafic
- ⚙️ Services systemd : statuts en temps réel
- 🛠️ DevTools : GitHub CLI auth, tmux sessions
- 📦 APT : packages mis à jour + compteur upgradable + timer auto-upgrade

### OpenClaw (Tab 2)
- 🦞 **Version** : installée vs dernière release (badge vert/rouge via blogwatcher)
- 🩺 **Doctor structuré** : Matrix status, agents, heartbeat, sessions store, plugin errors, skills blocked, memory plugin
- 🛡️ **Security structuré** : summary (critical/warn/info), warnings avec fix, attack surface
- 📄 Expanders "Détails bruts" pour debug

### Général
- 🌡️ Global status badge : ✅/⚠️/❌ agrégé depuis toutes les sections
- 🔄 Auto-refresh configurable (10s / 30s / 60s)
- 🔔 Alertes CPU>80% / Disk<20% (opt-in)
- 🔄 Cache persistant — données affichées même entre les refreshs
- 🧵 Threads arrière-plan — métriques système (5 min), Webmin (30s), live collector (10s)
