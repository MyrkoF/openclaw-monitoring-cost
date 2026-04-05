# openclaw-monitoring-cost

Dashboard local de monitoring des coûts et usage des fournisseurs AI + santé du VPS.  
Tourne sur Docker, accessible uniquement via VPN (usage personnel).

---

## Prérequis sur le VPS host

| Prérequis | Requis | Usage |
|---|---|---|
| **Docker + docker-compose** | ✅ Obligatoire | Lancer le container |
| **OpenClaw installé** (`openclaw`) | ✅ Obligatoire | Logs de sessions pour coûts Anthropic/Google |
| **Dossier `~/.openclaw/logs`** | ✅ Obligatoire | Monté en lecture seule — logs d'usage |
| **Dossier `~/.openclaw/agents`** | ✅ Obligatoire | Monté en lecture seule — sessions tokens |
| **Docker socket** `/var/run/docker.sock` | ✅ Obligatoire | Containers list + Watchtower logs |
| **Claude Code CLI** (`~/.claude/`) | Optionnel | Billing Anthropic via token CLI |
| **Service account GCP** JSON key | Optionnel | Billing Google Cloud réel |
| **Token Watchtower HTTP API** | Optionnel | Logs Watchtower via API (fallback: docker logs) |

---

## Fournisseurs supportés

| Fournisseur | Données | Méthode |
|---|---|---|
| **OpenRouter** | Crédits restants, usage total, coût par modèle | API `/credits` + logs OpenClaw |
| **OpenAI** | Usage 30j, crédits prépayés, coût estimé par modèle | Admin API `/organization/costs` + `/usage/completions` |
| **Anthropic** | Coût estimé par modèle + crédits restants (optionnel) | Logs OpenClaw + Console API ou CLI token |
| **Google Gemini** | Coût estimé par modèle + coût réel GCP (optionnel) | Logs OpenClaw + Cloud Billing API |

---

## Stack

- **Python 3.12** + **Streamlit** — dashboard web
- **httpx** — appels API REST
- **cryptography** — signature JWT pour service account Google
- **Docker + docker-compose** — déploiement

---

## Installation

### 1. Cloner le dépôt

```bash
git clone <repo>
cd openclaw-monitoring-cost
```

### 2. Configurer les clés API

```bash
cp .env.example .env
# Éditer .env avec vos clés API
```

Voir `.env.example` pour la liste complète et les instructions de création de clés.

### 3. (Optionnel) Billing Google Cloud

Si tu veux le coût GCP réel (pas seulement l'estimation logs) :

1. Aller sur [GCP Console](https://console.cloud.google.com) > IAM → Service Accounts
2. Sélectionner `vertex-express@gen-lang-client-...`
3. Onglet **Clés** → **Ajouter une clé** → JSON → télécharger
4. Copier le fichier sur le host : `cp key.json ~/google-sa-key.json`
5. S'assurer que le compte a le rôle `Billing Account Viewer`
6. Décommenter la ligne de volume dans `docker-compose.yml` :
   ```yaml
   - /home/myrko/google-sa-key.json:/google-sa-key.json:ro
   ```

### 4. (Optionnel) Billing Anthropic Console

Si tu as des crédits prépayés Anthropic :

- **Option A** — Clé Console dédiée :
  Créer sur [console.anthropic.com](https://console.anthropic.com) une clé avec accès billing.
  Ajouter dans `.env` : `ANTHROPIC_CONSOLE_API_KEY=sk-ant-...`

- **Option B** — Token Claude Code CLI :
  Le container monte `~/.claude/` en lecture seule automatiquement.
  Le dashboard tente de lire le token d'authentification CLI.

### 5. (Optionnel) Cron job daily-health-check.py

Pour alimenter les sections **OpenClaw Doctor** et **Security Audit** du dashboard :

```bash
# Tester manuellement
python3 daily-health-check.py

# Ajouter en cron (exemple : tous les jours à 6h)
crontab -e
# 0 6 * * * cd /home/myrko/openclaw-monitoring-cost && python3 daily-health-check.py > /dev/null 2>&1
```

Le script écrit automatiquement un fichier `data/daily-health.json` lu par le dashboard.

### 6. Lancer

```bash
# Via start.sh (injecte les clés depuis openclaw.json)
./start.sh

# Ou directement
docker compose up -d --build
```

Dashboard disponible sur `http://localhost:8888`

---

## Variables d'environnement

Voir `.env.example` pour la liste complète.

| Variable | Requis | Description |
|---|---|---|
| `OPENAI_API_KEY_MONITORING` | Oui | Admin key OpenAI |
| `OPENROUTER_API_KEY_MONITORING` | Oui | Clé API OpenRouter |
| `ANTHROPIC_API_KEY_MONITORING` | Oui | Clé API Anthropic (logs) |
| `GOOGLE_API_KEY` | Optionnel | Clé API Gemini |
| `ANTHROPIC_CONSOLE_API_KEY` | Optionnel | Clé Console billing Anthropic |
| `WATCHTOWER_API_URL` | Optionnel | URL API Watchtower (défaut: host.docker.internal:8080) |
| `WATCHTOWER_API_TOKEN` | Optionnel | Token API Watchtower (vide = docker logs) |
| `GOOGLE_SA_KEY_PATH` | Optionnel | Chemin JSON service account GCP |

---

## Volumes Docker

| Volume local | Volume container | Usage |
|---|---|---|
| `./data` | `/data` | SQLite + health cache + daily-health sidecar |
| `~/.openclaw/logs` | `/openclaw-logs` | Logs OpenClaw (lecture seule) |
| `~/.openclaw/agents` | `/openclaw-sessions` | Sessions OpenClaw (lecture seule) |
| `/var/run/docker.sock` | `/var/run/docker.sock` | Docker daemon (lecture seule) |
| `~/.claude` | `/claude-home` | Claude Code CLI config (lecture seule) |

---

## Structure

```
├── app/
│   ├── app.py              # Dashboard Streamlit
│   ├── collectors.py       # Collecte API par fournisseur
│   ├── health_collector.py # Métriques système Linux (via /proc)
│   └── requirements.txt
├── daily-health-check.py   # Cron job host — doctor + audit → JSON sidecar
├── data/                   # SQLite + health cache (gitignored)
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## Features

- 📊 Cards par fournisseur avec crédits restants/consommés et usage
- 💰 Coût USD par modèle sur OpenRouter, OpenAI, Anthropic, Google
- 🏦 Double vue Anthropic : billing API (si configuré) + estimation logs
- 🏦 OpenAI : crédits prépayés (si compte prépayé) ou mode postpayé
- 📅 Sélecteur de période : 1j / 7j / 30j
- 🖥️ System Health : uptime, CPU%, RAM, disque, Docker containers, Watchtower
- 🔒 OpenClaw Doctor + Security Audit (depuis cron job daily-health-check.py)
- 🔄 Cache persistant — données affichées même entre les refreshs
- 🧵 Thread arrière-plan — métriques système rafraîchies toutes les 5 min
