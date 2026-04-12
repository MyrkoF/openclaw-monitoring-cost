#!/usr/bin/env python3
"""
collectors.py — Collecte usage/coûts depuis les fournisseurs AI.
Chaque collector retourne un dict standardisé.
"""

import os
import json
import glob
import httpx
from datetime import datetime, timedelta

DB_PATH               = os.environ.get("DB_PATH", "/data/monitoring.db")
OPENAI_API_KEY        = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY     = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_CONSOLE_KEY = os.environ.get("ANTHROPIC_CONSOLE_API_KEY", "")
CLAUDE_HOME           = os.environ.get("CLAUDE_HOME", "/claude-home")
OPENROUTER_API_KEY    = os.environ.get("OPENROUTER_API_KEY", "")
GOOGLE_API_KEY        = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_SA_KEY_PATH    = os.environ.get("GOOGLE_SA_KEY_PATH", "/google-sa-key.json")
CHATGPT_OAUTH_TOKEN   = os.environ.get("CHATGPT_OAUTH_TOKEN", "")
OPENCLAW_LOGS_DIR     = os.environ.get("OPENCLAW_LOGS_DIR", "/openclaw-logs")
OPENCLAW_SESSIONS_DIR = os.environ.get("OPENCLAW_SESSIONS_DIR", "/openclaw-sessions")
OPENCLAW_CRON_DIR     = os.environ.get("OPENCLAW_CRON_DIR", "/openclaw-cron")

# Pricing tables (USD / 1M tokens)
ANTHROPIC_PRICING = {
    "claude-sonnet-4-6": {"input": 3.00,  "output": 15.00},
    "claude-haiku-4-5":  {"input": 0.80,  "output": 4.00},
    "claude-opus-4":     {"input": 15.0,  "output": 75.00},
    "default":           {"input": 3.00,  "output": 15.00},
}
GOOGLE_PRICING = {
    "gemini-2.0-flash":  {"input": 0.075, "output": 0.30},
    "gemini-1.5-pro":    {"input": 1.25,  "output": 5.00},
    "gemini-1.5-flash":  {"input": 0.075, "output": 0.30},
    "default":           {"input": 0.075, "output": 0.30},
}
# Pricing for cost estimation when OpenAI doesn't return cost directly
OPENAI_PRICING = {
    "gpt-4o":                 {"input": 2.50,  "output": 10.00},
    "gpt-4o-mini":            {"input": 0.15,  "output": 0.60},
    "gpt-4-turbo":            {"input": 10.00, "output": 30.00},
    "gpt-4":                  {"input": 30.00, "output": 60.00},
    "gpt-3.5-turbo":          {"input": 0.50,  "output": 1.50},
    "gpt-5":                  {"input": 2.50,  "output": 10.00},
    "gpt-5-nano":             {"input": 0.15,  "output": 0.60},
    "gpt-5.3-codex":          {"input": 3.00,  "output": 15.00},
    "gpt-5.1-codex":          {"input": 3.00,  "output": 15.00},
    "gpt-4.1":                {"input": 2.00,  "output": 8.00},
    "gpt-4.1-mini":           {"input": 0.40,  "output": 1.60},
    "default":                {"input": 2.50,  "output": 10.00},
}


def _get_price(pricing_table, model):
    for k, v in pricing_table.items():
        if k != "default" and k in model:
            return v
    return pricing_table["default"]


# ─── OpenClaw session file parser ─────────────────────────────────────────────

def _parse_ts(ts_str):
    """Parse ISO 8601 timestamp to naive UTC datetime."""
    if not ts_str:
        return None
    try:
        ts_str = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_str)
        return dt.replace(tzinfo=None)  # strip tz for comparison with utcnow
    except Exception:
        return None


def _parse_session_files(provider_filter_fn, days=30):
    """Parse OpenClaw session JSONL files and extract usage entries."""
    entries = []
    since = datetime.utcnow() - timedelta(days=days)
    patterns = [
        f"{OPENCLAW_SESSIONS_DIR}/**/*.jsonl",
        f"{OPENCLAW_LOGS_DIR}/**/*.jsonl",
        f"{OPENCLAW_CRON_DIR}/**/*.jsonl",
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
                            ts = entry.get("timestamp", "")
                            ts_dt = _parse_ts(ts)
                            if ts_dt and ts_dt < since:
                                continue
                            msg      = entry.get("message", {})
                            model    = str(msg.get("model") or entry.get("model", ""))
                            provider = str(msg.get("provider") or entry.get("provider", ""))
                            if not provider_filter_fn(model, provider):
                                continue
                            usage = msg.get("usage") or entry.get("usage") or {}
                            if not usage:
                                continue
                            inp  = (usage.get("input") or usage.get("input_tokens")
                                    or usage.get("promptTokenCount") or 0)
                            out  = (usage.get("output") or usage.get("output_tokens")
                                    or usage.get("candidatesTokenCount") or 0)
                            cost = (usage.get("cost") or {}).get("total") or 0
                            if not (inp or out):
                                continue
                            entries.append({
                                "model":         model,
                                "provider":      provider,
                                "input_tokens":  int(inp),
                                "output_tokens": int(out),
                                "cost_usd":      float(cost),
                                "timestamp":     ts,
                            })
                        except Exception:
                            pass
            except Exception:
                pass
    return entries


# ─── OpenRouter ────────────────────────────────────────────────────────────────

def collect_openrouter(days=30):
    if not OPENROUTER_API_KEY:
        return {"provider": "openrouter", "status": "no_key", "data": None}
    try:
        r = httpx.get(
            "https://openrouter.ai/api/v1/credits",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            timeout=10,
        )
        r.raise_for_status()
        raw   = r.json().get("data", {})
        total = raw.get("total_credits", 0) or 0
        used  = raw.get("total_usage", 0) or 0

        # Coût par modèle depuis les logs OpenClaw (provider = openrouter)
        entries = _parse_session_files(
            lambda m, prov: "openrouter" in prov.lower(),
            days=days,
        )
        by_model = {}
        for e in entries:
            m = e["model"]
            if m not in by_model:
                by_model[m] = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
            by_model[m]["input_tokens"]  += e["input_tokens"]
            by_model[m]["output_tokens"] += e["output_tokens"]
            by_model[m]["cost_usd"]       = round(
                by_model[m]["cost_usd"] + e["cost_usd"], 6
            )

        return {
            "provider":          "openrouter",
            "status":            "ok",
            "collected_at":      datetime.utcnow().isoformat(),
            "total_credits_usd": total,
            "total_usage_usd":   used,
            "remaining_usd":     round(total - used, 4),
            "rate_limit":        raw.get("rate_limit", {}),
            "by_model":          by_model,
        }
    except Exception as e:
        return {"provider": "openrouter", "status": "error", "error": str(e)}


# ─── OpenAI ────────────────────────────────────────────────────────────────────

def collect_openai(days=30):
    if not OPENAI_API_KEY:
        return {"provider": "openai", "status": "no_key", "data": None}
    hdrs = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    try:
        start_time = int((datetime.utcnow() - timedelta(days=days)).timestamp())

        # 1. Coûts journaliers
        r_costs = httpx.get(
            "https://api.openai.com/v1/organization/costs",
            headers=hdrs,
            params={"start_time": start_time, "bucket_width": "1d"},
            timeout=15,
        )
        costs_data = r_costs.json()

        daily    = []
        total    = 0.0
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
                    "date":     bucket.get("start_time_iso", "")[:10],
                    "cost_usd": round(day_cost, 4),
                })
                total += day_cost

        # 2. Usage par modèle
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
                    by_model[m] = {"input_tokens": 0, "output_tokens": 0,
                                   "requests": 0, "cost_usd": 0.0}
                inp  = res.get("input_tokens", 0) or 0
                out  = res.get("output_tokens", 0) or 0
                reqs = res.get("num_model_requests", 0) or 0
                by_model[m]["input_tokens"]  += inp
                by_model[m]["output_tokens"] += out
                by_model[m]["requests"]      += reqs
                # Estimation du coût par modèle
                p = _get_price(OPENAI_PRICING, m)
                by_model[m]["cost_usd"] = round(
                    by_model[m]["cost_usd"]
                    + inp / 1_000_000 * p["input"]
                    + out / 1_000_000 * p["output"],
                    6,
                )

        # 3. Crédits prépayés (legacy billing API — postpay → None)
        prepaid_total = prepaid_used = prepaid_remaining = None
        try:
            r_billing = httpx.get(
                "https://api.openai.com/dashboard/billing/credit_grants",
                headers=hdrs,
                timeout=10,
            )
            if r_billing.status_code == 200:
                bg = r_billing.json()
                prepaid_total     = bg.get("total_granted")
                prepaid_used      = bg.get("total_used")
                prepaid_remaining = bg.get("total_available")
        except Exception:
            pass

        return {
            "provider":              "openai",
            "status":                "ok",
            "collected_at":          datetime.utcnow().isoformat(),
            "org":                   org_name,
            "total_usage_usd_30d":   round(total, 4),
            "daily":                 daily,
            "by_model":              by_model,
            "prepaid_total_usd":     prepaid_total,
            "prepaid_used_usd":      prepaid_used,
            "prepaid_remaining_usd": prepaid_remaining,
        }
    except Exception as e:
        return {"provider": "openai", "status": "error", "error": str(e)}


# ─── ChatGPT Plus (OAuth) ──────────────────────────────────────────────────────

def collect_chatgpt_plus():
    """Collect ChatGPT Plus account info + rate limits via OAuth token."""
    if not CHATGPT_OAUTH_TOKEN:
        return {"status": "no_token"}
    hdrs = {"Authorization": f"Bearer {CHATGPT_OAUTH_TOKEN}"}
    try:
        # /v1/me — account info, plan type, org
        r_me = httpx.get("https://api.openai.com/v1/me", headers=hdrs, timeout=10)
        me = r_me.json()
        auth = me.get("https://api.openai.com/auth", {})
        # If /v1/me doesn't have auth claims, try decoding from token
        if not auth:
            import base64
            parts = CHATGPT_OAUTH_TOKEN.split(".")
            if len(parts) >= 2:
                payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
                claims = json.loads(base64.urlsafe_b64decode(payload))
                auth = claims.get("https://api.openai.com/auth", {})

        plan = auth.get("chatgpt_plan_type", "unknown")
        user_id = me.get("id", auth.get("chatgpt_user_id", "?"))
        name = me.get("name", "")
        email = me.get("https://api.openai.com/profile", {}).get("email", "")

        # Rate limits — make a minimal completion to read headers
        rate_limits = {}
        try:
            r_rl = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={**hdrs, "Content-Type": "application/json"},
                json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "1"}], "max_tokens": 1},
                timeout=10,
            )
            for h in ("x-ratelimit-limit-requests", "x-ratelimit-limit-tokens",
                       "x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens",
                       "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
                if h in r_rl.headers:
                    rate_limits[h.replace("x-ratelimit-", "")] = r_rl.headers[h]
        except Exception:
            pass

        return {
            "status": "ok",
            "plan": plan,
            "name": name,
            "user_id": user_id,
            "rate_limits": rate_limits,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ─── Anthropic billing API ─────────────────────────────────────────────────────

def _collect_anthropic_billing():
    """
    Tente de récupérer le solde Anthropic Console.
    Source A : ANTHROPIC_CONSOLE_API_KEY (clé dédiée billing)
    Source B : token Claude Code CLI depuis $CLAUDE_HOME
    Retourne un dict ou None si indisponible.
    """
    def _try_key(key):
        if not key:
            return None
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/credits",
                headers={
                    "x-api-key":         key,
                    "anthropic-version": "2023-06-01",
                },
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
            # Format attendu : {"total": ..., "used": ..., "remaining": ...}
            # (endpoint non documenté publiquement — traiter avec souplesse)
            return {
                "billing_status":        "ok",
                "credits_total_usd":     data.get("total"),
                "credits_used_usd":      data.get("used"),
                "credits_remaining_usd": data.get("remaining"),
            }
        except Exception:
            return None

    # Source A — clé console dédiée
    result = _try_key(ANTHROPIC_CONSOLE_KEY)
    if result:
        result["billing_source"] = "console_key"
        return result

    # Source B — token Claude Code CLI
    try:
        # Claude Code stocke ses credentials dans ~/.claude/
        for fname in [".credentials.json", "credentials.json", ".auth.json"]:
            fpath = os.path.join(CLAUDE_HOME, fname)
            if os.path.exists(fpath):
                with open(fpath) as f:
                    creds = json.load(f)
                token = (creds.get("anthropic_api_key")
                         or creds.get("api_key")
                         or creds.get("token"))
                if token:
                    result = _try_key(token)
                    if result:
                        result["billing_source"] = "claude_cli"
                        return result
    except Exception:
        pass

    return {"billing_status": "unavailable", "billing_source": "none"}


# ─── Anthropic (depuis logs OpenClaw) ──────────────────────────────────────────

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
        by_model[m]["input_tokens"]  += e["input_tokens"]
        by_model[m]["output_tokens"] += e["output_tokens"]
        if e["cost_usd"]:
            by_model[m]["cost_usd"] = round(by_model[m]["cost_usd"] + e["cost_usd"], 6)
        else:
            p = _get_price(ANTHROPIC_PRICING, m)
            by_model[m]["cost_usd"] = round(
                by_model[m]["cost_usd"]
                + e["input_tokens"] / 1_000_000 * p["input"]
                + e["output_tokens"] / 1_000_000 * p["output"],
                6,
            )

    total_cost   = round(sum(v["cost_usd"] for v in by_model.values()), 6)
    total_input  = sum(v["input_tokens"] for v in by_model.values())
    total_output = sum(v["output_tokens"] for v in by_model.values())

    billing = _collect_anthropic_billing()

    return {
        "provider":             "anthropic",
        "status":               "ok" if entries else "no_logs",
        "source":               "openclaw_sessions",
        "collected_at":         datetime.utcnow().isoformat(),
        "period_days":          days,
        "total_input_tokens":   total_input,
        "total_output_tokens":  total_output,
        "estimated_cost_usd":   total_cost,
        "by_model":             by_model,
        "note":                 "Coûts depuis logs OpenClaw. Fallback: grille pricing publique.",
        **billing,
    }


# ─── Google Gemini ─────────────────────────────────────────────────────────────

def _collect_google_billing():
    """
    Tente de récupérer le coût réel depuis la Cloud Billing API Google.
    Nécessite un service account JSON avec le rôle billing.accounts.viewer.
    Retourne un dict ou None si non configuré.
    """
    if not os.path.exists(GOOGLE_SA_KEY_PATH):
        return None
    try:
        with open(GOOGLE_SA_KEY_PATH) as f:
            sa = json.load(f)

        import urllib.request, urllib.parse, time, base64, hmac, hashlib

        # 1. Créer un JWT pour l'authentification service account
        now = int(time.time())
        header  = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).rstrip(b"=")
        payload = base64.urlsafe_b64encode(json.dumps({
            "iss": sa["client_email"],
            "scope": "https://www.googleapis.com/auth/cloud-billing.readonly",
            "aud": "https://oauth2.googleapis.com/token",
            "exp": now + 3600,
            "iat": now,
        }).encode()).rstrip(b"=")

        # Signer avec RSA (nécessite cryptography ou fallback)
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
            private_key = serialization.load_pem_private_key(
                sa["private_key"].encode(), password=None
            )
            msg = header + b"." + payload
            sig = private_key.sign(msg, padding.PKCS1v15(), hashes.SHA256())
            token_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=")
            jwt_token = (msg + b"." + token_b64).decode()
        except ImportError:
            # Sans cryptography → pas de billing Google
            return {"billing_status": "no_crypto_lib"}

        # 2. Échanger JWT contre access token
        body = urllib.parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion":  jwt_token,
        }).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            token_data = json.loads(resp.read().decode())
        access_token = token_data.get("access_token")
        if not access_token:
            return {"billing_status": "token_error"}

        # 3. Lister les billing accounts
        req2 = urllib.request.Request(
            "https://cloudbilling.googleapis.com/v1/billingAccounts",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(req2, timeout=10) as resp:
            accounts = json.loads(resp.read().decode())
        if not accounts.get("billingAccounts"):
            return {"billing_status": "no_billing_account"}

        account_name = accounts["billingAccounts"][0]["name"]

        # 4. Rapport de coûts du mois courant (v2beta)
        from datetime import date
        today = date.today()
        period = f"{today.year:04d}-{today.month:02d}"
        req3 = urllib.request.Request(
            f"https://cloudbilling.googleapis.com/v2beta/{account_name}/reports"
            f"?dateRange.startDate.year={today.year}"
            f"&dateRange.startDate.month={today.month}"
            f"&dateRange.startDate.day=1"
            f"&dateRange.endDate.year={today.year}"
            f"&dateRange.endDate.month={today.month}"
            f"&dateRange.endDate.day={today.day}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        try:
            with urllib.request.urlopen(req3, timeout=10) as resp:
                report = json.loads(resp.read().decode())
            by_service = {}
            total = 0.0
            for entry in report.get("costSummaryEntries", []):
                svc = entry.get("serviceName", "unknown")
                cost = float(entry.get("cost", {}).get("units", 0))
                if "gemini" in svc.lower() or "generative" in svc.lower() or "aiplatform" in svc.lower():
                    by_service[svc] = round(cost, 4)
                    total += cost
            return {
                "billing_status":   "ok",
                "billing_source":   "gcp_billing_api",
                "billing_period":   period,
                "billing_total_usd": round(total, 4),
                "billing_by_service": by_service,
            }
        except Exception:
            # Reports API peut nécessiter BigQuery export → retourner au moins le statut ok
            return {"billing_status": "ok_no_report", "billing_source": "gcp_billing_api"}

    except Exception as e:
        return {"billing_status": f"error: {str(e)[:80]}"}


def collect_google(days=30):
    """
    Google Gemini — log parsing + Cloud Billing API si service account configuré.
    """
    def is_google_direct(model, provider):
        if "openrouter" in provider.lower():
            return False
        m = model.lower()
        return "gemini" in m or ("google" in m and "gemini" in m)

    entries = _parse_session_files(is_google_direct, days=days)

    by_model = {}
    for e in entries:
        m = e["model"]
        if m not in by_model:
            by_model[m] = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        by_model[m]["input_tokens"]  += e["input_tokens"]
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

    billing = _collect_google_billing() or {"billing_status": "no_sa_key"}

    return {
        "provider":          "google",
        "status":            "ok" if entries else "no_logs",
        "source":            "openclaw_sessions",
        "collected_at":      datetime.utcnow().isoformat(),
        "mode":              "gemini_api_key",
        "estimated_cost_usd": total_cost,
        "by_model":          by_model,
        "note":              "Clé API simple — estimation depuis logs OpenClaw.",
        **billing,
    }


# ─── Collecte complète ──────────────────────────────────────────────────────────

def collect_all():
    return {
        "collected_at": datetime.utcnow().isoformat(),
        "providers": {
            "openrouter": collect_openrouter(),
            "openai":     collect_openai(),
            "anthropic":  collect_anthropic(),
            "google":     collect_google(),
        }
    }


if __name__ == "__main__":
    result = collect_all()
    print(json.dumps(result, indent=2))
