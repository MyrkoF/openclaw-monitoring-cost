#!/usr/bin/env python3
"""
daily-health-check.py — Collecte les métriques hôte pour le dashboard monitoring.

Tourne sur le HOST (pas dans le container Docker).
Écrit ./data/host-health.json (lu par le container via le volume ./data:/data).
Stdout : rapport markdown pour Matrix/notifications.

Cron : */10 * * * * cd ~/openclaw-monitoring-cost && python3 daily-health-check.py >/dev/null 2>&1
"""

import subprocess
import json
import os
import shlex
from datetime import datetime, timezone
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────

SIDECAR_PATH = Path(os.environ.get(
    "HEALTH_SIDECAR",
    Path(__file__).parent / "data" / "host-health.json",
))

SERVICES_TO_CHECK = ["docker", "caddy", "nginx", "ssh", "ufw", "fail2ban"]

OPENCLAW_DOCTOR_BASELINE = [
    "trusted_proxies_missing",
    "weak_tier",
    "multi_user_heuristic",
    "security_full_configured",
    "tools_reachable_permissive_policy",
    "sandbox=off",
    "sandbox off",
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def run(cmd, timeout=30):
    """Execute shell command, return (stdout, stderr, returncode). Never raises."""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT", -1
    except Exception as e:
        return "", str(e), -1


def _calc_status(*statuses):
    """Aggregate module statuses: error > warn > ok."""
    if "error" in statuses:
        return "error"
    if "warn" in statuses:
        return "warn"
    return "ok"


# ── Modules de collecte ────────────────────────────────────────────────────────

def collect_meta():
    hostname, _, _  = run("hostname -f")
    uptime_str, _, _ = run("uptime -p")
    kernel, _, _    = run("uname -r")
    return {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "hostname":     hostname or "unknown",
        "uptime":       uptime_str or "",
        "kernel":       kernel or "",
        "global_status": "ok",   # sera recalculé en fin de script
    }


def collect_resources():
    try:
        load_raw, _, _ = run("cat /proc/loadavg")
        load_parts = load_raw.split() if load_raw else []
        load_1m  = float(load_parts[0]) if len(load_parts) > 0 else None
        load_5m  = float(load_parts[1]) if len(load_parts) > 1 else None
        load_15m = float(load_parts[2]) if len(load_parts) > 2 else None
    except Exception:
        load_1m = load_5m = load_15m = None

    try:
        free_out, _, _ = run("free -m")
        mem_line = [l for l in free_out.splitlines() if l.startswith("Mem:")][0].split()
        ram_total = int(mem_line[1])
        ram_used  = int(mem_line[2])
        ram_free  = int(mem_line[3])
        ram_pct   = round(ram_used / ram_total * 100) if ram_total else 0
    except Exception:
        ram_total = ram_used = ram_free = ram_pct = None

    try:
        df_out, _, _ = run("df -h /")
        df_line = [l for l in df_out.splitlines() if not l.startswith("Filesystem")][0].split()
        disk_total = df_line[1]
        disk_used  = df_line[2]
        disk_avail = df_line[3]
        disk_pct   = df_line[4]
    except Exception:
        disk_total = disk_used = disk_avail = disk_pct = None

    return {
        "load_1m":     load_1m,
        "load_5m":     load_5m,
        "load_15m":    load_15m,
        "ram_total_mb": ram_total,
        "ram_used_mb":  ram_used,
        "ram_free_mb":  ram_free,
        "ram_pct":      ram_pct,
        "disk_total":  disk_total,
        "disk_used":   disk_used,
        "disk_avail":  disk_avail,
        "disk_pct":    disk_pct,
    }


def collect_docker():
    try:
        out, _, _ = run("docker ps -a --format '{{json .}}' 2>/dev/null")
        containers = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
                containers.append({
                    "name":   c.get("Names", c.get("Name", "")),
                    "image":  c.get("Image", ""),
                    "state":  c.get("State", ""),
                    "status": c.get("Status", ""),
                    "ports":  c.get("Ports", ""),
                })
            except json.JSONDecodeError:
                # Fallback parsing si le format JSON est partiel
                pass
        running = sum(1 for c in containers if c.get("state") == "running")
        return {
            "containers": containers,
            "total":      len(containers),
            "running":    running,
            "stopped":    len(containers) - running,
        }
    except Exception as e:
        return {"containers": [], "total": 0, "running": 0, "stopped": 0, "error": str(e)}


def collect_watchtower():
    try:
        out, err, _ = run("docker logs --tail 50 watchtower 2>&1")
        combined = out or err
        raw_lines = [l for l in combined.splitlines() if l.strip()]
        updates = [l for l in raw_lines if "updated" in l.lower()]
        errors  = [l for l in raw_lines if "error" in l.lower()]
        return {
            "raw_last50": raw_lines,
            "updates":    updates,
            "errors":     errors,
        }
    except Exception as e:
        return {"raw_last50": [], "updates": [], "errors": [], "error": str(e)}


def collect_apt():
    try:
        out, _, _ = run("tail -200 /var/log/dpkg.log 2>/dev/null")
        lines = out.splitlines() if out else []
        installs  = [l for l in lines if " install " in l]
        upgrades  = [l for l in lines if " upgrade " in l]
        recent    = sorted(set(installs + upgrades))
    except Exception:
        recent = installs = upgrades = []

    try:
        upg_out, _, _ = run("apt list --upgradable 2>/dev/null")
        upgradable = [
            l for l in upg_out.splitlines()
            if "/" in l and "upgradable" not in l.lower()
        ]
    except Exception:
        upgradable = []

    return {
        "recent_lines":    recent[-50:],   # garder les 50 dernières
        "install_count":   len(installs),
        "upgrade_count":   len(upgrades),
        "upgradable":      upgradable,
        "upgradable_count": len(upgradable),
    }


def _filter_baseline(lines):
    return [
        l for l in lines
        if not any(b in l for b in OPENCLAW_DOCTOR_BASELINE)
    ]


def collect_openclaw_doctor():
    out, err, code = run("openclaw doctor --yes 2>&1", timeout=60)
    combined = out or err
    lines = [l for l in combined.splitlines() if l.strip()]
    filtered = _filter_baseline(lines)

    has_error = any(
        kw in l.lower() for l in filtered
        for kw in ("error", "✗", "failed", "critical")
    )
    has_warn  = any(
        kw in l.lower() for l in filtered
        for kw in ("warn", "⚠", "warning")
    )
    if code != 0 and has_error:
        status = "error"
    elif has_warn:
        status = "warn"
    else:
        status = "ok"

    return {
        "output":    filtered if filtered else ["✅ All clear"],
        "exit_code": code,
        "status":    status,
    }


def collect_openclaw_security():
    out, err, code = run("openclaw security audit 2>&1", timeout=60)
    combined = out or err
    lines = [l for l in combined.splitlines() if l.strip()]
    filtered = _filter_baseline(lines)

    issues = [
        l for l in filtered
        if any(kw in l.lower() for kw in ("warn", "error", "✗", "⚠", "warning", "failed"))
    ]
    if code != 0 and any("error" in l.lower() for l in issues):
        status = "error"
    elif issues:
        status = "warn"
    else:
        status = "ok"

    return {
        "output":    filtered if filtered else ["✅ All clear"],
        "issues":    issues,
        "exit_code": code,
        "status":    status,
    }


def collect_services():
    result = {}
    for svc in SERVICES_TO_CHECK:
        out, _, code = run(f"systemctl is-active {svc} 2>/dev/null")
        result[svc] = out.strip() if out.strip() else ("active" if code == 0 else "inactive")
    return result


# ── Assemblage ─────────────────────────────────────────────────────────────────

def build_report():
    meta      = collect_meta()
    resources = collect_resources()
    docker    = collect_docker()
    watchtower = collect_watchtower()
    apt       = collect_apt()
    doctor    = collect_openclaw_doctor()
    security  = collect_openclaw_security()
    services  = collect_services()

    # Calcul du statut global
    global_status = _calc_status(
        doctor["status"],
        security["status"],
        "warn" if watchtower.get("errors") else "ok",
        "warn" if apt["upgradable_count"] > 10 else "ok",
    )
    meta["global_status"] = global_status

    return {
        "meta":              meta,
        "resources":         resources,
        "docker":            docker,
        "watchtower":        watchtower,
        "apt":               apt,
        "openclaw_doctor":   doctor,
        "openclaw_security": security,
        "services":          services,
    }


def format_markdown(data):
    """Rapport markdown pour stdout (Matrix/notifications)."""
    meta = data["meta"]
    res  = data["resources"]
    gs   = {"ok": "✅", "warn": "⚠️", "error": "❌"}.get(meta["global_status"], "")
    lines = [
        f"## {gs} Health Check — {meta['hostname']} — {meta['collected_at'][:16]} UTC",
        "",
        f"### 🖥 Système",
        f"- Uptime : {meta['uptime']}  |  Kernel : {meta['kernel']}",
        f"- Load : {res.get('load_1m')} / {res.get('load_5m')} / {res.get('load_15m')}",
        f"- RAM : {res.get('ram_used_mb')} / {res.get('ram_total_mb')} MB ({res.get('ram_pct')}%)",
        f"- Disque / : {res.get('disk_used')} / {res.get('disk_total')} ({res.get('disk_pct')})",
        "",
    ]

    d = data["docker"]
    lines += [
        f"### 🐳 Docker — {d['running']} running / {d['stopped']} stopped",
    ]
    for c in d["containers"]:
        icon = "🟢" if c["state"] == "running" else "🔴"
        lines.append(f"  {icon} {c['name']} — {c['status']}")
    lines.append("")

    wt = data["watchtower"]
    lines += [f"### 🔄 Watchtower — {len(wt['updates'])} updates"]
    for u in wt["updates"]:
        lines.append(f"  - {u}")
    if wt["errors"]:
        lines.append(f"  ⚠️ {len(wt['errors'])} erreur(s)")
    lines.append("")

    a = data["apt"]
    lines += [
        f"### 📦 APT — {a['install_count']} installs, {a['upgrade_count']} upgrades (log récent) · {a['upgradable_count']} upgradable",
    ]
    for pkg in a["upgradable"][:10]:
        lines.append(f"  - {pkg}")
    lines.append("")

    doc = data["openclaw_doctor"]
    lines += [f"### 🔒 OpenClaw Doctor — {doc['status'].upper()}"]
    for l in doc["output"][:10]:
        lines.append(f"  {l}")
    lines.append("")

    sec = data["openclaw_security"]
    lines += [f"### 🛡 Security Audit — {sec['status'].upper()}"]
    for l in (sec["issues"] or sec["output"])[:10]:
        lines.append(f"  {l}")
    lines.append("")

    svcs = data["services"]
    lines += ["### ⚙️ Services"]
    for svc, st in svcs.items():
        icon = "🟢" if st == "active" else "🔴"
        lines.append(f"  {icon} {svc} : {st}")

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    data = build_report()

    # Stdout markdown (Matrix / notification)
    print(format_markdown(data))

    # JSON sidecar pour le dashboard
    try:
        SIDECAR_PATH.parent.mkdir(parents=True, exist_ok=True)
        SIDECAR_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[WARN] Impossible d'écrire le sidecar JSON : {e}", flush=True)
