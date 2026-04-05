# openclaw-monitoring-cost

Dashboard local de monitoring des coûts et usage des fournisseurs AI.  
Tourne sur Docker, accessible uniquement via VPN (usage personnel).

---

## Fournisseurs supportés

| Fournisseur | Données | Méthode |
|---|---|---|
| **OpenRouter** | Crédits restants, usage total | API `/credits` |
| **OpenAI** | Usage 30j, détail par modèle | Admin API `/organization/costs` + `/usage/completions` |
| **Anthropic** | Coût estimé par modèle | Logs OpenClaw (pas d'API billing publique) |
| **Google Gemini** | Coût estimé | Logs OpenClaw (pas d'endpoint usage avec clé API simple) |

---

## Stack

- **Python 3.12** + **Streamlit** — dashboard web
- **httpx** — appels API REST
- **Docker + docker-compose** — déploiement

---

## Installation

### 1. Prérequis

- Docker + docker-compose
- Python 3.10+

### 2. Configuration

```bash
cp .env.example .env
# Éditer .env avec vos clés API
```

### 3. Lancer

```bash
docker compose up -d --build
```

Dashboard disponible sur `http://localhost:8888`

---

## Variables d'environnement

Voir `.env.example` pour la liste complète et les instructions de création de clés.

| Variable | Fournisseur | Type de clé requis |
|---|---|---|
| `OPENAI_API_KEY_MONITORING` | OpenAI | Admin key |
| `OPENROUTER_API_KEY_MONITORING` | OpenRouter | Standard |
| `ANTHROPIC_API_KEY_MONITORING` | Anthropic | Standard (optionnel) |
| `GOOGLE_API_KEY` | Google Gemini | API key (optionnel) |

---

## Structure

```
├── app/
│   ├── app.py              # Dashboard Streamlit
│   ├── collectors.py       # Collecte API par fournisseur
│   ├── health_collector.py # Métriques système Linux
│   └── requirements.txt
├── data/                   # SQLite + health cache (gitignored)
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## Volumes Docker

| Volume local | Volume container | Usage |
|---|---|---|
| `./data` | `/data` | Base SQLite + health cache |
| `~/.openclaw/logs` | `/openclaw-logs` | Logs OpenClaw (lecture seule) |
| `~/.openclaw/agents` | `/openclaw-sessions` | Sessions OpenClaw (lecture seule) |

---

## Features

- 📊 Cards par fournisseur avec crédits restants et usage
- 📅 Sélecteur de période : 1j / 7j / 30j
- 🖥️ Onglet System Health (uptime, RAM, disque, Docker, Watchtower) — refresh auto 5 min
- 🔄 Cache persistant — les données restent affichées entre les refreshs
