#!/usr/bin/env python3
"""
daily-health-check.py — Collecte les infos de santé du VPS.
Tourne en cron job sur le HOST (pas dans le container Docker).
Écrit un rapport markdown sur stdout (pour Matrix/notification) ET
un fichier JSON sidecar dans ./data/ pour le dashboard.
"""

import subprocess
import json
import os
from datetime import datetime, timedelta

# Chemin du sidecar JSON lu par le dashboard (doit correspondre au volume ./data:/data)
SIDECAR_PATH = os.environ.get(
    "HEALTH_SIDECAR",
    os.path.join(os.path.dirname(__file__), "data", "daily-health.json"),
)

BASELINE = [
    "trusted_proxies_missing",
    "weak_tier",
    "multi_user_heuristic",
    "security_full_configured",
    "tools_reachable_permissive_policy",
    "sandbox=off",
    "sandbox off",
]


def run(cmd, shell=True, timeout=30):
    try:
        r = subprocess.run(cmd, shell=shell, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT"
    except Exception as e:
        return "", str(e)


def filter_baseline(text):
    filtered = [l for l in text.splitlines() if not any(b in l for b in BASELINE)]
    return "\n".join(filtered).strip()


lines = []
lines.append(f"## 🏥 Daily Health Check — {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC")
lines.append("")

# --- VPS Uptime ---
uptime_human, _ = run("uptime -p")
uptime_since, _ = run("uptime -s")
lines.append("### 🖥 VPS Uptime")
lines.append(f"- {uptime_human} (since {uptime_since})")
lines.append("")

# --- Apt updates last 24h ---
since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d")
out, _ = run(f"grep -E ' install | upgrade ' /var/log/dpkg.log | awk '$0 >= \"{since}\"' 2>/dev/null")
pkgs = [l for l in out.splitlines() if l.strip()]
lines.append(f"### 📦 Apt updates (last 24h): {len(pkgs)} packages")
for p in (pkgs[-20:] if pkgs else []):
    lines.append(f"  - {p}")
if not pkgs:
    lines.append("  - none")
lines.append("")

# --- Watchtower ---
out, _ = run("docker logs watchtower --since 24h 2>&1 | grep -iE 'updated|pulled|updating' | tail -20")
wt_lines = [l for l in out.splitlines() if l.strip()]
lines.append("### 🐳 Watchtower — images updated (last 24h)")
for l in wt_lines:
    lines.append(f"  - {l}")
if not wt_lines:
    lines.append("  - No updates")
lines.append("")

# --- Docker containers ---
out, _ = run("docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' 2>/dev/null")
lines.append("### 🐳 Docker containers")
if out:
    for l in out.splitlines():
        lines.append(f"  {l}")
else:
    lines.append("  (no containers running or docker unavailable)")
lines.append("")

# --- OpenClaw Doctor ---
out, err = run("openclaw doctor --yes 2>&1", timeout=60)
doctor_filtered = filter_baseline(out + err)
lines.append("### 🔒 OpenClaw Doctor")
if doctor_filtered:
    lines.append("```")
    lines.append(doctor_filtered[:2000])
    lines.append("```")
else:
    lines.append("✅ All clear (no new issues)")
lines.append("")

# --- OpenClaw Security Audit ---
out, err = run("openclaw security audit 2>&1", timeout=60)
audit_filtered = filter_baseline(out + err)
lines.append("### 🛡 Security Audit")
if audit_filtered:
    lines.append("```")
    lines.append(audit_filtered[:2000])
    lines.append("```")
else:
    lines.append("✅ All clear (no new issues beyond known baseline)")
lines.append("")

# Sortie markdown (stdout — pour Matrix/notification)
print("\n".join(lines))

# ── JSON sidecar pour le dashboard ────────────────────────────────────────────
sidecar = {
    "collected_at":   datetime.now().isoformat(),
    "doctor":         doctor_filtered if doctor_filtered else "✅ All clear",
    "security_audit": audit_filtered if audit_filtered else "✅ All clear",
    "watchtower_raw": wt_lines,
    "apt_updates":    pkgs,
}
try:
    os.makedirs(os.path.dirname(SIDECAR_PATH), exist_ok=True)
    with open(SIDECAR_PATH, "w") as f:
        json.dump(sidecar, f, indent=2)
except Exception as e:
    print(f"[WARN] Impossible d'écrire le sidecar JSON : {e}", flush=True)
