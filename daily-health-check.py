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
import re

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


def collect_wireguard():
    """Parse `wg show all dump` (tab-separated). Tente sans sudo puis avec."""
    out, err, rc = run("wg show all dump 2>/dev/null || sudo wg show all dump 2>/dev/null")
    if not out:
        return {"interfaces": [], "total_peers": 0, "connected_peers": 0, "status": "unavailable"}

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


def collect_github_auth():
    out, err, rc = run("gh auth status 2>&1", timeout=10)
    combined = (out + "\n" + err).strip()
    account, token_src = "", ""
    for line in combined.splitlines():
        m = re.search(r'account (\S+)', line)
        if m:
            account = m.group(1).strip("()")
        m2 = re.search(r'\(([A-Z_]+)\)', line)
        if m2:
            token_src = m2.group(1)
    return {
        "authenticated": rc == 0,
        "account":       account,
        "token_source":  token_src,
        "status":        "ok" if rc == 0 else "error",
    }


def collect_tmux():
    out, err, rc = run("tmux ls 2>/dev/null")
    if rc != 0 or not out:
        return {"sessions": [], "count": 0, "status": "ok"}
    sessions = []
    for line in out.splitlines():
        if ":" not in line:
            continue
        name = line.split(":")[0].strip()
        m = re.search(r'(\d+) windows?', line)
        windows = int(m.group(1)) if m else 0
        sessions.append({"name": name, "windows": windows, "attached": "(attached)" in line})
    return {"sessions": sessions, "count": len(sessions), "status": "ok"}


# ── Assemblage ─────────────────────────────────────────────────────────────────

def _reuse_openclaw_from_sidecar():
    """Réutilise les résultats openclaw du sidecar s'ils ont moins d'1 heure."""
    try:
        data = json.loads(SIDECAR_PATH.read_text(encoding="utf-8"))
        ts = data.get("meta", {}).get("collected_at", "")
        if not ts:
            return None, None
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
        if age < 3600:
            return data.get("openclaw_doctor"), data.get("openclaw_security")
    except Exception:
        pass
    return None, None


def build_report():
    meta      = collect_meta()
    resources = collect_resources()
    docker    = collect_docker()
    watchtower = collect_watchtower()
    apt       = collect_apt()
    services  = collect_services()
    wireguard = collect_wireguard()
    github_cli = collect_github_auth()
    tmux      = collect_tmux()

    # OpenClaw checks : réutiliser le sidecar si < 1h (économise ~7s)
    cached_doc, cached_sec = _reuse_openclaw_from_sidecar()
    doctor   = cached_doc  or collect_openclaw_doctor()
    security = cached_sec  or collect_openclaw_security()

    # Calcul du statut global
    global_status = _calc_status(
        doctor["status"],
        security["status"],
        wireguard["status"],
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
        "wireguard":         wireguard,
        "github_cli":        github_cli,
        "tmux":              tmux,
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
