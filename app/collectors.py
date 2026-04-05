#!/usr/bin/env python3
"""
collectors.py — Collecte usage/coûts depuis les fournisseurs AI.
Chaque collector retourne un dict standardisé.
"""

import os
import json
import glob
import sqlite3
import httpx
from datetime import datetime, timedelta, timezone, date

DB_PATH = os.environ.get("DB_PATH", "/data/monitoring.db")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
OPENCLAW_LOGS_DIR = os.environ.get("OPENCLAW_LOGS_DIR", "/openclaw-logs")
OPENCLAW_SESSIONS_DIR = os.environ.get("OPENCLAW_SESSIONS_DIR", "/openclaw-sessions")

# Pricing (USD / 1M tokens)
ANTHROPIC_PRICING = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5":  {"input": 0.80, "output": 4.00},
    "claude-opus-4":     {"input": 15.0, "output": 75.00},
    "default":           {"input": 3.00, "output": 15.00},
}
GOOGLE_PRICING = {
    "gemini-2.0-flash":  {"input": 0.075, "output": 0.30},
    "gemini-1.5-pro":    {"input": 1.25,  "output": 5.00},
    "gemini-1.5-flash":  {"input": 0.075, "output": 0.30},
    "default":           {"input": 0.075, "output": 0.30},
}


def _get_price(pricing_table, model):
    for k, v in pricing_table.items():
        if k != "default" and k in model:
            return v
    return pricing_table["default"]


# ─── OpenRouter ────────────────────────────────────────────────────────────────

def collect_openrouter():
    if not OPENROUTER_API_KEY:
        return {"provider": "openrouter", "status": "no_key", "data": None}
    try:
        r = httpx.get(
            "https://openrouter.ai/api/v1/credits",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            timeout=10,
        )
        r.raise_for_status()
        raw = r.json().get("data", {})
        total = raw.get("total_credits", 0) or 0
        used = raw.get("total_usage", 0) or 0
        return {
            "provider": "openrouter",
            "status": "ok",
            "collected_at": datetime.utcnow().isoformat(),
            "total_credits_usd": total,
            "total_usage_usd": used,
            "remaining_usd": round(total - used, 4),
            "rate_limit": raw.get("rate_limit", {}),
        }
    except Exception as e:
        return {"provider": "openrouter", "status": "error", "error": str(e)}


# ─── OpenAI ────────────────────────────────────────────────────────────────────

def collect_openai():
    if not OPENAI_API_KEY:
        return {"provider": "openai", "status": "no_key", "data": None}
    hdrs = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    try:
        start_time = int((datetime.utcnow() - timedelta(days=30)).timestamp())

        # 1. Coûts journaliers
        r_costs = httpx.get(
            "https://api.openai.com/v1/organization/costs",
            headers=hdrs,
            params={"start_time": start_time, "bucket_width": "1d"},
            timeout=15,
        )
        costs_data = r_costs.json()

        daily = []
        total = 0.0
        org_name = ""
        for bucket in costs_data.get("data", []):
            day_cost = sum(
                float(res.get("amount", {}).get("value", 0))
                for res in bucket.get("results", [])
            )
            if not org_name and bucket.get("results"):
                org_name = bucket["results"][0].get("organization_name", "")
            if day_cost > 0:
                daily.append({
                    "date": bucket.get("start_time_iso", "")[:10],
                    "cost_usd": round(day_cost, 4),
                })
                total += day_cost

        # 2. Usage par modèle — group_by[]=model requis
        r_usage = httpx.get(
            "https://api.openai.com/v1/organization/usage/completions",
            headers=hdrs,
            params={"start_time": start_time, "limit": 31, "group_by[]": "model"},
            timeout=15,
        )
        usage_data = r_usage.json()

        by_model = {}
        for bucket in usage_data.get("data", []):
            for res in bucket.get("results", []):
                m = res.get("model") or "unknown"
                if m not in by_model:
                    by_model[m] = {"input_tokens": 0, "output_tokens": 0, "requests": 0}
                by_model[m]["input_tokens"] += res.get("input_tokens", 0) or 0
                by_model[m]["output_tokens"] += res.get("output_tokens", 0) or 0
                by_model[m]["requests"] += res.get("num_model_requests", 0) or 0

        return {
            "provider": "openai",
            "status": "ok",
            "collected_at": datetime.utcnow().isoformat(),
            "org": org_name,
            "total_usage_usd_30d": round(total, 4),
            "daily": daily,
            "by_model": by_model,
        }
    except Exception as e:
        return {"provider": "openai", "status": "error", "error": str(e)}




# ─── Anthropic (depuis logs OpenClaw) ──────────────────────────────────────────

def _parse_session_files(provider_filter_fn, days=30):
    """Parse OpenClaw session JSONL files and extract usage entries."""
    entries = []
    since = datetime.utcnow() - timedelta(days=days)

    # Sessions de tous les agents
    patterns = [
        f"{OPENCLAW_SESSIONS_DIR}/**/*.jsonl",
        f"{OPENCLAW_LOGS_DIR}/**/*.jsonl",
    ]
    seen_files = set()
    for pattern in patterns:
        for lf in glob.glob(pattern, recursive=True):
            if lf in seen_files:
                continue
            seen_files.add(lf)
            try:
                with open(lf) as f:
                    for line in f:
                        try:
                            entry = json.loads(line)
                            # Format OpenClaw : message.model + message.usage
                            msg = entry.get("message", {})
                            model = str(msg.get("model") or entry.get("model", ""))
                            provider = str(msg.get("provider") or entry.get("provider", ""))
                            if not provider_filter_fn(model, provider):
                                continue
                            usage = msg.get("usage") or entry.get("usage") or {}
                            if not usage:
                                continue
                            # Format OpenClaw : usage.input / usage.output / usage.cost.total
                            inp = usage.get("input") or usage.get("input_tokens") or usage.get("promptTokenCount") or 0
                            out = usage.get("output") or usage.get("output_tokens") or usage.get("candidatesTokenCount") or 0
                            cost = (usage.get("cost") or {}).get("total") or 0
                            if not (inp or out):
                                continue
                            ts = entry.get("timestamp", "")
                            entries.append({
                                "model": model,
                                "input_tokens": int(inp),
                                "output_tokens": int(out),
                                "cost_usd": float(cost),
                                "timestamp": ts,
                            })
                        except Exception:
                            pass
            except Exception:
                pass
    return entries


def collect_anthropic(days=30):
    entries = _parse_session_files(
        lambda m, prov: ("anthropic" in m or "claude" in m) and "openrouter" not in prov,
        days=days,
    )

    by_model = {}
    for e in entries:
        m = e["model"]
        if m not in by_model:
            by_model[m] = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        by_model[m]["input_tokens"] += e["input_tokens"]
        by_model[m]["output_tokens"] += e["output_tokens"]
        if e["cost_usd"]:
            # Coût direct depuis les logs OpenClaw (plus précis)
            by_model[m]["cost_usd"] = round(by_model[m]["cost_usd"] + e["cost_usd"], 6)
        else:
            # Fallback : calcul depuis la grille de pricing
            p = _get_price(ANTHROPIC_PRICING, m)
            by_model[m]["cost_usd"] = round(
                by_model[m]["cost_usd"]
                + e["input_tokens"] / 1_000_000 * p["input"]
                + e["output_tokens"] / 1_000_000 * p["output"],
                6,
            )

    total_cost = round(sum(v["cost_usd"] for v in by_model.values()), 6)
    total_input = sum(v["input_tokens"] for v in by_model.values())
    total_output = sum(v["output_tokens"] for v in by_model.values())

    return {
        "provider": "anthropic",
        "status": "ok" if entries else "no_logs",
        "source": "openclaw_sessions",
        "collected_at": datetime.utcnow().isoformat(),
        "period_days": days,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "estimated_cost_usd": total_cost,
        "by_model": by_model,
        "note": "Coûts depuis logs OpenClaw (usage.cost.total). Fallback: grille pricing publique.",
    }


# ─── Google Gemini (clé API simple) ────────────────────────────────────────────

def collect_google():
    """
    Google Gemini via clé API simple : pas d'endpoint usage/billing.
    Lecture depuis les sessions OpenClaw.
    ATTENTION : exclure les modèles passés via OpenRouter (prefix 'openrouter/').
    """
    def is_google_direct(model, provider):
        # Exclure tout ce qui passe par OpenRouter
        if "openrouter" in provider:
            return False
        m = model.lower()
        return "gemini" in m or ("google" in m and "gemini" in m)

    entries = _parse_session_files(is_google_direct)

    by_model = {}
    for e in entries:
        m = e["model"]
        if m not in by_model:
            by_model[m] = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        by_model[m]["input_tokens"] += e["input_tokens"]
        by_model[m]["output_tokens"] += e["output_tokens"]
        if e["cost_usd"]:
            by_model[m]["cost_usd"] = round(by_model[m]["cost_usd"] + e["cost_usd"], 6)
        else:
            p = _get_price(GOOGLE_PRICING, m)
            by_model[m]["cost_usd"] = round(
                by_model[m]["cost_usd"]
                + e["input_tokens"] / 1_000_000 * p["input"]
                + e["output_tokens"] / 1_000_000 * p["output"],
                6,
            )

    total_cost = round(sum(v["cost_usd"] for v in by_model.values()), 6)

    return {
        "provider": "google",
        "status": "ok" if entries else "no_logs",
        "source": "openclaw_sessions",
        "collected_at": datetime.utcnow().isoformat(),
        "mode": "gemini_api_key",
        "estimated_cost_usd": total_cost,
        "by_model": by_model,
        "note": "Clé API Gemini simple — pas d'endpoint usage. Coûts depuis logs OpenClaw.",
    }


# ─── Collecte complète ──────────────────────────────────────────────────────────

def collect_all():
    return {
        "collected_at": datetime.utcnow().isoformat(),
        "providers": {
            "openrouter": collect_openrouter(),
            "openai": collect_openai(),
            "anthropic": collect_anthropic(),
            "google": collect_google(),
        }
    }


if __name__ == "__main__":
    result = collect_all()
    print(json.dumps(result, indent=2))
